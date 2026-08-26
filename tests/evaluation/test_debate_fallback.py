"""Evaluation (Phase 11) — total debate collapse falls back gracefully.

All three strategists cite fabricated screening values → every proposal is
discarded as HALLUCINATION before selection. The moderator must fall back
to the deterministic single-specialist recommendation (fallback_used=True)
so the mission continues with exactly the pre-debate behaviour.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import EvaluationHarness, astro_screening_fixture  # noqa: E402

NAME = "debate_fallback"
DESCRIPTION = "All proposals hallucinated → fallback_used=True, legacy recommendation returned"


async def execute(harness: EvaluationHarness):
    checks = []
    screening = astro_screening_fixture(recommended_dv_mps=8.0)
    fake_cited = {"pc": 1e-9, "miss_distance_km": 999.0, "tca_iso": "1970-01-01T00:00:00Z",
                  "recommended_dv_mps": 0.0}

    def make(strategist, dv):
        return lambda: {
            "strategist": strategist, "strategy": "prograde_burn" if dv else "hold_and_rescreen",
            "delta_v_mps": dv, "target_miss_distance_km": 2.0,
            "rationale": f"{strategist} argues from numbers that do not exist.",
            "cited_screening_values": dict(fake_cited),
        }

    moderator = harness.fresh_debate_moderator(
        make("fuel_minimizer", 4.0),
        make("safety_maximizer", 20.0),
        lambda: {"strategist": "reassess", "strategy": "hold_and_rescreen",
                 "delta_v_mps": 0.0, "target_miss_distance_km": 2.0,
                 "rationale": "hold.", "cited_screening_values": dict(fake_cited)},
        lambda: {"winner": "nobody_real", "justification": "judge should never be reached",
                 "tradeoffs_rejected": []},
    )

    outcome, trace_id = await harness.run_debate(moderator, screening)

    checks.append(harness.require("outcome_committed", outcome is not None))
    if outcome:
        checks.append(harness.require("fallback_used", outcome.get("fallback_used") is True))
        checks.append(harness.check(
            "legacy_recommendation_restored",
            abs(float(outcome["delta_v_mps"]) - float(screening["recommended_dv_mps"])) < 1e-9
            and outcome["strategy"] != "hold_and_rescreen",
            f"dv={outcome['delta_v_mps']} vs recommended {screening['recommended_dv_mps']}",
        ))

    doc = await harness.memory_bank.get_doc("debate_transcripts", trace_id)
    flags = (doc or {}).get("flags", [])
    hallucination_count = sum(1 for f in flags if f["code"] == "HALLUCINATION" and f["severity"] == "CRITICAL")
    fallback_flags = [f for f in flags if f["code"] == "FALLBACK"]
    checks.append(harness.check("three_hallucinations_flagged", hallucination_count >= 3, f"{hallucination_count}"))
    checks.append(harness.check("fallback_audited_as_warning", bool(fallback_flags)))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
