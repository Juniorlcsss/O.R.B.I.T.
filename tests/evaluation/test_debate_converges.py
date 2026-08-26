"""Evaluation (Phase 11) — the debate converges and the transcript persists.

All three strategists land on a prograde burn within epsilon in round 0 →
numeric convergence, no judge needed, transcript persisted with three
opening arguments.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import EvaluationHarness, astro_screening_fixture  # noqa: E402

NAME = "debate_converges"
DESCRIPTION = "Three strategists converge on an ~8 m/s prograde burn in round 0"


async def execute(harness: EvaluationHarness):
    checks = []
    screening = astro_screening_fixture()
    cited = {
        "pc": screening["pc"],
        "miss_distance_km": screening["miss_distance_km"],
        "tca_iso": screening["tca_iso"],
        "recommended_dv_mps": screening["recommended_dv_mps"],
    }

    def make(strategist, dv, target, rationale):
        return lambda: {
            "strategist": strategist, "strategy": "prograde_burn",
            "delta_v_mps": dv, "target_miss_distance_km": target,
            "rationale": rationale, "cited_screening_values": dict(cited),
        }

    moderator = harness.fresh_debate_moderator(
        make("fuel_minimizer", 7.8, 0.9, "smallest credible burn"),
        make("safety_maximizer", 8.2, 2.0, "comfortable margin"),
        make("reassess", 8.0, 1.5, "burn is justified; evidence strong"),
        lambda: {"winner": "safety_maximizer", "justification": "unused when converged", "tradeoffs_rejected": []},
    )

    outcome, trace_id = await harness.run_debate(moderator, screening)

    checks.append(harness.require("outcome_committed", outcome is not None))
    if outcome:
        checks.append(harness.check("converged", outcome["converged"] is True))
        checks.append(harness.check("no_fallback_needed", outcome["fallback_used"] is False))
        checks.append(harness.check("judge_skipped_on_consensus", outcome["judge_used"] is False))
        checks.append(harness.check("delta_v_is_8ish", abs(float(outcome["delta_v_mps"]) - 8.0) <= 0.5,
                                    str(outcome["delta_v_mps"])))
        checks.append(harness.check("downstream_payload_shape",
                                    outcome.get("action") == "we_dodge" and "our_dv_mps" in outcome))

    doc = await harness.memory_bank.get_doc("debate_transcripts", trace_id)
    checks.append(harness.require("transcript_persisted", doc is not None))
    if doc:
        checks.append(harness.check("transcript_has_three_openers", len(doc["rounds"]) == 3,
                                    f"{len(doc['rounds'])} arguments"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
