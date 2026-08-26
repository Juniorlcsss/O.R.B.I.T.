"""Evaluation (Phase 10) — the hard envelope clamps even APPROVED proposals.

An approved proposal that steps past the per-cycle cap on two parameters
(pc_high_threshold beyond the 20 % step, preferred_miss_distance_km far
beyond it) must come out of clamp_to_envelope inside the envelope with
clamp_actions recorded — proving the deterministic boundary runs even when
every reviewer said yes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.outcome import OutcomeSimulator  # noqa: E402
from evolution.policy import EVOLUTION_ENVELOPE, ScreeningPolicy, validate_invariants  # noqa: E402
from tests.evaluation.harness import EvaluationHarness  # noqa: E402

NAME = "evolution_envelope_clamp"
DESCRIPTION = "Approved over-cap proposal → CLAMPED_APPLIED with recorded clamp actions"


async def execute(harness: EvaluationHarness):
    checks = []
    await OutcomeSimulator(harness.memory_bank).seed_over_reactions(8)

    def analyst():
        return {
            "proposed_policy": {
                "pc_high_threshold": 3.0e-4, "pc_medium_threshold": 1e-6,
                "preferred_miss_distance_km": 3.0, "delta_v_efficiency_bias": 0.5,
                "fuel_reserve_floor_pct": 5.0,
            },
            "no_change": False,
            "reasoning": "raise the HIGH bar and widen the preferred cushion",
            "confidence": 0.9, "expected_effect": "calibrated loosening",
        }

    def critic():
        return {"verdict": "APPROVE", "reasoning": "direction is evidence-backed",
                "gaming_suspicion_score": 0.2, "safety_concerns": []}

    report = await harness.run_evolution_cycle(harness.fresh_evolution_engine(analyst, critic))

    checks.append(harness.require(
        "clamped_applied", report["status"] in ("CLAMPED_APPLIED", "APPLIED"),
        f"{report['status']}: {report.get('reasoning', '')[:120]}",
    ))
    checks.append(harness.check("clamp_actions_recorded", len(report.get("clamp_actions", [])) >= 1,
                                str(report.get("clamp_actions"))[:200]))

    after_fields = {k: v for k, v in (report.get("after") or {}).items() if k in EVOLUTION_ENVELOPE}
    final_policy = ScreeningPolicy(**after_fields)
    violations = validate_invariants(final_policy)
    checks.append(harness.check("final_inside_envelope", not violations, str(violations)))
    checks.append(harness.check("provenance_marked_clamped", after_fields and (report["after"].get("provenance") == "clamped") == bool(report["clamp_actions"]),
                                str(report["after"].get("provenance"))))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
