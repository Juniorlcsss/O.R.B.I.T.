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

The FleetCommander never calls tools directly and never authorises anything
itself — execution authority flows exclusively through the SafetyOfficer's
verdict.
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

from .astro import astrodynamics_agent
from .diplomat import diplomat_agent
from .safety import safety_officer_agent

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

    alert_triage: LlmAgent
    astrodynamics_specialist: LlmAgent
    negotiation_officer: LlmAgent
    model_armor_checkpoint: LlmAgent

    #: GEAP MemoryBank — persistent satellite state & conjunction history.
    mission_memory: MemoryBank | None = None
    #: GEAP ModelArmor — deterministic post-verdict guardrail sweep.
    armor_inspector: ModelArmor | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def specialists(self) -> tuple[LlmAgent, LlmAgent, LlmAgent]:
        """The three specialist agents routed to by this commander."""
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
        )

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
        yield self._status_event("HIGH RISK confirmed. Engaging negotiation officer…")
        yield self._dossier_event(dossier)
        async for event in self._guarded_invoke(ctx, agent=self.negotiation_officer, state_key=NEGOTIATION_OUTPUT_KEY, validator=_validate_negotiation):
            yield event
        if not ctx.session.state.get(f"{NEGOTIATION_OUTPUT_KEY}:ok"):
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
            # FAIL-CLOSED: no armour verdict means NO execution, ever.
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

        if report.status != "APPROVED":
            decision = {
                "decision": STATUS_MANEUVER_BLOCKED,
                "violations": report.violations,
                "checks": report.checks,
                "audit_trace_id": report.audit_trace_id,
                "memory_bank_updated": False,
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
            yield self._status_event(f"Manoeuvre BLOCKED by Model Armour ({'; '.join(report.violations)}).")
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
        }
        ctx.session.state[STATE_EXECUTION_DECISION] = decision
        self._log_observation(ctx, "INFO", self.name, "Manoeuvre APPROVED end-to-end; authorised for uplink.", decision=decision)
        yield self._status_event("Model Armour APPROVED all checks. Manoeuvre authorised for uplink; fleet state persisted.")
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
    mission_memory=get_shared_memory_bank(),
    armor_inspector=get_shared_model_armor(),
    # Registered as formal sub-agents so ADK tooling (tree walkers, `adk
    # web`, agent discovery) sees the full fleet hierarchy.
    sub_agents=[alert_triage_agent, astrodynamics_agent, diplomat_agent, safety_officer_agent],
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
    for member in (astrodynamics_agent, diplomat_agent):
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
    "STATUS_MANEUVER_BLOCKED",
    "FleetCommanderPipeline",
    "STATE_FINAL_STATUS",
    "STATE_HUMAN_DISPATCH",
    "STATE_OBSERVABILITY_LOG",
    "fleet_commander_agent",
]
