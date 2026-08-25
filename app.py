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
    yield


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


@app.post("/api/conjunction_alert", response_model=ConjunctionAlertResponse)
async def conjunction_alert(payload: ConjunctionAlertRequest) -> ConjunctionAlertResponse:
    """Execute a full collision-response mission for one conjunction alert.

    The alert is normalised by alert_triage, screened by the astrodynamics
    specialist and — when warranted — negotiated and gated through both the
    LLM Safety Officer and the deterministic Model Armour sweep before any
    execution decision is persisted.
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
            parts=[genai_types.Part(text=json.dumps({"alert": payload.model_dump()}))],
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
    """Return the live ADK fleet hierarchy — the architecture, provable."""
    return {"fleet_version": FLEET_VERSION, "root": _describe_agent(fleet_commander_agent)}


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
