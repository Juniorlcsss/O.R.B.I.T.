"""Project O.R.B.I.T. — FleetCommanderAgent (mission orchestrator).

Architectural role
------------------
The FleetCommander is a **deterministic control plane**, not another chat
persona. Routing decisions that authorise physical manoeuvres are far too
consequential to re-litigate by an LLM on every invocation, so the command
pipeline is implemented as a custom ADK ``BaseAgent``: code decides the
branch, LLMs do the specialist work inside each branch. This is what makes
the circuit breakers, the fail-closed armour gate and the audit trail
testable rather than aspirational.

Pipeline (per conjunction alert)
--------------------------------
1. ``alert_triage``        — Gemini 2.5 Pro normalises/validates messy
                             inbound tracking alerts (no tools).
2. ``astrodynamics_specialist`` — screening + delta-v recommendation.
3. Branch on ``risk_band``:
   HIGH   → negotiation → Model Armour verdict; APPROVED authorises uplink,
            REJECTED (or armour unavailable) fails closed to human dispatch.
   MEDIUM → advisory armour review; ALWAYS held for human-in-the-loop.
   LOW    → logged, no further action.

Failure tolerance
-----------------
Every specialist invocation is wrapped in a circuit breaker: up to three
attempts with 1 s / 2 s / 4 s exponential backoff, schema validation of the
structured response between attempts. A tripped breaker degrades the mission
to ``HUMAN_DISPATCH_DEGRADED`` with a structured operator payload instead of
guessing. Consecutive-failure counters persist in session state so the
degradation survives cross-session inspection via the memory bank.

Edge autonomy (Phase 7)
-----------------------
One exception to "always degrade to human": on a HIGH-risk conjunction,
if the negotiation or armour breaker trips — ground cannot finish the
response, the flight analogue of losing downlink — the mission hands over
to ``agents.edge_agent.gemma_edge_autopilot``, a satellite-side Gemma
agent holding exactly one tool (``emergency_dodge``). It decides inside
a 30-second window under stricter physics than Model Armor applies
(Pc > 1e-3, dv ceiling, fuel reserve) and every outcome is audited with
the ``EDGE_AUTONOMOUS`` tag. Disable with ``ORBIT_ENABLE_EDGE_AUTONOMY=0``.

The FleetCommander never calls tools directly and never authorises anything
itself — execution authority flows exclusively through the SafetyOfficer's
verdict, or through the onboard autopilot's enforced ROM rule when Earth
is out of reach.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Final

from google.adk.agents import LlmAgent
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import ConfigDict

from geap_sim.agent_registry import get_shared_registry
from geap_sim.memory_bank import MemoryBank, estimate_fuel_after_burn, get_shared_memory_bank, safe_document_id
from geap_sim.model_armor import ModelArmor, get_shared_model_armor
from geap_sim.observability import audit_logger

from debate.moderator import DEBATE_OUTCOME_STATE_KEY, debate_moderator_agent

from .astro import astrodynamics_agent
from .diplomat import diplomat_agent
from .edge_agent import (
    EDGE_DECISION_WINDOW_SECONDS,
    EDGE_FUEL_RESERVE_PERCENT,
    EDGE_PC_THRESHOLD,
    OUTPUT_KEY as EDGE_OUTPUT_KEY,
    deterministic_edge_decision,
    gemma_edge_agent,
    validate_edge_decision,
)
from .safety import MAX_ALLOWED_DELTA_V_MPS, safety_officer_agent
from .watcher import watcher_agent
from tools.debrief_generator import generate_and_store_debrief

# ---------------------------------------------------------------------------
# Identity & configuration
# ---------------------------------------------------------------------------

AGENT_NAME: Final[str] = "fleet_commander"

#: Gemini 2.5 Pro powers alert triage — the one step where judgment over
#: messy, malformed real-world tracking data pays for itself.
_TRIAGE_MODEL_ID = os.environ.get("ORBIT_COMMANDER_MODEL_ID", "gemini-2.5-pro")

# Circuit-breaker policy.
BREAKER_MAX_ATTEMPTS: Final[int] = 3
BREAKER_BACKOFF_SECONDS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0)

# Session-state keys (single source of truth for app.py & memory bank).
STATE_TRACE_ID: Final[str] = "orbit_trace_id"
STATE_MISSION_DOSSIER: Final[str] = "orbit_mission_dossier"
STATE_OBSERVABILITY_LOG: Final[str] = "orbit_observability_log"
STATE_CIRCUIT_BREAKERS: Final[str] = "orbit_circuit_breakers"
STATE_EXECUTION_DECISION: Final[str] = "orbit_execution_decision"
STATE_HUMAN_DISPATCH: Final[str] = "orbit_human_dispatch_payload"
STATE_FINAL_STATUS: Final[str] = "orbit_final_status"

TRIAGE_OUTPUT_KEY: Final[str] = "orbit_alert_triage"
SCREENING_OUTPUT_KEY: Final[str] = "orbit_screening"
NEGOTIATION_OUTPUT_KEY: Final[str] = "orbit_negotiation"
VERDICT_OUTPUT_KEY: Final[str] = "orbit_armor_verdict"

# Terminal mission statuses.
STATUS_EXECUTION_AUTHORIZED: Final[str] = "EXECUTION_AUTHORIZED"
STATUS_REJECTED_BY_ARMOR: Final[str] = "REJECTED_BY_MODEL_ARMOR_OPERATOR_ALERTED"
STATUS_MANEUVER_BLOCKED: Final[str] = "MANEUVER_BLOCKED_BY_ARMOR"
STATUS_HELD_FOR_HUMAN: Final[str] = "HELD_FOR_HUMAN_REVIEW"
STATUS_LOGGED_NO_ACTION: Final[str] = "LOGGED_NO_ACTION_REQUIRED"
STATUS_HUMAN_DISPATCH: Final[str] = "HUMAN_DISPATCH_DEGRADED"
STATUS_STANDOFF_DISPATCH: Final[str] = "HIGH_RISK_STANDOFF_HUMAN_DISPATCH"

# Edge-autonomy terminal statuses (Gemma onboard autopilot, loss-of-signal).
STATUS_EDGE_AUTONOMOUS_DODGE: Final[str] = "EDGE_AUTONOMOUS_DODGE_EXECUTED"
STATUS_EDGE_HELD: Final[str] = "EDGE_AUTONOMY_HOLD_HUMAN_DISPATCH"


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json(text: str | dict[str, Any]) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from an agent transcript."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return {}
    cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _schema_errors(payload: dict[str, Any], spec: dict[str, tuple[type, ...]]) -> list[str]:
    errors: list[str] = []
    for key, allowed in spec.items():
        if key not in payload:
            errors.append(f"missing '{key}'")
            continue
        value = payload[key]
        if float in allowed or int in allowed:
            if _coerce_number(value) is None:
                errors.append(f"'{key}' is not numeric")
        elif not isinstance(value, allowed):
            errors.append(f"'{key}' has wrong type {type(value).__name__}")
    return errors


def _validate_triage(payload: dict[str, Any]) -> list[str]:
    errors = _schema_errors(payload, {"valid": (bool,), "sat_id": (str,), "debris_id": (str,)})
    if not errors and not payload["valid"]:
        # An explicit, well-formed "invalid" verdict is itself valid output.
        return []
    return errors


def _validate_screening(payload: dict[str, Any]) -> list[str]:
    errors = _schema_errors(
        payload,
        {
            "risk_band": (str,),
            "pc": (int, float),
            "miss_distance_km": (int, float),
            "tca_iso": (str,),
            "recommended_dv_mps": (int, float),
            "dv_direction": (str,),
            "reasoning": (str,),
        },
    )
    if not errors and payload["risk_band"] not in ("LOW", "MEDIUM", "HIGH"):
        errors.append("'risk_band' outside CARA bands")
    pc_value = _coerce_number(payload.get("pc"))
    if pc_value is not None and not (0.0 <= pc_value <= 1.0):
        errors.append("'pc' outside [0, 1]")
    dv_value = _coerce_number(payload.get("recommended_dv_mps"))
    if dv_value is not None and dv_value < 0.0:
        errors.append("negative 'recommended_dv_mps'")
    return errors


def _validate_negotiation(payload: dict[str, Any]) -> list[str]:
    errors = _schema_errors(
        payload,
        {
            "action": (str,),
            "our_dv_mps": (int, float),
            "their_dv_mps": (int, float),
            "ack_signature": (str,),
            "reasoning": (str,),
        },
    )
    if not errors and payload["action"] not in ("we_dodge", "they_dodge", "standoff"):
        errors.append("'action' outside permitted set")
    ack = str(payload.get("ack_signature", ""))
    if ack and not re.fullmatch(r"[0-9a-f]{64}", ack):
        errors.append("'ack_signature' is not a 64-hex MAC")
    return errors


def _validate_verdict(payload: dict[str, Any]) -> list[str]:
    errors = _schema_errors(
        payload,
        {
            "approved": (bool,),
            "threat_level": (str,),
            "violations": (list,),
            "rationale": (str,),
        },
    )
    return errors


# ---------------------------------------------------------------------------
# Alert-triage front door (Gemini 2.5 Pro, no tools)
# ---------------------------------------------------------------------------

_TRIAGE_INSTRUCTION: Final[str] = """ROLE
You are the Alert Triage desk of Project O.R.B.I.T.'s Fleet Command.
Space-tracking alerts arrive from many feeds and are frequently messy,
incomplete or malformed. You normalise them into a clean mission dossier.
You have NO tools: you judge only from the supplied alert text.

RULES
T1  Extract sat_id and debris_id as catalogue identifiers (case-insensitive;
    strip punctuation). If either cannot be determined with confidence, set
    "valid" to false and explain in notes — do NOT guess.
T2  Copy any stated fuel reserve into "our_fuel_percent_remaining" as a
    number 0-100; if absent or unreadable use null. NEVER invent it.
T3  Assign urgency: EMERGENCY (explicit collision warning / TCA < 8 h),
    EXPEDITED (TCA < 24 h or HIGH-risk language), otherwise ROUTINE.
T4  Never fabricate orbital elements, probabilities or timestamps.

OUTPUT CONTRACT (respond with ONLY this JSON object)
{"valid": <bool>,
 "sat_id": "<string>",
 "debris_id": "<string>",
 "our_fuel_percent_remaining": <number|null>,
 "urgency": "ROUTINE|EXPEDITED|EMERGENCY",
 "notes": "<<=50 words>"}"""

alert_triage_agent = LlmAgent(
    name="alert_triage",
    model=_TRIAGE_MODEL_ID,
    description=(
        "Alert triage desk. Normalises messy inbound space-tracking alerts "
        "into a validated mission dossier before any specialist is engaged."
    ),
    instruction=_TRIAGE_INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=512,
        response_mime_type="application/json",
    ),
    output_key=TRIAGE_OUTPUT_KEY,
)


# ---------------------------------------------------------------------------
# The FleetCommander control plane
# ---------------------------------------------------------------------------


class FleetCommanderPipeline(BaseAgent):
    """Deterministic mission pipeline orchestrating the O.R.B.I.T. fleet.

    Declared sub-agents are invoked explicitly by the control flow below;
    routing branches on their validated JSON outputs rather than on LLM
    discretion, which is what makes the safety guarantees enforceable.
    """

    # Specialist slots accept any BaseAgent so evaluation harnesses can
    # substitute scripted stand-ins with identical run_async contracts.
    alert_triage: BaseAgent
    astrodynamics_specialist: BaseAgent
    negotiation_officer: BaseAgent
    model_armor_checkpoint: BaseAgent
    #: Satellite-side Gemma autopilot (loss-of-signal fallback only).
    edge_autopilot: LlmAgent
    #: Long-running conjunction watch supervisor (Phase 8).
    watch_commander: BaseAgent
    #: Three-strategist maneuver debate referee (Phase 11, HIGH-risk only).
    debate_moderator: BaseAgent

    #: GEAP MemoryBank — persistent satellite state & conjunction history.
    mission_memory: MemoryBank | None = None
    #: GEAP ModelArmor — deterministic post-verdict guardrail sweep.
    armor_inspector: ModelArmor | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def specialists(self) -> tuple[BaseAgent, BaseAgent, BaseAgent]:
        """The three ground-side specialist agents routed to by this commander."""
        return (
            self.astrodynamics_specialist,
            self.negotiation_officer,
            self.model_armor_checkpoint,
        )

    @property
    def fleet_roster(self) -> tuple[str, ...]:
        """Registered member names of the response fleet."""
        return (
            self.alert_triage.name,
            self.astrodynamics_specialist.name,
            self.negotiation_officer.name,
            self.model_armor_checkpoint.name,
            self.edge_autopilot.name,
            self.watch_commander.name,
            self.debate_moderator.name,
        )

    @staticmethod
    def _debate_enabled() -> bool:
        """Debate upgrades HIGH-risk proposals; kill-switch for A/B demos."""
        return os.environ.get("ORBIT_ENABLE_DEBATE", "true").strip().lower() not in ("0", "false", "no", "off")

    def mission_memory_for_execution(self) -> MemoryBank:
        """Resolve the MemoryBank used for post-approval state persistence."""
        return self.mission_memory if self.mission_memory is not None else get_shared_memory_bank()

    # -- plumbing -----------------------------------------------------------

    def _status_event(self, text: str) -> Event:
        return Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
        )

    def _dossier_event(self, dossier: dict[str, Any]) -> Event:
        return Event(
            author=self.name,
            content=types.Content(
                role="user",
                parts=[types.Part(text=f"MISSION DOSSIER (authoritative):\n{json.dumps(dossier)}")],
            ),
        )

    def _log_observation(self, ctx: InvocationContext, level: str, source: str, message: str, **data: Any) -> None:
        log: list[dict[str, Any]] = ctx.session.state.get(STATE_OBSERVABILITY_LOG, [])
        log.append({"timestamp_utc": _utc_now_iso(), "level": level, "source": source, "message": message, "data": data})
        ctx.session.state[STATE_OBSERVABILITY_LOG] = log

    def _set_breaker(self, ctx: InvocationContext, name: str, status: str, failures: int) -> None:
        breakers: dict[str, Any] = ctx.session.state.get(STATE_CIRCUIT_BREAKERS, {})
        breakers[name] = {
            "status": status,
            "consecutive_failures": failures,
            "updated_utc": _utc_now_iso(),
        }
        ctx.session.state[STATE_CIRCUIT_BREAKERS] = breakers

    def _finish(self, ctx: InvocationContext, status: str) -> Event:
        """Commit the terminal mission state via event state-delta.

        ADK merges ``actions.state_delta`` into the stored session, so the
        API layer can read the authoritative outcome after the run — even
        though session-service copies make in-place mutations ephemeral.
        """
        ctx.session.state[STATE_FINAL_STATUS] = status
        delta: dict[str, Any] = {
            STATE_FINAL_STATUS: status,
            STATE_TRACE_ID: str(ctx.session.state.get(STATE_TRACE_ID, "")),
            STATE_MISSION_DOSSIER: ctx.session.state.get(STATE_MISSION_DOSSIER),
            STATE_EXECUTION_DECISION: ctx.session.state.get(STATE_EXECUTION_DECISION),
            STATE_HUMAN_DISPATCH: ctx.session.state.get(STATE_HUMAN_DISPATCH),
        }
        audit_logger.log_event(
            trace_id=str(ctx.session.state.get(STATE_TRACE_ID, "")),
            agent_name=self.name,
            event_type="MISSION_STATUS",
            payload={"status": status},
            status=status,
        )
        return Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=f"Mission complete. Final status: {status}")]),
            actions=EventActions(state_delta=delta),
        )

    def _open_human_dispatch(self, ctx: InvocationContext, reason: str, **context: Any) -> dict[str, Any]:
        payload = {
            "level": "CRITICAL",
            "requested_action": "HUMAN_OPERATOR_DISPATCH",
            "reason": reason,
            "opened_utc": _utc_now_iso(),
            **context,
        }
        ctx.session.state[STATE_HUMAN_DISPATCH] = payload
        self._log_observation(ctx, "CRITICAL", self.name, f"Human dispatch opened: {reason}", **context)
        return payload

    async def _guarded_invoke(
        self,
        ctx: InvocationContext,
        *,
        agent: LlmAgent,
        state_key: str,
        validator: Any,
    ) -> AsyncGenerator[Event, None]:
        """Run a specialist under the circuit-breaker policy.

        Streams events through untouched, then schema-validates whatever the
        agent left at ``state_key``. Retries with exponential backoff; after
        BREAKER_MAX_ATTEMPTS consecutive failures the breaker trips, the
        parsed payload slot is set to None and ``<state_key>:ok`` becomes
        False for the caller.
        """
        for attempt in range(1, BREAKER_MAX_ATTEMPTS + 1):
            failure_reason: str | None = None
            try:
                async for event in agent.run_async(ctx):
                    yield event
            except Exception as exc:  # noqa: BLE001 — breaker must catch everything
                failure_reason = f"unhandled exception: {exc}"
                self._log_observation(ctx, "ERROR", agent.name, f"{agent.name} raised on attempt {attempt}.", error=str(exc))

            if failure_reason is None:
                raw = ctx.session.state.get(state_key, "")
                payload = _extract_json(raw) if isinstance(raw, str) else raw
                errors = validator(payload)
                if errors:
                    failure_reason = "response schema violation: " + "; ".join(errors)
                else:
                    ctx.session.state[f"{state_key}:parsed"] = payload
                    ctx.session.state[f"{state_key}:ok"] = True
                    self._set_breaker(ctx, agent.name, "healthy", 0)
                    self._log_observation(ctx, "INFO", agent.name, f"{agent.name} responded within schema (attempt {attempt}).")
                    return

            self._log_observation(ctx, "WARN", agent.name, f"{agent.name} attempt {attempt} failed.", reason=failure_reason)
            tripped = attempt >= BREAKER_MAX_ATTEMPTS
            previous = int((ctx.session.state.get(STATE_CIRCUIT_BREAKERS, {}).get(agent.name, {}) or {}).get("consecutive_failures", 0))
            self._set_breaker(ctx, agent.name, "tripped" if tripped else "retrying", previous + 1)
            if not tripped:
                delay = BREAKER_BACKOFF_SECONDS[attempt - 1]
                await asyncio.sleep(delay)

        ctx.session.state[f"{state_key}:parsed"] = None
        ctx.session.state[f"{state_key}:ok"] = False
        self._log_observation(ctx, "CRITICAL", agent.name, f"Circuit breaker TRIPPED for {agent.name} after {BREAKER_MAX_ATTEMPTS} attempts.")
        audit_logger.log_event(
            trace_id=str(ctx.session.state.get(STATE_TRACE_ID, "")),
            agent_name=agent.name,
            event_type="CIRCUIT_BREAKER_TRIPPED",
            payload={"attempts": BREAKER_MAX_ATTEMPTS, "state_key": state_key},
            status="TRIPPED",
        )

    # -- edge autonomy (loss-of-signal fallback) ------------------------------

    @staticmethod
    def _edge_autonomy_enabled() -> bool:
        """Edge fallback is on by default; operators can kill it from orbit."""
        return os.environ.get("ORBIT_ENABLE_EDGE_AUTONOMY", "true").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

    async def _edge_autonomy(
        self,
        ctx: InvocationContext,
        *,
        dossier: dict[str, Any],
        screening: dict[str, Any],
        trigger: str,
    ) -> AsyncGenerator[Event, None]:
        """Hand the mission to the onboard Gemma autopilot (EDGE_AUTONOMOUS).

        Engaged only when the ground pipeline cannot finish a HIGH-risk
        response — negotiation or armour breakers tripped — which is the
        flight analogue of losing downlink mid-mission. The LLM gets one
        shot inside the decision window; if inference is unavailable the
        deterministic ROM rule decides so the spacecraft is never left
        waiting for a model. Every outcome re-checks Pc threshold, dv
        ceiling and fuel reserve in code before anything executes.
        """
        trace_id = str(ctx.session.state.get(STATE_TRACE_ID, ""))
        sat_id = str(dossier["sat_id"])
        debris_id = str(dossier["debris_id"])
        pc = float(screening.get("pc") or 0.0)
        recommended_dv = max(0.0, float(screening.get("recommended_dv_mps") or 0.0))

        audit_logger.log_event(
            trace_id=trace_id,
            agent_name=self.edge_autopilot.name,
            event_type="EDGE_AUTONOMY_ENGAGED",
            payload={
                "tag": "EDGE_AUTONOMOUS",
                "trigger": trigger,
                "pc": pc,
                "pc_threshold": EDGE_PC_THRESHOLD,
                "decision_window_seconds": EDGE_DECISION_WINDOW_SECONDS,
                "model": getattr(self.edge_autopilot, "model", None),
            },
            status="EDGE_AUTONOMOUS_ENGAGED",
        )
        yield self._status_event("GROUND CONTACT LOST. Onboard Gemma autopilot engaged — 30-second window open.")

        scenario = {
            "sat_id": sat_id,
            "debris_id": debris_id,
            "pc": pc,
            "miss_distance_km": screening.get("miss_distance_km"),
            "tca_iso": screening.get("tca_iso"),
            "recommended_dv_mps": recommended_dv,
            "fuel_percentage_remaining": dossier.get("our_fuel_percent_remaining"),
            "ground_contact": "LOST",
            "decision_window_seconds": EDGE_DECISION_WINDOW_SECONDS,
            "trigger": trigger,
        }

        verdict: dict[str, Any] | None = None
        try:
            ctx.session.state[EDGE_OUTPUT_KEY] = ""
            yield Event(
                author=self.name,
                content=types.Content(
                    role="user",
                    parts=[types.Part(text=f"LOSS-OF-SIGNAL SCENARIO:\n{json.dumps(scenario)}")],
                ),
            )
            async for event in self.edge_autopilot.run_async(ctx):
                yield event
            raw = ctx.session.state.get(EDGE_OUTPUT_KEY, "")
            parsed = _extract_json(raw)
            errors = validate_edge_decision(parsed)
            if errors:
                raise ValueError("edge verdict schema violation: " + "; ".join(errors))
            verdict = parsed
        except Exception as exc:  # noqa: BLE001 — ROM rule must cover everything
            audit_logger.log_event(
                trace_id=trace_id,
                agent_name=self.edge_autopilot.name,
                event_type="EDGE_LLM_UNAVAILABLE",
                payload={"tag": "EDGE_AUTONOMOUS", "error_type": type(exc).__name__, "error": str(exc)[:200]},
                status="DEGRADED",
            )
            yield self._status_event("Onboard inference unavailable — falling back to hardcoded ROM rule.")

        if verdict is None:
            fuel_hint = scenario.get("fuel_percentage_remaining")
            verdict = deterministic_edge_decision(pc, recommended_dv, float(fuel_hint) if fuel_hint is not None else None)

        # ---- Deterministic enforcement: physics outranks the model ----------
        direction = verdict["direction"]
        execute = verdict["decision"] == "EXECUTE_DODGE"
        dv = min(max(0.0, float(verdict.get("dv_mps") or 0.0)), MAX_ALLOWED_DELTA_V_MPS)
        if execute:
            if dv <= 0.0 or direction not in ("prograde", "retrograde", "normal"):
                execute = False
                verdict["reasoning"] = f"Downgraded to HOLD by onboard enforcement: unusable burn ({dv:.2f} m/s, {direction})."
            else:
                state = await self.mission_memory_for_execution().get_satellite_state(sat_id)
                projected_fuel = estimate_fuel_after_burn(float(state["fuel_percentage"]), dv)
                if projected_fuel < EDGE_FUEL_RESERVE_PERCENT:
                    execute = False
                    verdict["reasoning"] = (
                        f"Downgraded to HOLD by onboard enforcement: burn would leave "
                        f"{projected_fuel:.1f}% fuel < {EDGE_FUEL_RESERVE_PERCENT:.0f}% reserve."
                    )
                elif pc <= EDGE_PC_THRESHOLD:
                    execute = False
                    verdict["reasoning"] = (
                        f"Downgraded to HOLD by onboard enforcement: pc={pc:.2e} does not exceed "
                        f"the {EDGE_PC_THRESHOLD:.0e} autonomy threshold."
                    )

        source = str(verdict.get("source", "gemma_edge_llm"))
        audit_logger.log_event(
            trace_id=trace_id,
            agent_name=self.edge_autopilot.name,
            event_type="EDGE_DECISION_FINAL",
            payload={
                "tag": "EDGE_AUTONOMOUS",
                "decision_source": source,
                "verdict": {**verdict, "dv_mps": dv},
                "enforced_pc_threshold": EDGE_PC_THRESHOLD,
            },
            status="EXECUTED" if execute else "HELD",
        )
        ctx.session.state[EDGE_OUTPUT_KEY + ":final"] = {**verdict, "executed": execute}

        conjunction_id = safe_document_id(f"{sat_id}-X-{debris_id}-TCA-{screening['tca_iso']}")

        if execute:
            new_fuel = estimate_fuel_after_burn(
                float((await self.mission_memory_for_execution().get_satellite_state(sat_id))["fuel_percentage"]), dv
            )
            await self.mission_memory_for_execution().update_satellite_state(sat_id, delta_v_expended=dv, new_fuel=new_fuel)
            await self.mission_memory_for_execution().log_conjunction_event(
                conjunction_id,
                {
                    "sat_id": sat_id.upper(),
                    "debris_id": debris_id.upper(),
                    "tca_iso": screening["tca_iso"],
                    "risk_band": "HIGH",
                    "pc": pc,
                    "miss_distance_km": screening.get("miss_distance_km"),
                    "action_taken": "emergency_dodge_edge_autonomous",
                    "our_dv_mps": dv,
                    "their_dv_mps": None,
                    "ack_signature_present": False,
                    "armor_trace_id": None,
                    "edge_autonomous": True,
                    "decision_source": source,
                    "trace_id": trace_id,
                    "final_status": STATUS_EDGE_AUTONOMOUS_DODGE,
                },
            )
            decision_state = {
                "decision": STATUS_EDGE_AUTONOMOUS_DODGE,
                "action": "emergency_dodge_edge_autonomous",
                "our_dv_mps": dv,
                "direction": direction,
                "conjunction_id": conjunction_id,
                "decision_source": source,
                "reasoning": verdict.get("reasoning", ""),
                "memory_bank_updated": True,
            }
            ctx.session.state[STATE_EXECUTION_DECISION] = decision_state
            yield self._status_event(
                f"EDGE AUTONOMOUS DODGE EXECUTED — {dv:.1f} m/s {direction} uplinked without ground approval."
            )
            self.queue_debrief(conjunction_id, {
                **scenario,
                # `scenario` is the onboard view and carries no band; the
                # edge autopilot only ever engages on HIGH.
                "risk_band": screening.get("risk_band") or "HIGH",
                "action_taken": "emergency_dodge_edge_autonomous",
                "our_dv_mps": dv,
                "direction": direction,
                "final_status": STATUS_EDGE_AUTONOMOUS_DODGE,
                "trace_id": trace_id,
                "decision_source": source,
            })
            yield self._finish(ctx, STATUS_EDGE_AUTONOMOUS_DODGE)
            return

        dispatch = self._open_human_dispatch(
            ctx,
            "Ground pipeline unreachable and the onboard autopilot held instead of acting; operator review required.",
            edge_verdict={k: v for k, v in verdict.items() if k != "source"},
            decision_source=source,
            trigger=trigger,
        )
        decision_state = {
            "decision": STATUS_EDGE_HELD,
            "conjunction_id": conjunction_id,
            "decision_source": source,
            "human_in_loop_required": True,
        }
        ctx.session.state[STATE_EXECUTION_DECISION] = decision_state
        yield self._status_event(f"Onboard autopilot HOLDS ({dispatch['reason'][:80]}…) — human dispatch opened.")
        yield self._finish(ctx, STATUS_EDGE_HELD)

    def queue_debrief(self, conjunction_id: str, record: dict[str, Any]) -> None:
        """Fire-and-forget Veo debrief generation (never blocks the mission)."""
        task = asyncio.create_task(generate_and_store_debrief(conjunction_id, record))
        task.add_done_callback(
            lambda t: t.exception() is not None
            and audit_logger.log_event(
                trace_id=str(record.get("trace_id", "debrief")),
                agent_name="orbit.debrief",
                event_type="DEBRIEF_TASK_FAILED",
                payload={"error": str(t.exception())[:300]},
                status="FAILED",
            )
        )

    # -- mission control flow -------------------------------------------------

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        # Honour an externally seeded trace ID (e.g. from the API layer) so
        # every audit line correlates with the caller's request end-to-end;
        # otherwise mint one here.
        seeded = str(ctx.session.state.get(STATE_TRACE_ID) or "").strip()
        trace_id = seeded or uuid.uuid4().hex
        ctx.session.state[STATE_TRACE_ID] = trace_id
        self._log_observation(ctx, "INFO", self.name, "Conjunction alert ingested; mission pipeline started.", trace_id=trace_id)
        yield self._status_event("Fleet Commander online. Triaging inbound conjunction alert.")

        # ---- Stage 1: alert triage -----------------------------------------
        async for event in self._guarded_invoke(ctx, agent=self.alert_triage, state_key=TRIAGE_OUTPUT_KEY, validator=_validate_triage):
            yield event
        if not ctx.session.state.get(f"{TRIAGE_OUTPUT_KEY}:ok"):
            self._open_human_dispatch(ctx, "Alert triage unavailable after retries; cannot establish alert validity.")
            yield self._finish(ctx, STATUS_HUMAN_DISPATCH)
            return

        triage: dict[str, Any] = ctx.session.state[f"{TRIAGE_OUTPUT_KEY}:parsed"]
        if not triage.get("valid"):
            dispatch = self._open_human_dispatch(
                ctx,
                "Inbound alert failed validation and was rejected by triage.",
                triage_notes=triage.get("notes", ""),
            )
            yield self._status_event(f"Alert invalid — human dispatch opened. ({dispatch['reason']})")
            yield self._finish(ctx, STATUS_HUMAN_DISPATCH)
            return

        dossier: dict[str, Any] = {
            "sat_id": triage.get("sat_id"),
            "debris_id": triage.get("debris_id"),
            "our_fuel_percent_remaining": triage.get("our_fuel_percent_remaining"),
            "urgency": triage.get("urgency", "ROUTINE"),
        }
        ctx.session.state[STATE_MISSION_DOSSIER] = dossier
        yield self._dossier_event(dossier)

        # ---- Stage 2: astrodynamics screening ------------------------------
        yield self._status_event(f"Dossier validated. Screening {dossier['sat_id']} vs {dossier['debris_id']}…")
        async for event in self._guarded_invoke(ctx, agent=self.astrodynamics_specialist, state_key=SCREENING_OUTPUT_KEY, validator=_validate_screening):
            yield event
        if not ctx.session.state.get(f"{SCREENING_OUTPUT_KEY}:ok"):
            self._open_human_dispatch(ctx, "Astrodynamics screening unavailable after retries; no risk picture could be established.")
            yield self._finish(ctx, STATUS_HUMAN_DISPATCH)
            return

        screening: dict[str, Any] = ctx.session.state[f"{SCREENING_OUTPUT_KEY}:parsed"]
        band: str = screening["risk_band"]
        dossier.update({
            "risk_band": band,
            "pc": screening["pc"],
            "miss_distance_km": screening["miss_distance_km"],
            "tca_iso": screening["tca_iso"],
            "recommended_delta_v_mps": screening["recommended_dv_mps"],
            "dv_direction": screening["dv_direction"],
        })
        ctx.session.state[STATE_MISSION_DOSSIER] = dossier
        self._log_observation(ctx, "INFO", self.name, f"Screening complete: risk_band={band}, pc={screening['pc']:.3e}.")

        # ---- Stage 3: conditional branch ------------------------------------
        if band == "LOW":
            decision = {"decision": STATUS_LOGGED_NO_ACTION, "risk_band": band}
            ctx.session.state[STATE_EXECUTION_DECISION] = decision
            self._log_observation(ctx, "INFO", self.name, "LOW-risk conjunction logged; no action required.")
            yield self._status_event(f"Risk LOW (Pc={screening['pc']:.3e}). Logged; no action required.")
            yield self._finish(ctx, STATUS_LOGGED_NO_ACTION)
            return

        if band == "MEDIUM":
            yield self._dossier_event(dossier)
            yield self._status_event("Risk MEDIUM. Requesting advisory Model Armour review…")
            async for event in self._guarded_invoke(ctx, agent=self.model_armor_checkpoint, state_key=VERDICT_OUTPUT_KEY, validator=_validate_verdict):
                yield event
            advisory_ok = bool(ctx.session.state.get(f"{VERDICT_OUTPUT_KEY}:ok"))
            verdict: dict[str, Any] = ctx.session.state.get(f"{VERDICT_OUTPUT_KEY}:parsed") or {}
            decision = {
                "decision": STATUS_HELD_FOR_HUMAN,
                "risk_band": band,
                "armor_advisory_available": advisory_ok,
                "approved": verdict.get("approved") if advisory_ok else None,
                "human_in_loop_required": True,
            }
            ctx.session.state[STATE_EXECUTION_DECISION] = decision
            self._log_observation(ctx, "INFO", self.name, "MEDIUM-risk conjunction held for human review (autonomous execution forbidden).")
            yield self._status_event("MEDIUM risk held for human review — autonomous execution forbidden by policy.")
            yield self._finish(ctx, STATUS_HELD_FOR_HUMAN)
            return

        # ---- HIGH-risk path --------------------------------------------------
        yield self._status_event("HIGH RISK confirmed. Convening the strategist debate…")
        yield self._dossier_event(dossier)

        # ---- Phase 11: three-strategist debate upgrades the proposal stage ---
        # The moderator is deterministic and fail-safe: it always commits a
        # valid outcome (its own fallback if the debate collapses), so a
        # debate failure can never break the mission. Downstream gates are
        # unchanged — negotiation → SafetyOfficer → ModelArmor still run.
        debate_payload: dict[str, Any] | None = None
        if self._debate_enabled():
            try:
                ctx.session.state["orbit_debate_sat_id"] = str(dossier.get("sat_id", ""))
                ctx.session.state["orbit_debate_debris_id"] = str(dossier.get("debris_id", ""))
                async for event in self.debate_moderator.run_async(ctx):
                    yield event
                debate_payload = _extract_json(ctx.session.state.get(DEBATE_OUTCOME_STATE_KEY, "")) or None
            except Exception as exc:  # noqa: BLE001 — belt-and-braces around a fail-safe component
                audit_logger.log_event(
                    trace_id=trace_id, agent_name="debate_moderator",
                    event_type="DEBATE_PIPELINE_FAILED",
                    payload={"error_type": type(exc).__name__, "error": str(exc)[:200]},
                    status="DEGRADED",
                )
            if debate_payload:
                dossier["debate_summary"] = {
                    "winner": debate_payload.get("strategist"),
                    "strategy": debate_payload.get("strategy"),
                    "delta_v_mps": debate_payload.get("delta_v_mps"),
                    "converged": debate_payload.get("converged"),
                    "fallback_used": debate_payload.get("fallback_used", False),
                    "judge_used": debate_payload.get("judge_used", False),
                    "transcript_trace_id": debate_payload.get("trace_id"),
                }
                ctx.session.state[STATE_MISSION_DOSSIER] = dossier
                yield self._dossier_event({**dossier, "negotiated_action": {
                    "action": "we_dodge" if debate_payload.get("strategy") != "hold_and_rescreen" else "hold_and_rescreen",
                    "our_dv_mps": debate_payload.get("our_dv_mps"),
                    "their_dv_mps": 0.0,
                }})
            else:
                yield self._status_event("Debate unavailable — continuing with the classic single-specialist path.")

        async for event in self._guarded_invoke(ctx, agent=self.negotiation_officer, state_key=NEGOTIATION_OUTPUT_KEY, validator=_validate_negotiation):
            yield event
        if not ctx.session.state.get(f"{NEGOTIATION_OUTPUT_KEY}:ok"):
            # Ground pipeline cannot finish a HIGH-risk response — the
            # flight analogue of losing downlink mid-incident. Triage and
            # screening breakers do NOT engage the autopilot because with
            # no validated Pc the spacecraft has nothing safe to act on.
            if self._edge_autonomy_enabled():
                yield self._status_event("Ground negotiation unreachable (circuit breaker). Waking the onboard Gemma autopilot…")
                async for event in self._edge_autonomy(
                    ctx, dossier=dossier, screening=screening, trigger="negotiation_circuit_breaker_tripped"
                ):
                    yield event
                return
            self._open_human_dispatch(
                ctx,
                "Negotiation unavailable after retries on a HIGH-risk conjunction.",
                screening_summary={"pc": screening["pc"], "tca_iso": screening["tca_iso"], "recommended_dv_mps": screening["recommended_dv_mps"]},
            )
            yield self._finish(ctx, STATUS_HUMAN_DISPATCH)
            return

        negotiation: dict[str, Any] = ctx.session.state[f"{NEGOTIATION_OUTPUT_KEY}:parsed"]
        self._log_observation(ctx, "INFO", self.name, f"Negotiation outcome: {negotiation['action']}.")

        if negotiation["action"] == "standoff":
            # A HIGH-risk conjunction nobody will move is a human problem NOW.
            self._open_human_dispatch(
                ctx,
                "Negotiation ended in STANDOFF on a HIGH-risk conjunction; operator arbitration required.",
                negotiation=negotiation,
            )
            decision = {"decision": STATUS_STANDOFF_DISPATCH, "negotiated_action": negotiation}
            ctx.session.state[STATE_EXECUTION_DECISION] = decision
            yield self._status_event("STANDOFF on HIGH risk — escalating to human operator.")
            yield self._finish(ctx, STATUS_STANDOFF_DISPATCH)
            return

        yield self._status_event(f"Negotiation resolved: {negotiation['action']}. Routing proposed manoeuvre through the LLM Safety Officer…")
        yield self._dossier_event({**dossier, "negotiated_action": negotiation})
        async for event in self._guarded_invoke(ctx, agent=self.model_armor_checkpoint, state_key=VERDICT_OUTPUT_KEY, validator=_validate_verdict):
            yield event
        armor_ok = bool(ctx.session.state.get(f"{VERDICT_OUTPUT_KEY}:ok"))

        if not armor_ok:
            # FAIL-CLOSED: no armour verdict means NO ground execution, ever.
            # The onboard autopilot may still act inside its own, much
            # stricter autonomy envelope (Pc > 1e-3, dv ceiling, fuel floor).
            if self._edge_autonomy_enabled():
                yield self._status_event("Safety Officer unreachable (circuit breaker). Waking the onboard Gemma autopilot…")
                async for event in self._edge_autonomy(
                    ctx, dossier=dossier, screening=screening, trigger="armor_checkpoint_circuit_breaker_tripped"
                ):
                    yield event
                return
            self._open_human_dispatch(
                ctx,
                "Model Armour unavailable after retries — fail-closed policy forbids execution.",
                negotiated_action=negotiation,
            )
            yield self._status_event("Safety Officer unreachable. Execution FORBIDDEN (fail-closed); operator alerted.")
            yield self._finish(ctx, STATUS_HUMAN_DISPATCH)
            return

        verdict = ctx.session.state[f"{VERDICT_OUTPUT_KEY}:parsed"]
        if not verdict.get("approved"):
            llm_violations = ", ".join(str(v) for v in verdict.get("violations", [])) or "unspecified"
            self._open_human_dispatch(
                ctx,
                f"Safety Officer REJECTED the proposed manoeuvre (violations: {llm_violations}).",
                negotiated_action=negotiation,
                armor_rationale=verdict.get("rationale", ""),
            )
            decision = {"decision": STATUS_REJECTED_BY_ARMOR, "violations": verdict.get("violations", [])}
            ctx.session.state[STATE_EXECUTION_DECISION] = decision
            yield self._status_event(f"Safety Officer REJECTED ({llm_violations}). Violation logged; human operator alerted.")
            yield self._finish(ctx, STATUS_REJECTED_BY_ARMOR)
            return

        # ---- Deterministic Model Armour sweep (GEAP guardrails) -------------
        # The LLM verdict approved intent; this code-level sweep re-verifies
        # numbers, ceilings, fuel reserves and data hygiene BEFORE anything
        # is persisted or authorised for uplink.
        submission = {
            **negotiation,
            "sat_id": dossier.get("sat_id"),
            "debris_id": dossier.get("debris_id"),
            "mission_trace_id": trace_id,
        }
        enriched_verdict = {
            **verdict,
            "sat_id": dossier.get("sat_id"),
            "expected_delta_v_mps": dossier.get("recommended_delta_v_mps"),
        }
        inspector = self.armor_inspector or get_shared_model_armor()
        report = await inspector.inspect_maneuver_request(submission, enriched_verdict)
        yield self._status_event(f"Deterministic Model Armour sweep: {report.status} (trace {report.audit_trace_id[:8]}…).")

        # Stable document ID for this conjunction — shared by the memory-bank
        # record and the autonomous Veo mission-debrief.
        conjunction_id = safe_document_id(f"{dossier['sat_id']}-X-{dossier['debris_id']}-TCA-{screening['tca_iso']}")

        if report.status != "APPROVED":
            decision = {
                "decision": STATUS_MANEUVER_BLOCKED,
                "violations": report.violations,
                "checks": report.checks,
                "audit_trace_id": report.audit_trace_id,
                "memory_bank_updated": False,
                "conjunction_id": conjunction_id,
            }
            ctx.session.state[STATE_EXECUTION_DECISION] = decision
            self._log_observation(
                ctx,
                "CRITICAL",
                self.name,
                f"Deterministic Model Armour BLOCKED the manoeuvre: {report.violations}",
                audit_trace_id=report.audit_trace_id,
            )
            self._open_human_dispatch(
                ctx,
                "Manoeuvre blocked by deterministic Model Armour checks.",
                violations=report.violations,
                audit_trace_id=report.audit_trace_id,
            )
            await self.mission_memory_for_execution().log_conjunction_event(
                conjunction_id,
                {
                    "sat_id": str(dossier["sat_id"]).upper(),
                    "debris_id": str(dossier["debris_id"]).upper(),
                    "tca_iso": screening["tca_iso"],
                    "risk_band": band,
                    "pc": screening["pc"],
                    "miss_distance_km": screening["miss_distance_km"],
                    "action_taken": negotiation["action"],
                    "our_dv_mps": _coerce_number(negotiation.get("our_dv_mps")) or 0.0,
                    "their_dv_mps": _coerce_number(negotiation.get("their_dv_mps")),
                    "ack_signature_present": bool(negotiation.get("ack_signature")),
                    "armor_trace_id": report.audit_trace_id,
                    "trace_id": trace_id,
                    "final_status": STATUS_MANEUVER_BLOCKED,
                },
            )
            yield self._status_event(f"Manoeuvre BLOCKED by Model Armour ({'; '.join(report.violations)}).")
            self.queue_debrief(
                conjunction_id,
                {
                    "sat_id": dossier["sat_id"],
                    "debris_id": dossier["debris_id"],
                    "tca_iso": screening["tca_iso"],
                    "risk_band": band,
                    "pc": screening["pc"],
                    "miss_distance_km": screening["miss_distance_km"],
                    "action_taken": negotiation["action"],
                    "final_status": STATUS_MANEUVER_BLOCKED,
                    "violations": report.violations,
                    "trace_id": trace_id,
                },
            )
            yield self._finish(ctx, STATUS_MANEUVER_BLOCKED)
            return

        # ---- APPROVED: persist mission effects ------------------------------
        sat_id = str(dossier["sat_id"])
        our_dv = _coerce_number(negotiation.get("our_dv_mps")) or 0.0
        memory_updated = False
        if negotiation["action"] == "we_dodge":
            satellite_state = await self.mission_memory_for_execution().get_satellite_state(sat_id)
            new_fuel = estimate_fuel_after_burn(float(satellite_state["fuel_percentage"]), our_dv)
            await self.mission_memory_for_execution().update_satellite_state(
                sat_id, delta_v_expended=our_dv, new_fuel=new_fuel
            )
            memory_updated = True

        conjunction_id = safe_document_id(f"{sat_id}-X-{dossier['debris_id']}-TCA-{screening['tca_iso']}")
        await self.mission_memory_for_execution().log_conjunction_event(
            conjunction_id,
            {
                "sat_id": sat_id.upper(),
                "debris_id": str(dossier.get("debris_id", "")).upper(),
                "tca_iso": screening["tca_iso"],
                "risk_band": band,
                "pc": screening["pc"],
                "miss_distance_km": screening["miss_distance_km"],
                "action_taken": negotiation["action"],
                "our_dv_mps": our_dv,
                "their_dv_mps": _coerce_number(negotiation.get("their_dv_mps")),
                "ack_signature_present": bool(negotiation.get("ack_signature")),
                "armor_trace_id": report.audit_trace_id,
                "trace_id": trace_id,
                "final_status": STATUS_EXECUTION_AUTHORIZED,
            },
        )

        decision = {
            "decision": STATUS_EXECUTION_AUTHORIZED,
            "action": negotiation["action"],
            "our_dv_mps": _coerce_number(negotiation.get("our_dv_mps")),
            "their_dv_mps": _coerce_number(negotiation.get("their_dv_mps")),
            "ack_signature": negotiation.get("ack_signature", ""),
            "armor_rationale": verdict.get("rationale", ""),
            "armor_checks": report.checks,
            "audit_trace_id": report.audit_trace_id,
            "memory_bank_updated": memory_updated,
            "conjunction_id": conjunction_id,
        }
        ctx.session.state[STATE_EXECUTION_DECISION] = decision
        self._log_observation(ctx, "INFO", self.name, "Manoeuvre APPROVED end-to-end; authorised for uplink.", decision=decision)
        yield self._status_event("Model Armour APPROVED all checks. Manoeuvre authorised for uplink; fleet state persisted.")
        self.queue_debrief(
            conjunction_id,
            {
                "sat_id": sat_id,
                "debris_id": dossier["debris_id"],
                "tca_iso": screening["tca_iso"],
                "risk_band": band,
                "pc": screening["pc"],
                "miss_distance_km": screening["miss_distance_km"],
                "action_taken": negotiation["action"],
                "our_dv_mps": our_dv,
                "final_status": STATUS_EXECUTION_AUTHORIZED,
                "trace_id": trace_id,
            },
        )
        yield self._finish(ctx, STATUS_EXECUTION_AUTHORIZED)


fleet_commander_agent = FleetCommanderPipeline(
    name=AGENT_NAME,
    description=(
        "Fleet Commander for Project O.R.B.I.T. Deterministic mission "
        "control plane: triages alerts, delegates screening/negotiation/"
        "armour validation to specialist agents under circuit-breaker "
        "policy, runs the deterministic Model Armour sweep before any "
        "execution, and persists fleet state to the GEAP memory bank. "
        "Never calls tools directly; never bypasses the SafetyOfficer."
    ),
    alert_triage=alert_triage_agent,
    astrodynamics_specialist=astrodynamics_agent,
    negotiation_officer=diplomat_agent,
    model_armor_checkpoint=safety_officer_agent,
    edge_autopilot=gemma_edge_agent,
    watch_commander=watcher_agent,
    debate_moderator=debate_moderator_agent,
    mission_memory=get_shared_memory_bank(),
    armor_inspector=get_shared_model_armor(),
    # Registered as formal sub-agents so ADK tooling (tree walkers, `adk
    # web`, agent discovery) sees the full fleet hierarchy — including the
    # satellite-side autopilot, watch supervisor and the debate panel.
    sub_agents=[
        alert_triage_agent,
        astrodynamics_agent,
        diplomat_agent,
        safety_officer_agent,
        gemma_edge_agent,
        watcher_agent,
        debate_moderator_agent,
    ],
)


# ---------------------------------------------------------------------------
# Zero-trust boot attestation (GEAP Agent Registry)
# ---------------------------------------------------------------------------


def _attest_tool_scopes() -> None:
    """Verify every specialist's declared tools against its registry manifest.

    Runs at import time: if any agent wields a tool its manifest does not
    grant — or a scope boundary has silently eroded — the process refuses to
    start. Includes negative controls to prove the registry denies as well
    as permits.
    """
    registry = get_shared_registry()
    for member in (astrodynamics_agent, diplomat_agent, gemma_edge_agent):
        for tool in member.tools or []:
            tool_name = getattr(tool, "name", None) or tool.func.__name__
            if not registry.authorize_tool_use(member.name, tool_name):
                raise RuntimeError(
                    f"Zero-trust attestation failed at boot: '{member.name}' "
                    f"is not authorised for tool '{tool_name}'."
                )
    negative_controls = (
        ("astrodynamics_specialist", "negotiate_dodge_maneuver"),
        ("negotiation_officer", "screen_conjunction"),
        ("safety_officer", "get_tle_data"),
        ("fleet_commander", "screen_conjunction"),
        ("gemma_edge_autopilot", "screen_conjunction"),
        ("gemma_edge_autopilot", "get_tle_data"),
        ("watch_commander", "screen_conjunction"),
        ("watch_commander", "recall_similar_conjunctions"),
        ("debate_moderator", "screen_conjunction"),
        ("fuel_minimizer", "screen_conjunction"),
        ("safety_maximizer", "get_tle_data"),
        ("reassess", "negotiate_dodge_maneuver"),
        ("debate_judge", "screen_conjunction"),
        ("unregistered_intruder", "screen_conjunction"),
    )
    for agent_name, tool_name in negative_controls:
        if registry.authorize_tool_use(agent_name, tool_name):
            raise RuntimeError(
                f"Zero-trust attestation failed at boot: '{agent_name}' was "
                f"granted unregistered tool '{tool_name}'."
            )


_attest_tool_scopes()

__all__ = [
    "AGENT_NAME",
    "BREAKER_BACKOFF_SECONDS",
    "BREAKER_MAX_ATTEMPTS",
    "STATUS_EDGE_AUTONOMOUS_DODGE",
    "STATUS_EDGE_HELD",
    "STATUS_MANEUVER_BLOCKED",
    "FleetCommanderPipeline",
    "STATE_FINAL_STATUS",
    "STATE_HUMAN_DISPATCH",
    "STATE_OBSERVABILITY_LOG",
    "fleet_commander_agent",
]
