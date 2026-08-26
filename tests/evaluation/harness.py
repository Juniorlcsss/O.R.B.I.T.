"""Evaluation harness for Project O.R.B.I.T.'s automated suite.

Design philosophy â€” "test the plane, not the weather"
-----------------------------------------------------
The fleet's *orchestration* is deterministic code: routing, circuit
breakers, dual-gate Model Armour, memory persistence, audit correlation.
The specialist LLMs are stochastic weather. This harness therefore runs
the **real** FleetCommanderPipeline end-to-end while substituting the four
specialist LLMs with scripted agents whose outputs are schema-valid (and,
where possible, computed from the real SGP4 tools). Everything downstream
of an agent's JSON answer â€” validation, branching, armour gating,
persistence, audit emission â€” executes unmodified production code.

This makes the entire suite fast (<30 s), hermetic (no network, no
credentials, no cost) and reproducible, while a ``--live`` mode exists for
credentialled environments that want the true models in the loop.

Every test module exposes::

    NAME / DESCRIPTION
    async def execute(harness) -> list[CheckResult]
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pydantic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Force the in-memory backend BEFORE anything constructs the shared bank:
# hermetic, per-instance isolation and zero Firestore dependency.
os.environ.setdefault("ORBIT_MEMORY_BACKEND", "memory")

from google.adk.agents.base_agent import BaseAgent  # noqa: E402
from google.adk.agents.invocation_context import InvocationContext  # noqa: E402
from google.adk.events import Event, EventActions  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

from agents.edge_agent import gemma_edge_agent  # noqa: E402
from agents.orchestrator import (  # noqa: E402
    STATE_FINAL_STATUS,
    STATE_TRACE_ID,
    FleetCommanderPipeline,
    alert_triage_agent as real_triage,
    astrodynamics_agent as real_astro,
    diplomat_agent as real_diplomat,
    safety_officer_agent as real_safety,
)
from agents.safety import MAX_ALLOWED_DELTA_V_MPS  # noqa: E402
from agents.watcher import watcher_agent  # noqa: E402
from debate.moderator import debate_moderator_agent  # noqa: E402
from geap_sim.memory_bank import MemoryBank, safe_document_id  # noqa: E402
from geap_sim.model_armor import ModelArmor  # noqa: E402
from geap_sim.observability import audit_logger  # noqa: E402
from tools.space_tools import screen_conjunction  # noqa: E402


def astro_screening_fixture(
    sat_id: str = "LANCASTER_ORBIT_1",
    debris_id: str = "FENGYUN_1C_DEB",
    recommended_dv_mps: float = 8.0,
) -> dict[str, Any]:
    """Screening in the astrodynamics specialist's OUTPUT CONTRACT shape —
    exactly what the debate moderator receives inside a live mission."""
    screened = screen_conjunction(sat_id, debris_id)
    assert screened.get("status") == "ok"
    return {
        "sat_id": sat_id.upper(),
        "debris_id": debris_id.upper(),
        "risk_band": screened["risk_level"],
        "pc": float(screened["probability_of_collision"]),
        "miss_distance_km": float(screened["miss_distance_km"]),
        "tca_iso": screened["tca_utc"],
        "recommended_dv_mps": recommended_dv_mps,
        "dv_direction": "prograde" if screened["risk_level"] == "HIGH" else "none",
        "reasoning": "harness fixture grounded in real SGP4 screening",
    }


@dataclass
class CheckResult:
    """One named assertion inside an evaluation test."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class MissionOutcome:
    """Everything one scripted mission produced, for assertions."""

    trace_id: str
    final_status: str
    state: dict[str, Any]
    audit_events: list[dict[str, Any]]
    duration_s: float


# ---------------------------------------------------------------------------
# Scripted specialist stand-ins
# ---------------------------------------------------------------------------


class ScriptedAgent(BaseAgent):
    """Minimal ADK agent that writes one canned payload to its output key.

    ``payload_factory`` returns a JSON-serialisable dict (use a closure over
    the scenario data; factories may call real tools). The written value
    mimics exactly what an LlmAgent leaves behind for ``output_key``, so
    every downstream validator and router behaves identically to production.
    """

    output_key: str = ""
    payload_factory: Any = None

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    async def _run_async_impl(self, ctx: InvocationContext):
        factory = self.payload_factory if callable(self.payload_factory) else (lambda: dict(self.payload_factory or {}))
        payload = factory()
        serialised = json.dumps(payload)
        ctx.session.state[self.output_key] = serialised
        yield Event(
            author=self.name,
            content=genai_types.Content(role="model", parts=[genai_types.Part(text=f"[scripted] {self.name} responded.")]),
            actions=EventActions(state_delta={self.output_key: serialised}),
        )


class FailingAgent(BaseAgent):
    """Specialist stand-in that always raises â€” the circuit-breaker fuel."""

    error_message: str = "simulated provider outage"

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    async def _run_async_impl(self, ctx: InvocationContext):
        raise RuntimeError(self.error_message)
        yield  # pragma: no cover â€” makes this an async generator


# ---------------------------------------------------------------------------
# Scenario payload factories (schema-valid per the orchestrator's validators)
# ---------------------------------------------------------------------------


def triage_payload(sat_id: str, debris_id: str, urgency: str = "URGENT", fuel: float | None = None) -> dict[str, Any]:
    return {
        "valid": True,
        "sat_id": sat_id,
        "debris_id": debris_id,
        "our_fuel_percent_remaining": fuel,
        "urgency": urgency,
        "notes": "scripted triage: identifiers extracted cleanly",
    }


def screening_payload(
    risk_band: str,
    pc: float,
    miss_distance_km: float,
    recommended_dv_mps: float,
    dv_direction: str = "prograde",
    hours_to_tca: float = 9.0,
) -> dict[str, Any]:
    """A screening answer; when ``pc`` is None it comes from the real SGP4 tool."""
    tca_iso = (datetime.now(timezone.utc) + timedelta(hours=hours_to_tca)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "risk_band": risk_band,
        "pc": pc,
        "miss_distance_km": miss_distance_km,
        "tca_iso": tca_iso,
        "recommended_dv_mps": recommended_dv_mps,
        "dv_direction": dv_direction,
        "reasoning": f"scripted screening consistent with CARA bands ({risk_band})",
    }


def real_screening_payload(sat_id: str, debris_id: str) -> Callable[[], dict[str, Any]]:
    """Factory backed by the REAL SGP4 screening tool â€” truthful orbital math."""

    def factory() -> dict[str, Any]:
        screened = screen_conjunction(sat_id, debris_id)
        if screened.get("status") != "ok":
            raise RuntimeError(f"real screening failed: {screened}")
        direction = "prograde" if screened["risk_level"] == "HIGH" else "none"
        return screening_payload(
            risk_band=screened["risk_level"],
            pc=float(screened["probability_of_collision"]),
            miss_distance_km=float(screened["miss_distance_km"]),
            recommended_dv_mps=8.0 if direction != "none" else 0.0,
            dv_direction=direction,
            hours_to_tca=24.0,
        )

    return factory


def negotiation_payload(action: str = "we_dodge", our_dv: float = 10.0, their_dv: float = 0.0, reasoning: str = "scripted negotiation") -> dict[str, Any]:
    return {
        "action": action,
        "our_dv_mps": our_dv,
        "their_dv_mps": their_dv,
        "ack_signature": secrets.token_hex(32),  # 64-hex MAC, as the validator demands
        "reasoning": reasoning,
    }


def verdict_payload(approved: bool = True, rationale: str = "scripted safety review: within policy") -> dict[str, Any]:
    return {
        "approved": approved,
        "threat_level": "LOW" if approved else "CRITICAL",
        "violations": [],
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Pipeline assembly & execution
# ---------------------------------------------------------------------------

APP_NAME = "orbit-evaluation"


class EvaluationHarness:
    """Builds isolated pipelines and runs missions against them."""

    def __init__(self) -> None:
        # Fresh MemoryBank per harness == isolated satellite/conjunction store.
        self.memory_bank = MemoryBank()
        self.armor = ModelArmor(memory_bank=self.memory_bank)
        from evolution.policy import PolicyStore

        self.policy_store = PolicyStore(bank=self.memory_bank)
        self._session_service = InMemorySessionService()

    def build_pipeline(
        self,
        *args: Any, triage: BaseAgent | None = None,
        astro: BaseAgent | None = None, diplomat: BaseAgent | None = None,
        safety: BaseAgent | None = None, debate_moderator: BaseAgent | None = None,
    ) -> FleetCommanderPipeline:
        """Real FleetCommanderPipeline with selected specialists replaced.

        Accepts either ``build_pipeline(specialists_tuple)`` or
        ``build_pipeline(t, a, d, s)``; keyword overrides win over positionals.
        Unspecified slots stay as the production LLM agents.
        """
        if len(args) == 1 and isinstance(args[0], (tuple, list)):
            p_triage, p_astro, p_diplomat, p_safety = args[0]
        elif len(args) == 4:
            p_triage, p_astro, p_diplomat, p_safety = args
        elif len(args) == 2:  # (triage, astro) â€” chaos scenarios kill upstream first
            p_triage, p_astro, p_diplomat, p_safety = args[0], args[1], None, None
        else:
            p_triage = p_astro = p_diplomat = p_safety = None
        return FleetCommanderPipeline(
            name="fleet_commander_eval",
            description="Evaluation twin of the production pipeline.",
            alert_triage=triage or p_triage or real_triage,
            astrodynamics_specialist=astro or p_astro or real_astro,
            negotiation_officer=diplomat or p_diplomat or real_diplomat,
            model_armor_checkpoint=safety or p_safety or real_safety,
            edge_autopilot=gemma_edge_agent,
            watch_commander=watcher_agent,
            debate_moderator=debate_moderator or debate_moderator_agent,
            mission_memory=self.memory_bank,
            armor_inspector=self.armor,
            sub_agents=[],
        )

    def scripted_specialists(
        self,
        triage_factory: Callable[[], dict[str, Any]],
        astro_factory: Callable[[], dict[str, Any]],
        diplomat_factory: Callable[[], dict[str, Any]],
        safety_factory: Callable[[], dict[str, Any]],
    ) -> tuple[BaseAgent, BaseAgent, BaseAgent, BaseAgent]:
        """Convenience: four ScriptedAgents wired to the right output keys."""
        from agents.orchestrator import (
            NEGOTIATION_OUTPUT_KEY,
            SCREENING_OUTPUT_KEY,
            TRIAGE_OUTPUT_KEY,
            VERDICT_OUTPUT_KEY,
        )

        return (
            ScriptedAgent(name="alert_triage", output_key=TRIAGE_OUTPUT_KEY, payload_factory=triage_factory),
            ScriptedAgent(name="astrodynamics_specialist", output_key=SCREENING_OUTPUT_KEY, payload_factory=astro_factory),
            ScriptedAgent(name="negotiation_officer", output_key=NEGOTIATION_OUTPUT_KEY, payload_factory=diplomat_factory),
            ScriptedAgent(name="safety_officer", output_key=VERDICT_OUTPUT_KEY, payload_factory=safety_factory),
        )

    async def run_mission(self, pipeline: FleetCommanderPipeline, alert: dict[str, Any]) -> MissionOutcome:
        """Execute one mission exactly like the API layer does."""
        trace_id = uuid.uuid4().hex
        started = asyncio.get_event_loop().time()

        session = await self._session_service.create_session(
            app_name=APP_NAME, user_id="eval-runner", state={STATE_TRACE_ID: trace_id}
        )
        message = genai_types.Content(role="user", parts=[genai_types.Part(text=json.dumps({"alert": alert}))])
        async for _ in Runner(agent=pipeline, app_name=APP_NAME, session_service=self._session_service).run_async(
            user_id=session.user_id, session_id=session.id, new_message=message
        ):
            pass

        final_session = await self._session_service.get_session(app_name=APP_NAME, user_id=session.user_id, session_id=session.id)
        state = dict(final_session.state) if final_session else {}
        duration = asyncio.get_event_loop().time() - started
        return MissionOutcome(
            trace_id=str(state.get(STATE_TRACE_ID) or trace_id),
            final_status=str(state.get(STATE_FINAL_STATUS) or "UNKNOWN"),
            state=state,
            audit_events=audit_logger.get_events_by_trace(str(state.get(STATE_TRACE_ID) or trace_id)),
            duration_s=round(duration, 4),
        )

    # -- convenience assertions -------------------------------------------------

    @staticmethod
    def check(name: str, condition: bool, detail: str = "") -> CheckResult:
        return CheckResult(name=name, passed=bool(condition), detail=detail)

    @staticmethod
    def require(name: str, condition: bool, detail: str = "") -> CheckResult:
        if not condition:
            raise AssertionError(f"{name}: {detail or 'condition failed'}")
        return CheckResult(name=name, passed=True, detail=detail)

    async def satellite_fuel(self, sat_id: str) -> float:
        state = await self.memory_bank.get_satellite_state(sat_id)
        return float(state["fuel_percentage"])

    def conjunction_doc(self, sat_id: str, debris_id: str, tca_iso: str):
        cid = safe_document_id(f"{sat_id.upper()}-X-{debris_id.upper()}-TCA-{tca_iso}")
        return cid

    async def read_conjunction(self, conjunction_id: str) -> dict[str, Any] | None:
        return await self.memory_bank.get_conjunction_event(conjunction_id)

    # -- evolution helpers -------------------------------------------------------

    def fresh_evolution_engine(self, analyst_factory, critic_factory):
        """Scripted-specialist EvolutionEngine on this harness's memory bank."""
        from evolution.engine import EvolutionEngine, PROPOSAL_OUTPUT_KEY, VERDICT_OUTPUT_KEY

        return EvolutionEngine(
            name="evolution_engine_eval",
            learning_analyst=ScriptedAgent(
                name="learning_analyst", output_key=PROPOSAL_OUTPUT_KEY, payload_factory=analyst_factory
            ),
            meta_critic=ScriptedAgent(
                name="meta_critic", output_key=VERDICT_OUTPUT_KEY, payload_factory=critic_factory
            ),
            mission_memory=self.memory_bank,
            policy_store=self.policy_store,
            sub_agents=[],
        )

    async def run_evolution_cycle(self, engine, trigger: str = "manual") -> dict[str, Any]:
        """Run one evolution cycle through the real ADK Runner; return report."""
        from evolution.engine import STATE_REPORT_KEY

        session = await self._session_service.create_session(app_name=APP_NAME, user_id="eval-evolution", state={})
        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=json.dumps({"trigger_source": trigger}))],
        )
        async for _ in Runner(agent=engine, app_name=APP_NAME, session_service=self._session_service).run_async(
            user_id=session.user_id, session_id=session.id, new_message=message
        ):
            pass
        final_session = await self._session_service.get_session(
            app_name=APP_NAME, user_id=session.user_id, session_id=session.id
        )
        raw_report = (final_session.state if final_session else {}).get(STATE_REPORT_KEY, "")
        return json.loads(raw_report) if raw_report else {"status": "ENGINE_ERROR", "reasoning": "no report committed"}

    # -- debate helpers -----------------------------------------------------------

    def fresh_debate_moderator(self, fuel_factory, safety_factory, reassess_factory, judge_factory):
        """Scripted-strategist DebateModerator on this harness's memory bank."""
        from debate.judge import OUTPUT_KEY as JUDGE_KEY
        from debate.moderator import DEBATE_OUTCOME_STATE_KEY, DebateModerator

        return DebateModerator(
            name="debate_moderator_eval",
            fuel_minimizer=ScriptedAgent(name="fuel_minimizer", output_key="orbit_debate_fuel_minimizer", payload_factory=fuel_factory),
            safety_maximizer=ScriptedAgent(name="safety_maximizer", output_key="orbit_debate_safety_maximizer", payload_factory=safety_factory),
            reassess=ScriptedAgent(name="reassess", output_key="orbit_debate_reassess", payload_factory=reassess_factory),
            debate_judge=ScriptedAgent(name="debate_judge", output_key=JUDGE_KEY, payload_factory=judge_factory),
            mission_memory=self.memory_bank,
            policy_store=self.policy_store,
            sub_agents=[],
        )

    async def run_debate(
        self,
        moderator,
        screening: dict[str, Any],
        sat_id: str = "LANCASTER_ORBIT_1",
        debris_id: str = "FENGYUN_1C_DEB",
        trace_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Run one debate; returns (outcome_dict, trace_id)."""
        from debate.moderator import DEBATE_OUTCOME_STATE_KEY

        tid = trace_id or uuid.uuid4().hex
        state = {
            STATE_TRACE_ID: tid,
            "orbit_screening:parsed": screening,
            "orbit_debate_sat_id": sat_id,
            "orbit_debate_debris_id": debris_id,
        }
        session = await self._session_service.create_session(app_name=APP_NAME, user_id="eval-debate", state=state)
        message = genai_types.Content(role="user", parts=[genai_types.Part(text=json.dumps({"debate": True}))])
        async for _ in Runner(agent=moderator, app_name=APP_NAME, session_service=self._session_service).run_async(
            user_id=session.user_id, session_id=session.id, new_message=message
        ):
            pass
        final_session = await self._session_service.get_session(app_name=APP_NAME, user_id=session.user_id, session_id=session.id)
        raw = (final_session.state if final_session else {}).get(DEBATE_OUTCOME_STATE_KEY, "")
        return (json.loads(raw) if raw else None), tid
