"""Evaluation (Phase 11) — a looping strategist is frozen, the debate finishes.

The Reassess strategist repeats its round-0 argument verbatim in the
critique round: the SHA-256 repetition detector flags STALLED and freezes
that agent, while the remaining two voices revise and proceed to
adjudication inside budget.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import EvaluationHarness, astro_screening_fixture  # noqa: E402

NAME = "debate_loop_caught"
DESCRIPTION = "Verbatim repeat -> STALLED flag + agent frozen; debate completes via judge within budget"


async def execute(harness: EvaluationHarness):
    checks = []
    screening = astro_screening_fixture()
    cited = {
        "pc": screening["pc"],
        "miss_distance_km": screening["miss_distance_km"],
        "tca_iso": screening["tca_iso"],
        "recommended_dv_mps": screening["recommended_dv_mps"],
    }

    # Deliberately divergent strategies in round 0 so no numeric convergence
    # occurs and the critique round (where the loop fires) actually runs.
    fuel_state = {"calls": 0}
    safety_state = {"calls": 0}
    reassess_state = {"calls": 0}

    def fuel():
        fuel_state["calls"] += 1
        revision = "" if fuel_state["calls"] == 1 else f" Revision #{fuel_state['calls']}: trimmed further after critiques."
        return {
            "strategist": "fuel_minimizer", "strategy": "prograde_burn",
            "delta_v_mps": 5.0 if fuel_state["calls"] == 1 else 4.5,
            "target_miss_distance_km": 0.9,
            "rationale": f"burn less, live longer; five m/s suffices.{revision}",
            "cited_screening_values": dict(cited),
        }

    def safety():
        safety_state["calls"] += 1
        revision = "" if safety_state["calls"] == 1 else f" Held firm on margin (restatement {safety_state['calls']})."
        return {
            "strategist": "safety_maximizer", "strategy": "normal_burn",
            "delta_v_mps": 14.0, "target_miss_distance_km": 3.2,
            "rationale": f"margins first: fourteen m/s normal for a wide berth.{revision}",
            "cited_screening_values": dict(cited),
        }

    def reassess():
        reassess_state["calls"] += 1
        if reassess_state["calls"] <= 2:
            # Round 0 valid; critique round repeats verbatim -> loop caught.
            return {
                "strategist": "reassess", "strategy": "retrograde_burn",
                "delta_v_mps": 9.0, "target_miss_distance_km": 1.8,
                "rationale": "retrograde nine m/s balances cost and clearance.",
                "cited_screening_values": dict(cited),
            }
        return {
            "strategist": "reassess", "strategy": "hold_and_rescreen",
            "delta_v_mps": 0.0, "target_miss_distance_km": screening["miss_distance_km"],
            "rationale": "after reflection, hold and rescreen once.",
            "cited_screening_values": dict(cited),
        }

    moderator = harness.fresh_debate_moderator(
        fuel, safety, reassess,
        lambda: {"winner": "safety_maximizer", "justification": "largest honest margin",
                 "tradeoffs_rejected": ["fuel economy", "further rescreen delay"]},
    )

    started = time.perf_counter()
    outcome, trace_id = await harness.run_debate(moderator, screening)
    elapsed = time.perf_counter() - started

    doc = await harness.memory_bank.get_doc("debate_transcripts", trace_id)
    stall_flags = [f for f in (doc or {}).get("flags", []) if f["code"] == "STALLED"]

    checks.append(harness.require("loop_detected", bool(stall_flags),
                                  str([f["code"] for f in (doc or {}).get("flags", [])])))
    checks.append(harness.check("looper_identified",
                                "reassess" in " ".join(f["detail"] for f in stall_flags)))
    checks.append(harness.require("debate_still_produced_winner",
                                  outcome is not None and not outcome.get("fallback_used"),
                                  str(outcome)))
    if outcome:
        checks.append(harness.check("judge_adjudicated_divergence",
                                    outcome.get("judge_used") is True))
        checks.append(harness.check("winner_is_valid_strategist",
                                    outcome.get("strategist") in ("fuel_minimizer", "safety_maximizer"),
                                    str(outcome.get("strategist"))))
    checks.append(harness.check("completed_within_budget", elapsed < 45.0, f"{elapsed:.2f}s"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
