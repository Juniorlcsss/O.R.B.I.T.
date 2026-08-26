"""Project O.R.B.I.T. — Cloud Run entrypoint (FastAPI).

Exposes the FleetCommanderPipeline to the world:

* ``POST /api/conjunction_alert`` — run a full mission for one conjunction.
* ``GET  /health``               — liveness probe (unauthenticated).
* ``GET  /api/agent_tree``       — live ADK fleet hierarchy (demo-critical).
* ``GET  /api/satellite_state/{sat_id}``        — GEAP memory-bank state.
* ``GET  /api/conjunction_history/{sat_id}``    — screening history.
* ``GET  /api/armor_report/{trace_id}``         — replay a mission's audit trail.
* ``GET  /api/orbital_state``   — live SGP4 positions of every tracked object.
* ``GET  /api/live_feed``       — Server-Sent-Events stream of audit events.
* ``GET  /api/debrief/{conjunction_id}`` — autonomous Veo mission-debrief video.
* ``GET  /api/audio/{event_type}``       — Lyria mission-control audio cue.
* ``POST /api/watches``                  — start a persistent conjunction watch.
* ``GET  /api/watches``                  — list watches (optional status filter).
* ``GET  /api/watches/{watch_id}``       — one watch document.
* ``POST /api/watches/{watch_id}/approval`` — human gate on escalation.
* ``POST /api/watches/{watch_id}/close`` — manually close a watch.
* ``POST /api/evolution/trigger``  — run one self-evolution cycle (seedable).
* ``GET  /api/evolution/policy``   — active ScreeningPolicy + its envelope.
* ``GET  /api/evolution/history``  — before/after cycle audit trail.
* ``GET  /api/evolution/status``   — freeze state and failure counters.
* ``POST /api/evolution/unfreeze`` — manual human reset after a freeze.
* ``GET  /api/debate/transcript/{trace_id}`` — full strategist-debate transcript.

Security: every ``/api/*`` route requires an ``X-API-Key`` header matching
the ``ORBIT_API_KEY`` environment variable (constant-time comparison).
If the variable is unset, enforcement is disabled with a loud WARNING so
local development stays frictionless. ``/health`` is always open for Cloud
Run probes.

Run locally from the repository root:
    uvicorn app:app --reload --port 8080
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Final, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

import agents
from agents import __version__ as FLEET_VERSION
from agents.orchestrator import (
    STATE_EXECUTION_DECISION,
    STATE_FINAL_STATUS,
    STATE_HUMAN_DISPATCH,
    STATE_MISSION_DOSSIER,
    STATE_TRACE_ID,
    fleet_commander_agent,
)
from agents.watcher import watcher_agent
from evolution.engine import STATE_REPORT_KEY as EVOLUTION_REPORT_KEY, EvolutionEngine
from evolution.learning_analyst import learning_analyst_agent
from evolution.meta_critic import meta_critic_agent
from evolution.outcome import OutcomeSimulator
from evolution.policy import EVOLUTION_ENVELOPE, get_shared_policy_store
from geap_sim.agent_registry import get_shared_registry
from geap_sim.memory_bank import get_shared_memory_bank
from geap_sim.model_armor import STATUS_APPROVED
from geap_sim.observability import audit_logger
from tools.audio_generator import MEDIA_TYPE, available_audio_events, get_audio_spec, get_event_audio
from tools.debrief_generator import get_debrief
from tools.space_tools import get_orbital_snapshot

# ---------------------------------------------------------------------------
# Configuration & shared singletons
# ---------------------------------------------------------------------------

APP_NAME: Final[str] = "orbit-fleet-commander"
API_KEY_HEADER: Final[str] = "X-API-KEY"
_API_KEY: Final[str] = os.getenv("ORBIT_API_KEY", "").strip()

memory_bank = get_shared_memory_bank()
session_service = InMemorySessionService()
runner = Runner(agent=fleet_commander_agent, app_name=APP_NAME, session_service=session_service)

# Self-evolution subsystem (Phase 10): production engine with the real
# analyst + adversarial Meta-Critic; evaluation tests inject scripted twins.
evolution_engine = EvolutionEngine(
    name="evolution_engine",
    description=(
        "Self-evolution control plane. Reviews mission outcomes, proposes "
        "ScreeningPolicy adjustments, gates them behind deterministic gaming "
        "heuristics and an adversarial Meta-Critic, clamps every candidate "
        "into the hard safety envelope and freezes itself when it misbehaves."
    ),
    learning_analyst=learning_analyst_agent,
    meta_critic=meta_critic_agent,
    # Formal sub-agents so /api/agent_tree shows the full evolution hierarchy.
    sub_agents=[learning_analyst_agent, meta_critic_agent],
)

if not _API_KEY:
    audit_logger.log_event(
        trace_id="startup",
        agent_name="orbit.api",
        event_type="API_KEY_ENFORCEMENT_DISABLED",
        payload={"reason": "ORBIT_API_KEY environment variable is not set"},
        status="DEGRADED",
    )


# ---------------------------------------------------------------------------
# Lifespan: boot-time attestation (fail the deployment, not the mission)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    registry = get_shared_registry()
    registry_agents = fleet_commander_agent.fleet_roster
    missing = [name for name in registry_agents if name not in registry.list_agents()]
    if missing:
        # Import-time attestation should have caught this; belt and braces.
        raise SystemExit(f"Boot attestation failed: agents missing manifests: {missing}")
    audit_logger.log_event(
        trace_id="startup",
        agent_name="orbit.api",
        event_type="BOOT_ATTESTATION",
        payload={"agents": list(registry_agents), "fleet_version": FLEET_VERSION},
        status="APPROVED",
    )

    # Long-running watch supervisor: crash recovery first (reload persisted
    # watches, audit the resume), then the periodic heartbeat task.
    watcher_agent.escalation_handler = _watch_escalation_handler
    resumed = await watcher_agent.resume_active_watches()
    supervisor_task = asyncio.create_task(watcher_agent.supervisor_loop())
    audit_logger.log_event(
        trace_id="startup",
        agent_name="orbit.api",
        event_type="WATCH_SUPERVISOR_ONLINE",
        payload={"resumed_watches": resumed},
        status="OK",
    )
    try:
        yield
    finally:
        supervisor_task.cancel()
        try:
            await supervisor_task
        except asyncio.CancelledError:
            pass


async def _watch_escalation_handler(watch: dict[str, Any]) -> dict[str, Any]:
    """Bridge a human-approved watch escalation into a full fleet mission."""
    response = await execute_mission(
        {
            "sat_id": str(watch.get("sat_id", "")),
            "debris_id": str(watch.get("debris_id", "")),
            "alert_source": "ORBIT_WATCH_ESCALATION",
            "priority": "URGENT",
            "raw_message": (
                f"Watch {watch.get('watch_id')} escalated: risk band "
                f"{watch.get('last_risk_band')} (Pc={watch.get('last_pc')}, "
                f"miss={watch.get('last_miss_distance_km')} km). Human approval granted."
            ),
        }
    )
    return {"trace_id": response.trace_id, "status": response.status}


app = FastAPI(
    title="Project O.R.B.I.T.",
    description=(
        "Orchestrated Routing & Ballistic Incident Tracking — an ADK multi-agent "
        "fleet that screens space-debris conjunctions, negotiates dodge "
        "responsibility and enforces Model Armour safety policy."
    ),
    version=FLEET_VERSION,
    lifespan=lifespan,
)

# CORS for separately-hosted command-center frontends (Firebase/Vercel).
# Comma-separated allowlist via ORBIT_CORS_ORIGINS; default "*" because the
# API-key gate still protects every /api/* route.
_CORS_ORIGINS: Final[list[str]] = [
    origin.strip() for origin in os.getenv("ORBIT_CORS_ORIGINS", "*").split(",") if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-KEY", "Content-Type", "X-ORBIT-TRACE-ID"],
    expose_headers=["X-ORBIT-SERVICE"],
)


# ---------------------------------------------------------------------------
# Middleware: API-key gate + structured request logging
# ---------------------------------------------------------------------------


@app.middleware("http")
async def security_and_audit_middleware(request: Request, call_next):
    """Constant-time API-key gate on /api/* plus per-request audit lines."""
    started = time.perf_counter()

    if _API_KEY and request.url.path.startswith("/api/"):
        supplied = request.headers.get(API_KEY_HEADER, "")
        if not supplied or not hmac.compare_digest(supplied.encode(), _API_KEY.encode()):
            audit_logger.log_event(
                trace_id="-",
                agent_name="orbit.api.gateway",
                event_type="AUTH_REJECTED",
                payload={"method": request.method, "path": request.url.path},
                status="REJECTED",
            )
            return JSONResponse(
                status_code=401,
                content={"detail": f"Missing or invalid {API_KEY_HEADER} header."},
            )

    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    audit_logger.log_event(
        trace_id=request.headers.get("X-ORBIT-TRACE-ID", "-"),
        agent_name="orbit.api.gateway",
        event_type="HTTP_REQUEST",
        payload={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
        status="OK" if response.status_code < 400 else "FAILED",
    )
    response.headers["X-ORBIT-SERVICE"] = APP_NAME
    return response


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConjunctionAlertRequest(BaseModel):
    """Inbound space-tracking alert (mocking a SPACE_TRACK / radar feed)."""

    sat_id: str = Field(..., min_length=3, max_length=64, examples=["LANCASTER_ORBIT_1"])
    debris_id: str = Field(..., min_length=3, max_length=64, examples=["FENGYUN_1C_DEB"])
    alert_source: str = Field(default="SPACE_TRACK_API", max_length=64)
    priority: Literal["ROUTINE", "URGENT", "CRITICAL"] = "ROUTINE"
    raw_message: Optional[str] = Field(
        default=None,
        description="Messy free-text from the feed; alert_triage will normalise it.",
    )


class ConjunctionAlertResponse(BaseModel):
    """Structured outcome of one mission run."""

    trace_id: str
    status: str
    risk_band: Optional[str] = None
    miss_distance_km: Optional[float] = None
    pc: Optional[float] = None
    action_taken: Optional[str] = None
    armor_violations: Optional[list[str]] = None
    conjunction_id: Optional[str] = None


class WatchRequest(BaseModel):
    """Command to begin long-running monitoring of one conjunction pair."""

    sat_id: str = Field(..., min_length=3, max_length=64, examples=["LANCASTER_ORBIT_1"])
    debris_id: str = Field(..., min_length=3, max_length=64, examples=["FENGYUN_1C_DEB"])
    interval_hours: float = Field(default=6.0, ge=1.0, le=72.0)
    escalate_band: Literal["LOW", "MEDIUM", "HIGH"] = "HIGH"


class WatchApprovalRequest(BaseModel):
    """Human verdict on a watch escalation awaiting confirmation."""

    approved_by: str = Field(..., min_length=3, max_length=64)
    approved: bool = True


class EvolutionTriggerRequest(BaseModel):
    """Command to run one self-evolution cycle (optionally seeded)."""

    trigger_source: Literal["manual", "post_mission"] = "manual"
    seed: Optional[Literal["over_reactions", "under_reactions", "gaming_temptation"]] = Field(
        default=None,
        description="Inject a synthetic, clearly-marked outcome batch before the cycle.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_agent(agent: Any) -> dict[str, Any]:
    """Recursively describe an ADK agent for the /api/agent_tree endpoint."""
    is_llm = isinstance(agent, LlmAgent)
    config = getattr(agent, "generate_content_config", None) if is_llm else None
    tools = []
    if is_llm:
        for tool in getattr(agent, "tools", None) or []:
            tools.append(getattr(tool, "name", None) or tool.func.__name__)
    return {
        "name": agent.name,
        "type": type(agent).__name__,
        "model": getattr(agent, "model", None) if is_llm else None,
        "tools": tools,
        "temperature": float(config.temperature) if config is not None and config.temperature is not None else None,
        "children": [_describe_agent(child) for child in (getattr(agent, "sub_agents", None) or [])],
    }


def _mission_response(trace_id: str, state: dict[str, Any]) -> ConjunctionAlertResponse:
    dossier: dict[str, Any] = state.get(STATE_MISSION_DOSSIER) or {}
    decision: dict[str, Any] = state.get(STATE_EXECUTION_DECISION) or {}
    negotiated: dict[str, Any] = decision.get("negotiated_action") or {}
    pc_value = dossier.get("pc")
    miss_value = dossier.get("miss_distance_km")
    return ConjunctionAlertResponse(
        trace_id=str(state.get(STATE_TRACE_ID) or trace_id),
        status=str(state.get(STATE_FINAL_STATUS) or "UNKNOWN"),
        risk_band=dossier.get("risk_band"),
        miss_distance_km=float(miss_value) if miss_value is not None else None,
        pc=float(pc_value) if pc_value is not None else None,
        action_taken=decision.get("action") or negotiated.get("action"),
        armor_violations=list(decision.get("violations") or []) or None,
        conjunction_id=decision.get("conjunction_id"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mission execution (shared by the HTTP API and watch escalations)
# ---------------------------------------------------------------------------


async def execute_mission(alert: dict[str, Any]) -> ConjunctionAlertResponse:
    """Run one full collision-response mission for a normalised alert dict.

    Single entry point for both the ``/api/conjunction_alert`` endpoint and
    the WatchCommander's escalation handler, guaranteeing that watches and
    operator-triggered missions share identical pipeline semantics.
    """
    trace_id = uuid.uuid4().hex
    try:
        session = await session_service.create_session(
            app_name=APP_NAME,
            user_id="spaceops-api",
            state={STATE_TRACE_ID: trace_id},
        )
        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=json.dumps({"alert": alert}))],
        )
        async for _ in runner.run_async(user_id=session.user_id, session_id=session.id, new_message=message):
            pass  # events stream into the session; final state is authoritative

        final_session = await session_service.get_session(
            app_name=APP_NAME, user_id=session.user_id, session_id=session.id
        )
        state: dict[str, Any] = dict(final_session.state) if final_session else {}
        return _mission_response(trace_id, state)
    except Exception as exc:  # noqa: BLE001 — surfaced to caller via trace ID
        audit_logger.log_event(
            trace_id=trace_id,
            agent_name="orbit.api",
            event_type="PIPELINE_EXCEPTION",
            payload={"error_type": type(exc).__name__, "error": str(exc)},
            status="CRITICAL",
        )
        raise HTTPException(
            status_code=500,
            detail={"message": "Mission pipeline failed; quote the trace_id to operators.", "trace_id": trace_id},
        ) from exc


@app.post("/api/conjunction_alert", response_model=ConjunctionAlertResponse)
async def conjunction_alert(payload: ConjunctionAlertRequest) -> ConjunctionAlertResponse:
    """Execute a full collision-response mission for one conjunction alert.

    The alert is normalised by alert_triage, screened by the astrodynamics
    specialist (now grounded in vector-memory recall of similar past
    encounters) and — when warranted — negotiated and gated through both
    the LLM Safety Officer and the deterministic Model Armour sweep before
    any execution decision is persisted.
    """
    return await execute_mission(payload.model_dump())


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe for Cloud Run — intentionally unauthenticated."""
    return {
        "status": "healthy",
        "version": FLEET_VERSION,
        "firestore_connected": memory_bank.backend_name == "firestore",
        "memory_backend": memory_bank.backend_name,
        "api_key_enforced": bool(_API_KEY),
    }


@app.get("/api/agent_tree")
async def agent_tree() -> dict[str, Any]:
    """Return the live ADK hierarchy — mission fleet AND evolution engine.

    ``root`` keeps the FleetCommanderPipeline tree (existing consumers);
    ``evolution_root`` adds the self-evolution subsystem and ``orbit_fleet``
    nests both under a single demo-friendly root.
    """
    fleet_root = _describe_agent(fleet_commander_agent)
    evolution_root = _describe_agent(evolution_engine)
    return {
        "fleet_version": FLEET_VERSION,
        "root": fleet_root,
        "evolution_root": evolution_root,
        "orbit_fleet": {
            "name": "orbit_fleet",
            "type": "SystemRoot",
            "model": None,
            "tools": [],
            "temperature": None,
            "children": [fleet_root, evolution_root],
        },
    }


@app.get("/api/satellite_state/{sat_id}")
async def satellite_state(sat_id: str) -> dict[str, Any]:
    """Current vehicle state from the GEAP memory bank.

    Unknown satellites resolve to nominal defaults rather than 404s, mirroring
    first-contact behaviour in the pipeline itself.
    """
    return await memory_bank.get_satellite_state(sat_id)


@app.get("/api/conjunction_history/{sat_id}")
async def conjunction_history(sat_id: str, limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    """Most-recent-first conjunction screening history for a satellite."""
    events = await memory_bank.get_historical_conjunctions(sat_id, limit=limit)
    return {"sat_id": sat_id.upper(), "count": len(events), "events": events}


@app.get("/api/armor_report/{trace_id}")
async def armor_report(trace_id: str) -> dict[str, Any]:
    """Replay the complete audit chain recorded under a mission trace ID."""
    events = audit_logger.get_events_by_trace(trace_id)
    if not events:
        raise HTTPException(
            status_code=404,
            detail={"message": "No audit events for this trace_id.", "trace_id": trace_id},
        )
    return {
        "trace_id": trace_id,
        "event_count": len(events),
        "events": events,
    }


@app.get("/api/debrief/{conjunction_id}")
async def debrief(conjunction_id: str) -> dict[str, Any]:
    """Autonomous Veo mission-debrief for one resolved conjunction.

    The orchestrator queues generation as a background task when a mission
    terminates in ``EXECUTION_AUTHORIZED``, ``MANEUVER_BLOCKED`` or an edge
    autonomous dodge; this endpoint reports generation status and the
    artifact itself. Clients poll until ``debrief_status`` is ``READY``
    (or ``FAILED``). Unknown IDs are 404s; known-but-not-yet-generated
    IDs return ``debrief_status: NOT_QUEUED | PENDING``.
    """
    report = await get_debrief(conjunction_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "Unknown conjunction_id.", "conjunction_id": conjunction_id},
        )
    return report


@app.get("/api/audio/{event_type}")
async def event_audio(event_type: str) -> Response:
    """Mission-control audio cue for one fleet event type.

    Serves a WAV clip generated by Lyria on Vertex AI when
    ``ORBIT_ENABLE_REAL_LYRIA=1``, or by the offline procedural synth
    otherwise; clips are memoised per event type for the process lifetime.
    """
    spec = get_audio_spec(event_type)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Unknown event_type.",
                "event_type": event_type,
                "available": list(available_audio_events()),
            },
        )
    clip = await get_event_audio(event_type)
    if clip is None:
        raise HTTPException(status_code=503, detail={"message": "Audio backend unavailable."})
    return Response(
        content=clip,
        media_type=MEDIA_TYPE,
        headers={"Cache-Control": "public, max-age=3600", "X-ORBIT-AUDIO-SOURCE": "orbit.audio"},
    )


@app.post("/api/watches")
async def create_watch(payload: WatchRequest) -> dict[str, Any]:
    """Start a persistent conjunction watch (idempotent per pair).

    The WatchCommander re-screens the pair every ``interval_hours``; risk
    rising to ``escalate_band`` parks the watch behind explicit human
    approval before any fleet mission is triggered. State persists across
    process restarts via the memory bank.
    """
    try:
        return await watcher_agent.start_watch(
            payload.sat_id,
            payload.debris_id,
            interval_hours=payload.interval_hours,
            escalate_band=payload.escalate_band,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/watches")
async def list_watches(status: str = Query(default=None, pattern="^(ACTIVE|AWAITING_HUMAN_APPROVAL|CLOSED_.*)?$")) -> dict[str, Any]:
    """All conjunction watches, newest-check first."""
    watches = await watcher_agent.list_watches(status or None)
    watches.sort(key=lambda w: str(w.get("last_checked_utc") or w.get("created_utc")), reverse=True)
    return {"count": len(watches), "watches": watches}


@app.get("/api/watches/{watch_id}")
async def get_watch(watch_id: str) -> dict[str, Any]:
    """One watch document by ID."""
    watch = await watcher_agent.get_watch(watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail={"message": "Unknown watch_id.", "watch_id": watch_id})
    return watch


@app.post("/api/watches/{watch_id}/approval")
async def approve_watch(watch_id: str, payload: WatchApprovalRequest) -> dict[str, Any]:
    """Human gate on an escalated watch (required before mission routing)."""
    result = await watcher_agent.approve_escalation(watch_id, payload.approved_by, payload.approved)
    if result is None:
        raise HTTPException(status_code=404, detail={"message": "Unknown watch_id.", "watch_id": watch_id})
    if result.get("error") == "not_awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail={"message": "Watch is not awaiting human approval.", "status": result.get("status")},
        )
    return result


@app.post("/api/watches/{watch_id}/close")
async def close_watch(watch_id: str) -> dict[str, Any]:
    """Manually close one watch."""
    result = await watcher_agent.close_watch(watch_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"message": "Unknown watch_id.", "watch_id": watch_id})
    return result


# ---------------------------------------------------------------------------
# Self-evolution endpoints (Phase 10)
# ---------------------------------------------------------------------------

_SEED_BATCH_SIZE: Final[int] = 12


@app.post("/api/evolution/trigger")
async def trigger_evolution(payload: EvolutionTriggerRequest) -> dict[str, Any]:
    """Run one self-evolution cycle through the real ADK Runner.

    With a ``seed``, the matching synthetic outcome batch (clearly marked
    ``synthetic=True``) is injected first — this is the demo lever for
    showing the learning loop, the gaming detector and the freeze breaker.
    The engine is fail-closed at every stage; the returned EvolutionReport
    states exactly what happened and why.
    """
    seeded = 0
    if payload.seed:
        simulator = OutcomeSimulator(bank=memory_bank)
        batches = {
            "over_reactions": simulator.seed_over_reactions,
            "under_reactions": simulator.seed_under_reactions,
            "gaming_temptation": simulator.seed_gaming_temptation,
        }
        outcomes = await batches[payload.seed](_SEED_BATCH_SIZE)
        seeded = len(outcomes)

    session = await session_service.create_session(app_name=APP_NAME, user_id="evolution-api", state={})
    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=json.dumps({"trigger_source": payload.trigger_source, "seed": payload.seed}))],
    )
    async for _ in Runner(agent=evolution_engine, app_name=APP_NAME, session_service=session_service).run_async(
        user_id=session.user_id, session_id=session.id, new_message=message
    ):
        pass

    final_session = await session_service.get_session(app_name=APP_NAME, user_id=session.user_id, session_id=session.id)
    raw_report = (final_session.state if final_session else {}).get(EVOLUTION_REPORT_KEY, "")
    try:
        report = json.loads(raw_report) if raw_report else {"status": "ENGINE_ERROR", "reasoning": "no report committed"}
    except json.JSONDecodeError:
        report = {"status": "ENGINE_ERROR", "reasoning": "unparsable report"}
    report["seeded_outcomes"] = seeded
    report["trigger_source"] = payload.trigger_source
    return report


@app.get("/api/evolution/policy")
async def evolution_policy() -> dict[str, Any]:
    """The active ScreeningPolicy plus the envelope it can never escape."""
    policy = await get_shared_policy_store().load()
    return {
        "policy": policy.model_dump(),
        "envelope": {name: list(bounds) for name, bounds in EVOLUTION_ENVELOPE.items()},
        "max_step_fraction": 0.20,
    }


@app.get("/api/evolution/history")
async def evolution_history(limit: int = Query(default=10, ge=1, le=50)) -> dict[str, Any]:
    """Before/after audit trail of recent evolution cycles (newest first)."""
    cycles = await memory_bank.get_evolution_history(limit=limit)
    return {"count": len(cycles), "cycles": cycles}


@app.get("/api/evolution/status")
async def evolution_status() -> dict[str, Any]:
    """Freeze state, rejection counter and last cycle trace."""
    return {
        "frozen": bool(await memory_bank.get_meta("evolution_frozen", False)),
        "rejection_counter": int(await memory_bank.get_meta("evolution_rejection_counter", 0) or 0),
        "envelope_push_counter": int(await memory_bank.get_meta("evolution_envelope_push_counter", 0) or 0),
        "last_trace_id": await memory_bank.get_meta("evolution_last_trace_id", ""),
    }


@app.post("/api/evolution/unfreeze")
async def unfreeze_evolution() -> dict[str, Any]:
    """Manual human action: clear the freeze and reset failure counters."""
    await memory_bank.set_meta("evolution_frozen", False)
    await memory_bank.set_meta("evolution_rejection_counter", 0)
    await memory_bank.set_meta("evolution_envelope_push_counter", 0)
    audit_logger.log_event(
        trace_id="evolution",
        agent_name="orbit.api",
        event_type="EVOLUTION_UNFROZEN_MANUAL",
        payload={"by": "human_operator"},
        status="OK",
    )
    return {"frozen": False, "rejection_counter": 0, "envelope_push_counter": 0}


@app.get("/api/debate/transcript/{trace_id}")
async def debate_transcript(trace_id: str) -> dict[str, Any]:
    """Full transcript of one strategist debate (rounds, flags, winner).

    Persisted by the DebateModerator under the mission trace ID; includes
    every argument hash, hallucination/loop/budget flags, judge decisions
    and the final validated proposal.
    """
    from debate.moderator import DEBATE_TRANSCRIPTS_COLLECTION

    doc = await memory_bank.get_doc(DEBATE_TRANSCRIPTS_COLLECTION, trace_id)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "No debate transcript for this trace_id.", "trace_id": trace_id},
        )
    return doc


@app.get("/api/orbital_state")
async def orbital_state() -> dict[str, Any]:
    """Live positions of every tracked object plus active conjunction lines.

    SGP4-propagates the catalogue to the current instant (TEME → WGS84) and
    returns the memoised non-LOW conjunction screens. Off the event loop so a
    burst of dashboard polls never starves mission traffic.
    """
    return await asyncio.to_thread(get_orbital_snapshot)


@app.get("/api/live_feed")
async def live_feed(request: Request, replay: int = Query(default=40, ge=0, le=500)) -> StreamingResponse:
    """Server-Sent-Events stream of audit records as they are committed.

    Source of truth is the AuditLogger ring buffer itself — the same records
    Cloud Logging captures. Each client keeps a ``seq`` cursor; new events
    are pushed within ~400 ms and ``: heartbeat`` comments keep proxies from
    idling the connection out. ``replay`` backfills the most recent events on
    connect so a freshly opened dashboard is never blank.
    """
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    async def event_stream() -> AsyncIterator[str]:
        last_seq = max(audit_logger.latest_seq() - replay, 0)
        next_heartbeat = time.monotonic() + 15.0
        yield ": orbit live feed connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            batch = audit_logger.get_events_since(last_seq)
            for record in batch:
                last_seq = int(record.get("seq", last_seq))
                yield f"id: {last_seq}\nevent: audit\ndata: {json.dumps(record, default=str)}\n\n"
            if not batch and time.monotonic() >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = time.monotonic() + 15.0
            await asyncio.sleep(0.4)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
