"""Evaluation (Phase 11) — when every strategist settles, the debate concludes.

The loop detector freezes a strategist that restates its position verbatim.
That is right when one voice stalls while the others keep moving. It was
badly wrong when the *last* remaining voices all restate in the same round:
the survivor pool emptied, three fully validated proposals were thrown away,
and the debate reported ``fallback_used=True`` — the single-specialist
recommendation, as though the panel had failed.

Observed live on a real conjunction: all three strategists were frozen in
one round and the debate fell back every time, so the panel never actually
influenced a mission.

Three strategists holding firm under critique is a debate *finishing*, not
breaking down. The freeze still applies — nobody is asked again, so a stall
cannot burn the round or wall-clock budget — but the positions they settled
on go to the judge, which is what they were produced for.

This test pins that distinction. The companion case (one looper, others
still revising, looper must not win) is ``test_debate_loop_caught.py``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import EvaluationHarness, astro_screening_fixture  # noqa: E402

NAME = "debate_all_settle"
DESCRIPTION = "All three strategists restate → SETTLED, judge adjudicates the settled proposals (no fallback)"

STRATEGISTS = ("fuel_minimizer", "safety_maximizer", "reassess")


async def execute(harness: EvaluationHarness):
    checks = []
    screening = astro_screening_fixture()
    cited = {
        "pc": screening["pc"],
        "miss_distance_km": screening["miss_distance_km"],
        "tca_iso": screening["tca_iso"],
        "recommended_dv_mps": screening["recommended_dv_mps"],
    }

    def fixed(name, strategy, dv, target, rationale):
        def factory():
            return {
                "strategist": name, "strategy": strategy, "delta_v_mps": dv,
                "target_miss_distance_km": target, "rationale": rationale,
                "cited_screening_values": dict(cited),
            }
        return factory

    moderator = harness.fresh_debate_moderator(
        fixed("fuel_minimizer", "prograde_burn", 5.0, 0.9, "five m/s is enough; propellant is life."),
        fixed("safety_maximizer", "normal_burn", 14.0, 3.2, "fourteen m/s buys a margin worth having."),
        fixed("reassess", "retrograde_burn", 9.0, 1.8, "nine m/s retrograde splits the difference."),
        lambda: {"winner": "safety_maximizer", "justification": "largest honest margin",
                 "tradeoffs_rejected": ["fuel economy", "split-the-difference reasoning"]},
    )

    started = time.perf_counter()
    outcome, trace_id = await harness.run_debate(moderator, screening)
    elapsed = time.perf_counter() - started

    doc = await harness.memory_bank.get_doc("debate_transcripts", trace_id) or {}
    codes = [f["code"] for f in doc.get("flags", [])]

    checks.append(harness.require("debate_produced_an_outcome", outcome is not None, str(outcome)))

    # The whole point: three validated proposals must not be discarded.
    checks.append(harness.require(
        "did_not_fall_back",
        outcome is not None and not outcome.get("fallback_used"),
        f"fallback_used={outcome.get('fallback_used') if outcome else 'n/a'} flags={codes}",
    ))

    checks.append(harness.check("all_three_stalled", codes.count("STALLED") == 3, str(codes)))
    checks.append(harness.check("settled_flag_recorded", "SETTLED" in codes, str(codes)))

    # A settled debate is still adjudicated on its merits, by the judge.
    checks.append(harness.check(
        "judge_adjudicated", bool(outcome and outcome.get("judge_used")), str(outcome and outcome.get("judge_used")),
    ))
    checks.append(harness.check(
        "winner_is_a_real_strategist",
        bool(outcome) and outcome.get("strategist") in STRATEGISTS,
        str(outcome and outcome.get("strategist")),
    ))

    # Flags must name the round they occurred in. This carried len(rounds) —
    # an argument count — so a 2-round debate emitted flags labelled round 5.
    stall_rounds = [f["round"] for f in doc.get("flags", []) if f["code"] == "STALLED"]
    checks.append(harness.check(
        "flag_rounds_are_round_indices",
        bool(stall_rounds) and all(0 <= r <= 2 for r in stall_rounds),
        f"rounds={stall_rounds}",
    ))

    # Every strategist frozen means the next round would invoke nobody, so the
    # moderator must stop rather than spend the remaining rounds discovering it.
    checks.append(harness.check("stopped_promptly", elapsed < 30.0, f"{elapsed:.2f}s"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
