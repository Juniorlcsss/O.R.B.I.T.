"""Evaluation (Phase 11) Ã¢â‚¬â€ a hallucinated citation disqualifies its author.

The Safety Maximizer cites a miss distance of 42 km when reality is
0.0889 km Ã¢â‚¬â€ the deterministic hallucination check must flag it CRITICAL,
discard the proposal before selection, and the debate must still produce a
valid winner from the two honest voices.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import EvaluationHarness, astro_screening_fixture  # noqa: E402

NAME = "debate_hallucination_caught"
DESCRIPTION = "Cited miss distance 42 km vs actual 0.0889 km Ã¢â€ â€™ HALLUCINATION flag, proposal discarded"


async def execute(harness: EvaluationHarness):
    checks = []
    screening = astro_screening_fixture()
    honest_cited = {
        "pc": screening["pc"],
        "miss_distance_km": screening["miss_distance_km"],
        "tca_iso": screening["tca_iso"],
        "recommended_dv_mps": 8.0,
    }

    def make(strategist, dv, target, cited=None):
        return lambda: {
            "strategist": strategist, "strategy": "prograde_burn",
            "delta_v_mps": dv, "target_miss_distance_km": target,
            "rationale": f"{strategist} position", "cited_screening_values": dict(cited or honest_cited),
        }

    moderator = harness.fresh_debate_moderator(
        make("fuel_minimizer", 7.5, 1.0),
        make("safety_maximizer", 12.0, 3.0,
             cited={"pc": screening["pc"], "miss_distance_km": 42.0,
                    "tca_iso": screening["tca_iso"], "recommended_dv_mps": 8.0}),
        make("reassess", 8.0, 1.4),
        lambda: {"winner": "reassess", "justification": "middle course", "tradeoffs_rejected": ["thin margin"]},
    )

    outcome, trace_id = await harness.run_debate(moderator, screening)

    doc = await harness.memory_bank.get_doc("debate_transcripts", trace_id)
    flags = [f["code"] for f in (doc or {}).get("flags", []) if f["severity"] == "CRITICAL"]
    details = " ".join(f["detail"] for f in (doc or {}).get("flags", []))

    checks.append(harness.require("hallucination_flagged", "HALLUCINATION" in flags, str(flags)))
    checks.append(harness.check("named_the_liar", "safety_maximizer" in details))
    checks.append(harness.require("outcome_still_valid", outcome is not None and outcome["fallback_used"] is False,
                                  str(outcome)))
    if outcome:
        checks.append(harness.check(
            "winner_among_honest_voices",
            outcome["strategist"] in ("fuel_minimizer", "reassess") or abs(float(outcome["delta_v_mps"]) - 12.0) > 1e-9,
            str(outcome["strategist"]),
        ))
        checks.append(harness.check("hallucinated_dv_not_selected",
                                    abs(float(outcome["delta_v_mps"]) - 12.0) > 1e-9, str(outcome["delta_v_mps"])))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
