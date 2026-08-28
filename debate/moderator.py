"""Debate — the DebateModerator: deterministic orchestration, zero LLM.

A custom ADK ``BaseAgent`` in exactly the mould of FleetCommanderPipeline:
code decides everything, models speak only when spoken to. Its referee
logic is deliberately boring and unit-testable without any model call:

* **Hallucination check** — every cited screening number must match reality
  within tolerance; unverifiable numbers disqualify the proposal.
* **Physics check** — strategy enum + delta-v inside [0, 50 m/s].
* **Envelope check** — burn targets land inside the policy envelope
  (hold_and_rescreen is exempt: its target *is* the current miss).
* **Loop detection** — SHA-256 over each argument's canonical form; a
  verbatim repeat by the same strategist freezes that agent for the rest
  of the debate (STALLED flag). Freezing stops it being *asked* again; it
  does not void the proposal, which already passed every check above.
  When the last surviving strategists all restate in the same round the
  debate has SETTLED, and their positions go to the judge — three voices
  holding firm under critique is a panel concluding, not failing.
* **Budgets** — MAX_DEBATE_ROUNDS critique rounds plus a wall-clock cap;
  exceeding either stops the debate cleanly.
* **Convergence** — identical strategy AND delta-v within epsilon ends the
  debate immediately, no judge needed.
* **Fallback** — if nothing valid survives, the classic single-specialist
  recommendation (derived deterministically from the screening result) is
  emitted with ``fallback_used=True``. The debate can fail; the mission
  cannot — because of the debate.

The winning proposal is written to session state under
``orbit_debate_outcome`` for the pipeline, and the full transcript is
persisted to the memory bank under the mission trace ID.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Final

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import ConfigDict

from geap_sim.safety_limits import MAX_ALLOWED_DELTA_V_MPS
from evolution.policy import EVOLUTION_ENVELOPE, PolicyStore, get_shared_policy_store
from geap_sim.memory_bank import MemoryBank, get_shared_memory_bank
from geap_sim.observability import audit_logger

from debate.judge import debate_judge_agent as default_judge
from debate.models import STRATEGIES, ManeuverProposal
from debate.strategists import (
    fuel_minimizer_agent as default_fuel_minimizer,
    reassess_agent as default_reassess,
    safety_maximizer_agent as default_safety_maximizer,
)

AGENT_NAME: Final[str] = "debate_moderator"

DEBATE_OUTCOME_STATE_KEY: Final[str] = "orbit_debate_outcome"
DEBATE_TRANSCRIPTS_COLLECTION: Final[str] = "debate_transcripts"

MAX_DEBATE_ROUNDS: Final[int] = int(os.environ.get("ORBIT_DEBATE_MAX_ROUNDS", "2"))
TIME_BUDGET_SECONDS: Final[float] = float(os.environ.get("ORBIT_DEBATE_TIME_BUDGET_S", "45"))
CONVERGENCE_EPSILON_MPS: Final[float] = float(os.environ.get("ORBIT_DEBATE_CONVERGENCE_EPSILON_MPS", "0.5"))

_HALLUCINATION_TOLERANCES: Final[dict[str, tuple[str, float]]] = {
    # key -> (kind, tolerance): relative for floats, exact for strings
    "pc": ("rel", 0.10),
    "miss_distance_km": ("rel", 0.05),
    "recommended_dv_mps": ("rel", 0.05),
    "tca_iso": ("exact", 0.0),
}


# ---------------------------------------------------------------------------
# Pure validation helpers (unit-testable with no ADK and no LLM)
# ---------------------------------------------------------------------------


def hallucination_check(cited: dict[str, Any], screening: dict[str, Any]) -> list[str]:
    """Names of every cited value that fails verification against reality."""
    problems: list[str] = []
    if not isinstance(cited, dict) or not cited:
        return ["cited_screening_values empty or missing"]
    for key, (kind, tol) in _HALLUCINATION_TOLERANCES.items():
        if key not in cited:
            problems.append(f"missing citation: {key}")
            continue
        real, claimed = screening.get(key), cited.get(key)
        try:
            if kind == "rel":
                real_f, claimed_f = abs(float(real)), abs(float(claimed))
                if real_f == 0.0:
                    ok = claimed_f <= tol
                else:
                    ok = abs(claimed_f - real_f) / real_f <= tol
            else:
                ok = str(claimed).strip() == str(real).strip()
        except (TypeError, ValueError):
            ok = False
        if not ok:
            problems.append(f"{key}: cited {claimed!r} vs actual {real!r}")
    return problems


def physics_check(strategy: Any, delta_v_mps: Any) -> list[str]:
    """Strategy-enum and delta-v ceiling violations."""
    problems: list[str] = []
    if strategy not in STRATEGIES:
        problems.append(f"unknown strategy {strategy!r}")
    try:
        dv = float(delta_v_mps)
    except (TypeError, ValueError):
        problems.append(f"delta_v_mps not numeric ({delta_v_mps!r})")
    else:
        if dv < 0.0 or dv > MAX_ALLOWED_DELTA_V_MPS:
            problems.append(f"delta_v {dv:.1f} m/s outside [0, {MAX_ALLOWED_DELTA_V_MPS:.0f}]")
    return problems


def envelope_check(proposal: ManeuverProposal, policy_envelope: dict[str, tuple[float, float]]) -> list[str]:
    """Burn-target miss distance must sit inside the policy envelope."""
    if proposal.strategy == "hold_and_rescreen":
        return []  # holding proposes no new trajectory; target equals today's miss
    low, high = policy_envelope["preferred_miss_distance_km"]
    if not (low <= proposal.target_miss_distance_km <= high):
        return [f"target miss {proposal.target_miss_distance_km:.3g} km outside policy envelope [{low:g}, {high:g}]"]
    return []


def validate_proposal(
    payload: dict[str, Any],
    screening: dict[str, Any],
    envelope: dict[str, tuple[float, float]],
) -> tuple[ManeuverProposal | None, list[str]]:
    """Full deterministic gate for one raw strategist answer.

    Returns ``(proposal, flags)``; ``proposal`` is None when any flag fires,
    and every flag string starts with a stable tag (HALLUCINATION / PHYSICS /
    ENVELOPE / SCHEMA) suitable for transcript logging.
    """
    flags: list[str] = []
    try:
        proposal = ManeuverProposal(**payload)
    except Exception as exc:  # noqa: BLE001 — malformed JSON shape is a schema flag
        return None, [f"SCHEMA: invalid proposal shape ({str(exc)[:120]})"]

    for problem in hallucination_check(payload.get("cited_screening_values") or {}, screening):
        flags.append(f"HALLUCINATION: {problem}")
    for problem in physics_check(payload.get("strategy"), payload.get("delta_v_mps")):
        flags.append(f"PHYSICS: {problem}")
    for problem in envelope_check(proposal, envelope):
        flags.append(f"ENVELOPE: {problem}")

    return (None, flags) if flags else (proposal, flags)


def hash_argument(text: str) -> str:
    """SHA-256 of an argument's canonical form (verbatim-repeat detection)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_fallback_proposal(screening: dict[str, Any], sat_id: str, debris_id: str) -> ManeuverProposal:
    """The classic single-specialist recommendation, deterministically derived.

    Maps the astrodynamics specialist's screening fields onto a proposal:
    direction → strategy, recommended delta-v → magnitude. This is what the
    pre-debate pipeline always acted on, so fallback behaviour is exactly
    legacy behaviour.
    """
    direction = str(screening.get("dv_direction") or "none")
    dv = max(0.0, float(screening.get("recommended_dv_mps") or 0.0))
    strategy_map = {"prograde": "prograde_burn", "retrograde": "retrograde_burn", "normal": "normal_burn"}
    strategy = strategy_map.get(direction, "hold_and_rescreen") if dv > 0 else "hold_and_rescreen"
    target = max(0.5, float(screening.get("miss_distance_km") or 0.0) * 1.25)
    return ManeuverProposal(
        strategist="astrodynamics_specialist",
        strategy=strategy,  # type: ignore[arg-type]
        delta_v_mps=dv,
        target_miss_distance_km=target,
        rationale="Fallback to the single-specialist screening recommendation.",
        cited_screening_values={},
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The moderator agent
# ---------------------------------------------------------------------------



def _strategists_offline() -> bool:
    """True when strategists must not touch the network.

    The evaluation suite advertises itself as hermetic — "no network, no
    credentials, no cost" — but for the debate that was only ever *accidentally*
    true: the three strategists are real ``LlmAgent`` instances, and full-pipeline
    tests wire the real moderator. They stayed offline purely because no
    credentials happened to be present, which had two costs.

    First, on any machine that exports ``GOOGLE_APPLICATION_CREDENTIALS``
    globally the suite would silently stop being hermetic: eighteen debates
    would go live, turning a fast deterministic run into a slow, billable and
    non-reproducible one, with no signal that anything had changed.

    Second, the failure it did produce was indistinguishable from a real
    outage. Every hermetic run logged fifty-four ``STRATEGIST_UNAVAILABLE``
    ALERTs reading "No API key was provided", which is precisely the message
    a genuinely misconfigured *deployment* emits. A passing suite that prints
    fifty-four production-shaped alerts trains readers to ignore them, and it
    sent a real investigation after a Vertex AI routing bug that did not exist.

    Setting ``ORBIT_OFFLINE_STRATEGISTS=1`` makes the degradation deliberate
    and self-describing instead. The debate still takes its documented fallback
    path — that path deserves the coverage — but it does so by decision rather
    than by absence, and says so in the audit trail.
    """
    return os.environ.get("ORBIT_OFFLINE_STRATEGISTS", "").strip().lower() in {"1", "true", "yes"}


class DebateModerator(BaseAgent):
    """Deterministic referee for the three-strategist maneuver debate."""

    fuel_minimizer: BaseAgent
    safety_maximizer: BaseAgent
    reassess: BaseAgent
    debate_judge: BaseAgent

    mission_memory: MemoryBank | None = None
    policy_store: PolicyStore | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- plumbing -----------------------------------------------------------------

    def bank(self) -> MemoryBank:
        return self.mission_memory if self.mission_memory is not None else get_shared_memory_bank()

    def store(self) -> PolicyStore:
        return self.policy_store if self.policy_store is not None else get_shared_policy_store()

    async def _policy_dump(self) -> dict[str, Any]:
        return (await self.store().load()).model_dump()

    def _status_event(self, text: str) -> Event:
        return Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=text)]))

    async def _ask_strategist(self, ctx: InvocationContext, agent: BaseAgent, prompt: str, output_key: str) -> tuple[dict[str, Any] | None, list[Event]]:
        """Invoke one strategist; returns (parsed payload or None, streamed events)."""
        collected: list[Event] = []
        ctx.session.state[output_key] = ""
        prompt_event = Event(
            author=self.name,
            content=types.Content(role="user", parts=[types.Part(text=prompt)]),
        )
        collected.append(prompt_event)

        # Hermetic runs skip the provider entirely. Scoped to LlmAgent so the
        # scripted strategists the debate tests inject still execute — the flag
        # silences the network, not the debate.
        if _strategists_offline() and isinstance(agent, LlmAgent):
            audit_logger.log_event(
                trace_id=str(ctx.session.state.get("orbit_trace_id", "debate")),
                agent_name=agent.name,
                event_type="STRATEGIST_OFFLINE",
                payload={"agent": agent.name, "reason": "ORBIT_OFFLINE_STRATEGISTS set; hermetic run"},
                status="SIMULATED",
            )
            return None, collected

        try:
            async for event in agent.run_async(ctx):
                collected.append(event)
        except Exception as exc:  # noqa: BLE001 — provider failure = this voice is silent
            audit_logger.log_event(
                trace_id=str(ctx.session.state.get("orbit_trace_id", "debate")),
                agent_name=agent.name,
                event_type="STRATEGIST_UNAVAILABLE",
                payload={"agent": agent.name, "error_type": type(exc).__name__, "error": str(exc)[:160]},
                status="DEGRADED",
            )
        raw = ctx.session.state.get(output_key, "")
        if not raw:
            # ADK's LlmAgent publishes ``output_key`` through
            # ``EventActions.state_delta``, and the Runner applies that delta
            # only once the event has been yielded up to it. This moderator
            # buffers a whole round's events before yielding, so at this point
            # the session state is still empty even though the model answered
            # perfectly. Read the delta straight off the collected events.
            for event in reversed(collected):
                delta = getattr(getattr(event, "actions", None), "state_delta", None) or {}
                candidate = delta.get(output_key)
                if candidate:
                    raw = candidate
                    ctx.session.state[output_key] = candidate
                    break
        parsed = _safe_json(raw)
        return (parsed if isinstance(parsed, dict) else None), collected

    async def _run_round(
        self,
        ctx: InvocationContext,
        participants: list[tuple[BaseAgent, str]],
        round_index: int,
        scenario: dict[str, Any],
        prior_arguments: list[dict[str, Any]],
    ) -> list[tuple[BaseAgent, str, dict[str, Any] | None, list[Event]]]:
        """Invoke all surviving strategists for one round, concurrently."""

        async def one(agent: BaseAgent, key: str) -> tuple[BaseAgent, str, dict[str, Any] | None, list[Event]]:
            mine = [p for p in prior_arguments if p.get("strategist") == agent.name]
            theirs = [p for p in prior_arguments if p.get("strategist") != agent.name]

            # The critique round used to say "revise your position in light of
            # the critiques" while supplying no critiques at all — only a flat
            # list of everyone's proposals, with no indication of what was
            # contested or what a strategist was expected to do differently.
            # Asked the same question twice with nothing new to answer, a
            # strategist emits the same answer, which the repetition detector
            # then reports as a loop. Name the disagreement, and say explicitly
            # that holding firm is a legitimate answer that must be *argued*
            # rather than restated, so a genuine stall stays distinguishable
            # from a considered one.
            critique_block = ""
            if round_index > 0 and mine and theirs:
                last_mine = mine[-1]
                spread = [
                    f"- {p['strategist']} argues {p['strategy']} at {p['delta_v_mps']:.2f} m/s: {p['rationale']}"
                    for p in theirs
                ]
                critique_block = (
                    "\n\nYOU PREVIOUSLY ARGUED: "
                    f"{last_mine['strategy']} at {last_mine['delta_v_mps']:.2f} m/s — {last_mine['rationale']}"
                    "\n\nTHE OTHER STRATEGISTS DISAGREE:\n" + "\n".join(spread)
                    + "\n\nAnswer them directly. Either move your position and say what "
                    "changed your mind, or hold it and give the specific reason their "
                    "argument fails. Do not restate your previous rationale unchanged: "
                    "if you have nothing to add, say so in a new sentence that explains "
                    "why their objection does not move you."
                )

            prompt = (
                f"SCENARIO:\n{json.dumps(scenario)}\n\n"
                f"CURRENT SCREENING POLICY:\n{json.dumps(await self._policy_dump())}\n\n"
                f"ROUND {round_index}. "
                + (
                    "Prior arguments from all strategists:\n" + json.dumps(prior_arguments, default=str)
                    if prior_arguments
                    else "This is the opening round; state your position."
                )
                + critique_block
            )
            payload, events = await self._ask_strategist(ctx, agent, prompt, key)
            return agent, key, payload, events

        results = await asyncio.gather(*(one(agent, key) for agent, key in participants))
        return list(results)

    # -- control flow ---------------------------------------------------------------

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        started_monotonic = time.monotonic()
        trace_id = str(ctx.session.state.get("orbit_trace_id") or "")
        sat_id = str(ctx.session.state.get("orbit_debate_sat_id", ""))
        debris_id = str(ctx.session.state.get("orbit_debate_debris_id", ""))
        screening = ctx.session.state.get("orbit_screening:parsed") or {}
        audit_logger.log_event(
            trace_id=trace_id, agent_name=self.name, event_type="CYCLE_START",
            payload={"sat_id": sat_id, "debris_id": debris_id}, status="OK",
        )
        yield self._status_event("Strategist debate convened: Fuel Minimizer vs Safety Maximizer vs Reassess.")

        flags: list[dict[str, Any]] = []
        rounds: list[dict[str, Any]] = []
        frozen_agents: set[str] = set()
        argument_hashes: dict[str, list[str]] = {}
        converged = False
        winner_name: str | None = None
        judge_used = False

        budget_exceeded = False

        def budget_left() -> bool:
            return (time.monotonic() - started_monotonic) < TIME_BUDGET_SECONDS

        # Flags report the round they happened in. This used to be len(rounds),
        # the number of arguments accumulated so far, which is why a debate
        # capped at 2 critique rounds emitted flags labelled "round": 5.
        current_round = 0

        def note_flag(code: str, detail: str, severity: str = "WARNING") -> None:
            entry = {"code": code, "severity": severity, "detail": detail[:240], "round": current_round}
            flags.append(entry)
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name,
                event_type=f"DEBATE_FLAG_{code}",
                payload=entry,
                status={"CRITICAL": "ERROR", "WARNING": "WARNING"}.get(severity, "NOTICE"),
            )

        roster: list[tuple[BaseAgent, str]] = [
            (self.fuel_minimizer, "orbit_debate_fuel_minimizer"),
            (self.safety_maximizer, "orbit_debate_safety_maximizer"),
            (self.reassess, "orbit_debate_reassess"),
        ]
        survivors: dict[str, ManeuverProposal] = {}
        #: Validated proposals from strategists frozen for restating their
        #: position. Held so a settled debate can still be adjudicated.
        settled: dict[str, ManeuverProposal] = {}

        scenario = {
            "sat_id": sat_id,
            "debris_id": debris_id,
            "screening": screening,
            "constraints": {
                "max_delta_v_mps": MAX_ALLOWED_DELTA_V_MPS,
                "target_miss_envelope_km": list(EVOLUTION_ENVELOPE["preferred_miss_distance_km"]),
            },
        }
        prior_arguments: list[dict[str, Any]] = []

        # ---- Round 0 + critique rounds -----------------------------------------
        for round_index in range(0, MAX_DEBATE_ROUNDS + 1):
            if not budget_left():
                note_flag("BUDGET_EXCEEDED", "wall-clock budget exhausted before this round", "WARNING")
                budget_exceeded = True
                break

            current_round = round_index
            active = [(a, k) for a, k in roster if a.name not in frozen_agents]
            results = await self._run_round(ctx, active, round_index, scenario, prior_arguments)
            for events in (r[3] for r in results):
                for e in events:
                    yield e

            round_args: list[dict[str, Any]] = []
            for agent, _key, payload, _events in results:
                if payload is None:
                    continue
                proposal, vflags = validate_proposal(payload, screening, EVOLUTION_ENVELOPE)
                canonical = proposal.canonical() if proposal else json.dumps(payload, sort_keys=True, default=str)
                digest = hash_argument(canonical)
                previous = argument_hashes.setdefault(agent.name, [])
                verbatim_repeat = digest in previous
                previous.append(digest)

                if verbatim_repeat:
                    frozen_agents.add(agent.name)
                    survivors.pop(agent.name, None)
                    # Keep the restated proposal aside. Repetition means this
                    # strategist has stopped moving, which is a reason to stop
                    # *asking* it — not a reason to throw away an argument that
                    # passed every validation check.
                    if proposal is not None:
                        settled[agent.name] = proposal
                    note_flag("STALLED", f"{agent.name} repeated a verbatim argument; frozen for the remainder", "WARNING")

                for vf in vflags:
                    note_flag(vf.split(":")[0], f"{agent.name}: {vf}", "CRITICAL" if vf.startswith(("HALLUCINATION", "PHYSICS")) else "WARNING")
                if proposal is None or verbatim_repeat:
                    survivors.pop(agent.name, None)
                    continue

                survivors[agent.name] = proposal
                arg_entry = {
                    "round": round_index,
                    "strategist": agent.name,
                    "argument": proposal.rationale,
                    "critique_of": None,
                    "proposal": proposal.model_dump(),
                    "hash": digest[:12],
                }
                round_args.append(arg_entry)
                rounds.append(arg_entry)

            prior_arguments = [
                {"strategist": a["strategist"], "round": a["round"], "strategy": a["proposal"]["strategy"],
                 "delta_v_mps": a["proposal"]["delta_v_mps"], "rationale": a["argument"]}
                for a in rounds
            ]

            # ---- Every voice has settled -------------------------------------
            # When the LAST surviving strategists all restate their positions in
            # the same round, the pool empties and the debate used to fall
            # straight through to the single-specialist fallback — discarding
            # three fully validated proposals and reporting a failure.
            #
            # That is backwards. Three strategists holding firm under critique
            # is the debate reaching its conclusion, not breaking down. The
            # freeze still applies (nobody is asked again, so the loop cannot
            # burn the budget), but the arguments they settled on go to the
            # judge, which is what they were produced for.
            if not survivors and settled:
                survivors.update(settled)
                note_flag(
                    "SETTLED",
                    "every remaining strategist restated its position; adjudicating on the settled proposals",
                    "NOTICE",
                )

            # ---- Convergence check ------------------------------------------
            burns = [p for p in survivors.values() if p.strategy != "hold_and_rescreen"]
            holds = [p for p in survivors.values() if p.strategy == "hold_and_rescreen"]
            pool = burns if len(burns) >= 2 else (holds if len(holds) >= 2 else list(survivors.values()))
            if len(pool) >= 2:
                first = pool[0]
                if all(p.strategy == first.strategy and abs(p.delta_v_mps - first.delta_v_mps) <= CONVERGENCE_EPSILON_MPS for p in pool[1:]):
                    converged = True
                    winner_name = first.strategist
                    audit_logger.log_event(
                        trace_id=trace_id, agent_name=self.name, event_type="DEBATE_CONVERGED",
                        payload={"strategy": first.strategy, "delta_v_mps": first.delta_v_mps,
                                 "round": round_index, "supporters": [p.strategist for p in pool]},
                        status="OK",
                    )
                    break

            if (
                round_index >= MAX_DEBATE_ROUNDS
                or budget_exceeded
                or not budget_left()
                # Every strategist frozen means the next round would invoke
                # nobody and change nothing; spending a round to discover that
                # is pure latency on the critical path of a HIGH-risk mission.
                or len(frozen_agents) >= len(roster)
            ):
                break

        # ---- Judge: only when several valid proposals remain unconverged -------
        winner_proposal: ManeuverProposal | None = None
        if converged and winner_name:
            winner_proposal = survivors.get(winner_name)
        elif len(survivors) == 1:
            winner_proposal = next(iter(survivors.values()))
        elif len(survivors) > 1 and not budget_exceeded:
            judge_used = True
            yield self._status_event("No numeric convergence — the Debate Judge selects among validated proposals.")
            judge_prompt = (
                "VALIDATED PROPOSALS:\n"
                + json.dumps([p.model_dump() for p in survivors.values()], default=str)
                + "\n\nSCREENING REALITY:\n" + json.dumps(screening, default=str)
                + "\n\nSelect the winner per your rules."
            )
            ctx.session.state["orbit_debate_judge"] = ""
            judge_answer: dict[str, Any] | None = None
            judge_raw = ""
            try:
                async for event in self.debate_judge.run_async(ctx):
                    # Same state_delta caveat as the strategists: capture the
                    # verdict off the event rather than trusting that the
                    # Runner has committed it by the time we read state.
                    delta = getattr(getattr(event, "actions", None), "state_delta", None) or {}
                    judge_raw = delta.get("orbit_debate_judge") or judge_raw
                    yield event
                judge_raw = ctx.session.state.get("orbit_debate_judge", "") or judge_raw
                judge_answer = _safe_json(judge_raw)
            except Exception as exc:  # noqa: BLE001 — judge outage → fallback path
                note_flag("JUDGE_UNAVAILABLE", f"{type(exc).__name__}: {exc}", "WARNING")
            chosen = str((judge_answer or {}).get("winner", "")).strip()
            if chosen in survivors:
                winner_proposal = survivors[chosen]
                audit_logger.log_event(
                    trace_id=trace_id, agent_name=self.name, event_type="DEBATE_JUDGE_DECIDED",
                    payload={"winner": chosen,
                             "justification": str((judge_answer or {}).get("justification", ""))[:400]},
                    status="OK",
                )
            else:
                note_flag("JUDGE_INVALID", f"judge named {chosen!r}, which is not a validated proposal", "WARNING")

        # ---- Fallback ------------------------------------------------------------
        fallback_used = False
        if winner_proposal is None:
            fallback_used = True
            winner_proposal = build_fallback_proposal(screening, sat_id, debris_id)
            note_flag("FALLBACK", "debate produced no safe winner; using the single-specialist recommendation", "WARNING")
            audit_logger.log_event(
                trace_id=trace_id, agent_name=self.name, event_type="DEBATE_FALLBACK",
                payload={"reason": "no validated converged/winning proposal"},
                status="WARNING",
            )

        outcome = {
            **winner_proposal.model_dump(),
            "action": "we_dodge" if winner_proposal.strategy != "hold_and_rescreen" else "hold_and_rescreen",
            "our_dv_mps": winner_proposal.delta_v_mps,
            "their_dv_mps": 0.0,
            "converged": converged,
            "fallback_used": fallback_used,
            "judge_used": judge_used,
            "trace_id": trace_id,
            "moderator": self.name,
        }

        transcript = {
            "trace_id": trace_id,
            "sat_id": sat_id,
            "debris_id": debris_id,
            "rounds": rounds,
            "flags": flags,
            "converged": converged,
            "winner": winner_proposal.strategist,
            "final_proposal": winner_proposal.model_dump(),
            "fallback_used": fallback_used,
            "judge_used": judge_used,
            "completed_utc": _utc_now_iso(),
        }

        await self.bank().put_doc(DEBATE_TRANSCRIPTS_COLLECTION, trace_id, transcript)
        audit_logger.log_event(
            trace_id=trace_id, agent_name=self.name, event_type="DEBATE_COMPLETE",
            payload={
                "winner": winner_proposal.strategist,
                "strategy": winner_proposal.strategy,
                "delta_v_mps": winner_proposal.delta_v_mps,
                "converged": converged,
                "fallback_used": fallback_used,
                "flags": [f["code"] for f in flags],
                "arguments": len(rounds),
            },
            status="EXECUTED",
        )
        yield self._status_event(
            f"Debate complete: winner={winner_proposal.strategist} "
            f"({winner_proposal.strategy} @ {winner_proposal.delta_v_mps:.1f} m/s"
            f"{' , fallback' if fallback_used else ''}{', judged' if judge_used else ''})."
        )

        payload_json = json.dumps(outcome, default=str)
        ctx.session.state[DEBATE_OUTCOME_STATE_KEY] = payload_json
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=f"Debate outcome committed: {winner_proposal.strategy}")]),
            actions=EventActions(state_delta={DEBATE_OUTCOME_STATE_KEY: payload_json}),
        )


def _safe_json(raw: Any) -> Any:
    import re as _re

    if not isinstance(raw, str) or not raw.strip():
        return None
    cleaned = _re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = _re.search(r"\{.*\}", cleaned, flags=_re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


debate_moderator_agent = DebateModerator(
    name=AGENT_NAME,
    description=(
        "Deterministic three-strategist maneuver debate for HIGH-risk "
        "conjunctions. Validates every proposal against screening reality, "
        "detects loops via argument hashing, enforces round/time budgets, "
        "judges when needed, and falls back gracefully."
    ),
    fuel_minimizer=default_fuel_minimizer,
    safety_maximizer=default_safety_maximizer,
    reassess=default_reassess,
    debate_judge=default_judge,
    # Formal sub-agents so /api/agent_tree shows the full debate panel.
    sub_agents=[default_fuel_minimizer, default_safety_maximizer, default_reassess, default_judge],
)

__all__ = [
    "AGENT_NAME",
    "DEBATE_OUTCOME_STATE_KEY",
    "DebateModerator",
    "build_fallback_proposal",
    "debate_moderator_agent",
    "envelope_check",
    "hallucination_check",
    "hash_argument",
    "physics_check",
    "validate_proposal",
]
