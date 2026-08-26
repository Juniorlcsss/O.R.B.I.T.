"""Evolution â€” the EvolutionEngine (self-modification control plane).

A custom ADK ``BaseAgent`` in exactly the same style as
``FleetCommanderPipeline``: deterministic code owns every decision, LLMs do
specialist work inside it, and everything downstream of a model's words is
validated, gated and clamped.

Per-cycle flow (fail-closed at every stage):

    CYCLE_START â†’ gather outcomes â†’ skip if thin â†’ load policy & history
    â†’ frozen? stop â†’ analyst proposes (or NO_CHANGE) â†’ deterministic gaming
    flags â†’ Meta-Critic verdict â†’ REJECT on CRITICAL flag / REJECT verdict /
    suspicion â‰¥ 0.7 â†’ freeze after repeated failures â†’ candidate policy
    â†’ clamp_to_envelope (ALWAYS) â†’ validate invariants â†’ save â†’ log cycle
    â†’ CYCLE_APPLIED â†’ EvolutionReport.

The two specialist slots are plain ``BaseAgent`` fields so evaluation
harnesses can substitute scripted stand-ins with identical contracts â€”
the same trick the Phase 9 suite uses for the mission pipeline.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Final

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from geap_sim.memory_bank import MemoryBank, get_shared_memory_bank
from geap_sim.observability import audit_logger

from evolution.gaming import GamingFlag, detect_gaming
from evolution.learning_analyst import learning_analyst_agent as default_learning_analyst
from evolution.meta_critic import meta_critic_agent as default_meta_critic
from evolution.policy import (
    EVOLUTION_ENVELOPE,
    PolicyStore,
    ScreeningPolicy,
    clamp_to_envelope,
    get_shared_policy_store,
    validate_invariants,
)

AGENT_NAME: Final[str] = "evolution_engine"

PROPOSAL_OUTPUT_KEY: Final[str] = "orbit_evolution_proposal"
VERDICT_OUTPUT_KEY: Final[str] = "orbit_evolution_verdict"
STATE_REPORT_KEY: Final[str] = "orbit_evolution_report"

MIN_OUTCOMES_FOR_EVOLUTION: Final[int] = int(os.environ.get("ORBIT_EVOLUTION_MIN_OUTCOMES", "5"))
MAX_CONSECUTIVE_REJECTIONS: Final[int] = int(os.environ.get("ORBIT_EVOLUTION_MAX_REJECTIONS", "3"))
MAX_ENVELOPE_PUSHES: Final[int] = int(os.environ.get("ORBIT_EVOLUTION_MAX_ENVELOPE_PUSHES", "3"))
MIN_CONFIDENCE: Final[float] = float(os.environ.get("ORBIT_EVOLUTION_MIN_CONFIDENCE", "0.3"))
REJECT_SUSPICION_THRESHOLD: Final[float] = 0.7

_STATUS_APPLIED = "APPLIED"
_STATUS_CLAMPED_APPLIED = "CLAMPED_APPLIED"
_STATUS_REJECTED = "REJECTED"
_STATUS_NO_CHANGE = "NO_CHANGE_PROPOSED"
_STATUS_SKIPPED = "SKIPPED_INSUFFICIENT_DATA"
_STATUS_FROZEN = "FROZEN_HUMAN_REVIEW"

_META_FROZEN: Final[str] = "evolution_frozen"
_META_REJECTIONS: Final[str] = "evolution_rejection_counter"
_META_ENVELOPE_PUSHES: Final[str] = "evolution_envelope_push_counter"
_META_LAST_TRACE: Final[str] = "evolution_last_trace_id"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json(text: Any) -> dict[str, Any]:
    """Best-effort JSON-object extraction from an agent transcript."""
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


class EvolutionCycle(BaseModel):
    """Persisted before/after record of one evolution attempt."""

    trace_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    proposal_reasoning: str = ""
    critique_reasoning: str = ""
    gaming_flags: list[dict[str, Any]] = Field(default_factory=list)
    clamp_actions: list[str] = Field(default_factory=list)
    verdict: str = ""
    applied: bool = False
    timestamp: str = Field(default_factory=_utc_now_iso)


class EvolutionReport(BaseModel):
    """What one engine invocation returned to its caller."""

    status: str
    trace_id: str = ""
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    diff_summary: list[str] = Field(default_factory=list)
    clamp_actions: list[str] = Field(default_factory=list)
    gaming_flags: list[dict[str, Any]] = Field(default_factory=list)
    verdict: str = ""
    suspicion_score: float | None = None
    reasoning: str = ""
    rejection_counter: int = 0
    frozen: bool = False


class EvolutionEngine(BaseAgent):
    """Deterministic self-evolution control plane (see module docstring)."""

    learning_analyst: BaseAgent
    meta_critic: BaseAgent

    mission_memory: MemoryBank | None = None
    policy_store: PolicyStore | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- plumbing ---------------------------------------------------------------

    def bank(self) -> MemoryBank:
        return self.mission_memory if self.mission_memory is not None else get_shared_memory_bank()

    def store(self) -> PolicyStore:
        return self.policy_store if self.policy_store is not None else get_shared_policy_store()

    def _status_event(self, text: str) -> Event:
        return Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=text)]))

    async def _invoke_specialist(self, ctx: InvocationContext, *, agent: BaseAgent, prompt_text: str, output_key: str, trace_id: str = "evolution") -> AsyncGenerator[Event, None]:
        """Stream one specialist turn; the caller reads ``ctx.session.state[output_key]`` after.

        Provider failures are caught and audited here — an unreachable model
        leaves the output slot empty, which every caller treats as
        fail-closed (no proposal / automatic REJECT), never as a crash.
        """
        ctx.session.state[output_key] = ""
        yield Event(
            author=self.name,
            content=types.Content(role="user", parts=[types.Part(text=prompt_text)]),
        )
        try:
            async for event in agent.run_async(ctx):
                yield event
        except Exception as exc:  # noqa: BLE001 — provider outages are expected, not fatal
            audit_logger.log_event(
                trace_id=trace_id,
                agent_name=agent.name,
                event_type="EVOLUTION_SPECIALIST_FAILED",
                payload={"agent": agent.name, "error_type": type(exc).__name__, "error": str(exc)[:200]},
                status="DEGRADED",
            )

    @staticmethod
    def _policy_from_payload(payload: dict[str, Any]) -> ScreeningPolicy | None:
        fields = payload.get("proposed_policy") or payload.get("clamped_policy")
        if not isinstance(fields, dict):
            return None
        try:
            tunables = {name: fields[name] for name in EVOLUTION_ENVELOPE if name in fields}
            return ScreeningPolicy(**tunables, provenance="evolved")
        except Exception:  # noqa: BLE001 â€” any malformed field invalidates the whole proposal
            return None

    @staticmethod
    def _diff(before: ScreeningPolicy, after: ScreeningPolicy) -> list[str]:
        lines: list[str] = []
        for name in EVOLUTION_ENVELOPE:
            old_value, new_value = float(getattr(before, name)), float(getattr(after, name))
            if abs(old_value - new_value) > abs(EVOLUTION_ENVELOPE[name][1] - EVOLUTION_ENVELOPE[name][0]) * 1e-9:
                lines.append(f"{name}: {old_value:.6g} â†’ {new_value:.6g}")
        return lines or ["(no parameter changes)"]

    # -- control flow -------------------------------------------------------------

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        trace_id = uuid.uuid4().hex
        audit_logger.log_event(
            trace_id=trace_id, agent_name=self.name, event_type="CYCLE_START",
            payload={"engine": self.name}, status="OK",
        )
        yield self._status_event("Evolution cycle started.")

        # ---- Step 2-3: outcome evidence --------------------------------------
        outcomes = await self.bank().get_recent_outcomes(limit=20)
        if len(outcomes) < MIN_OUTCOMES_FOR_EVOLUTION:
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="CYCLE_SKIPPED_INSUFFICIENT_DATA",
                payload={"outcomes": len(outcomes), "required": MIN_OUTCOMES_FOR_EVOLUTION},
                status="SKIPPED",
            )
            yield await self._commit(ctx, EvolutionReport(
                status=_STATUS_SKIPPED, trace_id=trace_id,
                reasoning=f"only {len(outcomes)} outcome(s); need {MIN_OUTCOMES_FOR_EVOLUTION}",
            ))
            yield self._status_event(f"Skipped: {len(outcomes)} outcomes < {MIN_OUTCOMES_FOR_EVOLUTION}.")
            return

        # ---- Steps 4-6: policy, history, freeze gate ---------------------------
        current_policy = await self.store().load()
        history = await self.bank().get_evolution_history(limit=10)

        if bool(await self.bank().get_meta(_META_FROZEN, False)):
            rejections = int(await self.bank().get_meta(_META_REJECTIONS, 0) or 0)
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="CYCLE_BLOCKED_FROZEN",
                payload={"rejection_counter": rejections}, status="FROZEN",
            )
            report = EvolutionReport(
                status=_STATUS_FROZEN, trace_id=trace_id,
                reasoning="evolution is frozen pending human review",
                rejection_counter=rejections, frozen=True,
            )
            yield await self._commit(ctx, report)
            yield self._status_event("Evolution is FROZEN â€” human review required.")
            return

        # ---- Step 7: proposal ---------------------------------------------------
        proposal_prompt = (
            "CURRENT POLICY:\n" + json.dumps(current_policy.model_dump()) +
            "\n\nRECENT MISSION OUTCOMES (newest first):\n" + json.dumps(outcomes, default=str) +
            "\n\nPropose the next ScreeningPolicy adjustment per your rules."
        )
        async for _ in self._invoke_specialist(ctx, agent=self.learning_analyst, prompt_text=proposal_prompt, output_key=PROPOSAL_OUTPUT_KEY, trace_id=trace_id):
            pass
        proposal_payload: dict[str, Any] | None = _extract_json(ctx.session.state.get(PROPOSAL_OUTPUT_KEY, "")) or None

        if proposal_payload is None:
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="ANALYST_UNAVAILABLE",
                payload={}, status="DEGRADED",
            )
            report = EvolutionReport(status=_STATUS_NO_CHANGE, trace_id=trace_id,
                                     reasoning="learning analyst produced no parsable proposal (fail-closed)")
            yield await self._commit(ctx, report)
            yield self._status_event("Analyst unavailable â€” no change proposed.")
            return

        confidence = float(proposal_payload.get("confidence") or 0.0)
        no_change = bool(proposal_payload.get("no_change")) or confidence < MIN_CONFIDENCE
        proposed_policy = self._policy_from_payload(proposal_payload)
        if no_change or proposed_policy is None:
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="NO_CHANGE_PROPOSED",
                payload={"confidence": confidence, "no_change": no_change, "schema_valid": proposed_policy is not None},
                status="OK",
            )
            report = EvolutionReport(status=_STATUS_NO_CHANGE, trace_id=trace_id,
                                     reasoning=str(proposal_payload.get("reasoning", ""))[:400],
                                     rejection_counter=int(await self.bank().get_meta(_META_REJECTIONS, 0) or 0))
            yield await self._commit(ctx, report)
            yield self._status_event("Analyst proposes NO CHANGE.")
            return

        # ---- Step 8: deterministic gaming heuristics ----------------------------
        gaming_flags = detect_gaming(current_policy, proposed_policy, outcomes, history)
        critical_flags = [f for f in gaming_flags if f.severity == "CRITICAL"]

        # ---- Step 9: adversarial critique ----------------------------------------
        critic_prompt = (
            "CURRENT POLICY:\n" + json.dumps(current_policy.model_dump()) +
            "\n\nPROPOSED POLICY:\n" + json.dumps(proposed_policy.model_dump()) +
            "\n\nOUTCOME EVIDENCE:\n" + json.dumps(outcomes, default=str) +
            "\n\nDETERMINISTIC GAMING FLAGS:\n" + json.dumps([f.model_dump() for f in gaming_flags], default=str) +
            "\n\nDeliver your adversarial verdict per your rules."
        )
        async for _ in self._invoke_specialist(ctx, agent=self.meta_critic, prompt_text=critic_prompt, output_key=VERDICT_OUTPUT_KEY, trace_id=trace_id):
            pass
        critique = _extract_json(ctx.session.state.get(VERDICT_OUTPUT_KEY, "")) or None

        if critique is None:
            # Fail-closed: no adversarial verdict â†’ treat as REJECT.
            critique = {
                "verdict": "REJECT",
                "reasoning": "Meta-Critic unavailable; fail-closed policy forbids unreviewed self-modification",
                "gaming_suspicion_score": 1.0,
                "safety_concerns": ["critic_unreachable"],
            }

        verdict = str(critique.get("verdict", "REJECT")).strip().upper()
        if verdict not in ("APPROVE", "CLAMP", "REJECT"):
            verdict = "REJECT"
        suspicion = float(critique.get("gaming_suspicion_score") or 0.0)

        # ---- Steps 10-11: reject conditions & freeze circuit-breaker -------------
        hard_reject = bool(critical_flags) or verdict == "REJECT" or suspicion >= REJECT_SUSPICION_THRESHOLD
        rejections = int(await self.bank().get_meta(_META_REJECTIONS, 0) or 0)
        envelope_pushes = int(await self.bank().get_meta(_META_ENVELOPE_PUSHES, 0) or 0)

        if hard_reject:
            rejections += 1
            if [f.code for f in critical_flags]:
                envelope_pushes += 1
            await self.bank().set_meta(_META_REJECTIONS, rejections)
            await self.bank().set_meta(_META_ENVELOPE_PUSHES, envelope_pushes)

            frozen_now = rejections >= MAX_CONSECUTIVE_REJECTIONS or envelope_pushes >= MAX_ENVELOPE_PUSHES
            if frozen_now:
                await self.bank().set_meta(_META_FROZEN, True)
                audit_logger.log_event(
                    trace_id=trace_id, agent_name=self.name, event_type="EVOLUTION_FROZEN_HUMAN_REVIEW",
                    payload={
                        "rejection_counter": rejections,
                        "envelope_push_counter": envelope_pushes,
                        "critical_flags": [f.model_dump() for f in critical_flags],
                    },
                    status="ERROR",
                )
                report = EvolutionReport(
                    status=_STATUS_FROZEN, trace_id=trace_id,
                    gaming_flags=[f.model_dump() for f in gaming_flags], verdict=verdict,
                    suspicion_score=suspicion, reasoning=str(critique.get("reasoning", ""))[:500],
                    rejection_counter=rejections, frozen=True,
                )
                await self._log_cycle(trace_id, current_policy, current_policy, proposal_payload, critique, gaming_flags, [], verdict, applied=False)
                yield await self._commit(ctx, report)
                yield self._status_event("Evolution FROZEN â€” human review required.")
                return

            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="CYCLE_REJECTED",
                payload={
                    "gaming_flags": [f.model_dump() for f in gaming_flags],
                    "verdict": verdict,
                    "gaming_suspicion_score": suspicion,
                    "critique_reasoning": str(critique.get("reasoning", ""))[:500],
                    "safety_concerns": critique.get("safety_concerns", []),
                    "rejection_counter": rejections,
                },
                status="REJECTED",
            )
            await self._log_cycle(trace_id, current_policy, current_policy, proposal_payload, critique, gaming_flags, [], verdict, applied=False)
            report = EvolutionReport(
                status=_STATUS_REJECTED, trace_id=trace_id,
                gaming_flags=[f.model_dump() for f in gaming_flags], verdict=verdict,
                suspicion_score=suspicion, reasoning=str(critique.get("reasoning", ""))[:500],
                rejection_counter=rejections,
            )
            yield await self._commit(ctx, report)
            yield self._status_event(f"Cycle REJECTED ({verdict}; suspicion={suspicion:.2f}).")
            return

        # Healthy cycle: decay the rejection streak.
        await self.bank().set_meta(_META_REJECTIONS, 0)

        # ---- Steps 12-13: candidate selection & the ALWAYS-applied clamp --------
        candidate = proposed_policy
        clamped_by_critic = False
        if verdict == "CLAMP" and isinstance(critique.get("clamped_policy"), dict):
            corrected = self._policy_from_payload({"clamped_policy": critique["clamped_policy"]})
            if corrected is not None:
                candidate = corrected
                clamped_by_critic = True

        final_policy, clamp_actions = clamp_to_envelope(current_policy, candidate)

        # Track repeated envelope-pushing even when the clamp saved us.
        if any("envelope [" in action for action in clamp_actions):
            envelope_pushes += 1
            await self.bank().set_meta(_META_ENVELOPE_PUSHES, envelope_pushes)
            if envelope_pushes >= MAX_ENVELOPE_PUSHES:
                await self.bank().set_meta(_META_FROZEN, True)
                audit_logger.log_event(
                    trace_id=trace_id, agent_name=self.name, event_type="EVOLUTION_FROZEN_HUMAN_REVIEW",
                    payload={"envelope_push_counter": envelope_pushes, "clamp_actions": clamp_actions},
                    status="ERROR",
                )
                await self._log_cycle(trace_id, current_policy, current_policy, proposal_payload, critique, gaming_flags, clamp_actions, "CLAMP_FROZEN", applied=False)
                report = EvolutionReport(
                    status=_STATUS_FROZEN, trace_id=trace_id, clamp_actions=clamp_actions,
                    gaming_flags=[f.model_dump() for f in gaming_flags], verdict="CLAMP",
                    reasoning="repeated envelope-pushing despite clamping; frozen for human review",
                    rejection_counter=rejections, frozen=True,
                )
                yield await self._commit(ctx, report)
                yield self._status_event("Repeated envelope-pushing â€” evolution FROZEN.")
                return
        else:
            await self.bank().set_meta(_META_ENVELOPE_PUSHES, 0)

        # ---- Step 14: final invariant validation ---------------------------------
        violations = validate_invariants(final_policy)
        if violations:
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="INVARIANT_VIOLATION",
                payload={"violations": violations}, status="ERROR",
            )
            report = EvolutionReport(
                status=_STATUS_REJECTED, trace_id=trace_id,
                reasoning="final policy failed invariant validation: " + "; ".join(violations),
                rejection_counter=rejections,
            )
            await self._log_cycle(trace_id, current_policy, current_policy, proposal_payload, critique, gaming_flags, clamp_actions, "INVARIANT_FAIL", applied=False)
            yield await self._commit(ctx, report)
            yield self._status_event("Invariant violation â€” cycle rejected.")
            return

        # ---- Steps 15-17: apply, persist, audit ----------------------------------
        saved_policy = await self.store().save(final_policy)
        diff_lines = self._diff(current_policy, saved_policy)

        await self._log_cycle(
            trace_id, current_policy, saved_policy, proposal_payload, critique,
            gaming_flags, clamp_actions, verdict, applied=True,
        )

        status = _STATUS_CLAMPED_APPLIED if (clamp_actions or clamped_by_critic) else _STATUS_APPLIED
        audit_logger.log_event(
            trace_id=trace_id, agent_name=self.name, event_type="CYCLE_APPLIED",
            payload={
                "before": current_policy.model_dump(),
                "after": saved_policy.model_dump(),
                "diff": diff_lines,
                "clamp_actions": clamp_actions,
                "verdict": verdict,
                "policy_version": saved_policy.policy_version,
            },
            status="EXECUTED",
        )
        report = EvolutionReport(
            status=status, trace_id=trace_id,
            before=current_policy.model_dump(), after=saved_policy.model_dump(),
            diff_summary=diff_lines, clamp_actions=clamp_actions,
            gaming_flags=[f.model_dump() for f in gaming_flags], verdict=verdict,
            suspicion_score=suspicion,
            reasoning=str(proposal_payload.get("reasoning", ""))[:400],
            rejection_counter=0,
        )
        yield await self._commit(ctx, report)
        yield self._status_event(
            f"Evolution {status}: {'; '.join(diff_lines)} (policy v{saved_policy.policy_version})."
        )

    # -- persistence helpers ------------------------------------------------------

    async def _log_cycle(
        self,
        trace_id: str,
        before: ScreeningPolicy,
        after: ScreeningPolicy,
        proposal: dict[str, Any],
        critique: dict[str, Any],
        gaming_flags: list[GamingFlag],
        clamp_actions: list[str],
        verdict: str,
        applied: bool,
    ) -> None:
        await self.bank().log_evolution_cycle({
            "trace_id": trace_id,
            "before": before.model_dump(),
            "after": after.model_dump(),
            "proposal_reasoning": str(proposal.get("reasoning", ""))[:800] if proposal else "",
            "critique_reasoning": str(critique.get("reasoning", ""))[:800] if critique else "",
            "gaming_flags": [f.model_dump() for f in gaming_flags],
            "clamp_actions": clamp_actions,
            "verdict": verdict,
            "applied": applied,
        })

    async def _commit(self, ctx: InvocationContext, report: EvolutionReport) -> Event:
        """Persist meta state and return the terminal event carrying the report.

        ADK merges ``actions.state_delta`` into the stored session, so the
        HTTP layer can read ``STATE_REPORT_KEY`` after the run.
        """
        await self.bank().set_meta(_META_LAST_TRACE, report.trace_id)
        payload = report.model_dump_json()
        ctx.session.state[STATE_REPORT_KEY] = payload
        return Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=f"Evolution cycle complete: {report.status}")]),
            actions=EventActions(state_delta={STATE_REPORT_KEY: payload}),
        )
