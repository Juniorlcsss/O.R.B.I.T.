"""Evaluation (Phase 10) — the evolution freeze circuit-breaker.

Three consecutive rejected cycles must trip the freeze; subsequent triggers
return FROZEN_HUMAN_REVIEW without invoking any LLM work that matters; and
a manual unfreeze restores normal operation. Freeze state is persisted in
the memory bank, so it survives restarts exactly like watches do.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.outcome import OutcomeSimulator  # noqa: E402
from tests.evaluation.harness import EvaluationHarness  # noqa: E402

NAME = "evolution_freeze"
DESCRIPTION = "3 consecutive rejections → FROZEN_HUMAN_REVIEW; manual unfreeze restores operation"


async def execute(harness: EvaluationHarness):
    checks = []
    await OutcomeSimulator(harness.memory_bank).seed_over_reactions(6)

    def analyst():
        return {
            "proposed_policy": {
                "pc_high_threshold": 9.0e-4, "pc_medium_threshold": 1e-6,
                "preferred_miss_distance_km": 1.0, "delta_v_efficiency_bias": 0.5,
                "fuel_reserve_floor_pct": 5.0,
            },
            "no_change": False, "reasoning": "aggressive loosen attempt",
            "confidence": 0.9, "expected_effect": "far fewer alerts",
        }

    def rejecting_critic():
        return {"verdict": "REJECT", "reasoning": "magnitude wildly disproportionate to 6 outcomes",
                "gaming_suspicion_score": 0.85, "safety_concerns": ["unjustified convenience drift"]}

    engine = harness.fresh_evolution_engine(analyst, rejecting_critic)

    statuses = []
    for _ in range(3):
        report = await harness.run_evolution_cycle(engine)
        statuses.append(report["status"])
    print("       cycle statuses:", statuses)
    checks.append(harness.require(
        "freeze_engaged_within_three_rejections",
        "FROZEN_HUMAN_REVIEW" in statuses, str(statuses),
    ))

    # While frozen, a fresh trigger is blocked immediately.
    blocked = await harness.run_evolution_cycle(engine)
    checks.append(harness.check("still_frozen_on_next_trigger", blocked["status"] == "FROZEN_HUMAN_REVIEW", blocked["status"]))

    # Manual human unfreeze (same action POST /api/evolution/unfreeze performs).
    await harness.memory_bank.set_meta("evolution_frozen", False)
    await harness.memory_bank.set_meta("evolution_rejection_counter", 0)
    await harness.memory_bank.set_meta("evolution_envelope_push_counter", 0)

    def approving_critic():
        return {"verdict": "APPROVE", "reasoning": "operator reviewed the evidence",
                "gaming_suspicion_score": 0.1, "safety_concerns": []}

    recovered = await harness.run_evolution_cycle(harness.fresh_evolution_engine(analyst, approving_critic))
    checks.append(harness.check("unfreeze_restores_operation",
                                recovered["status"] not in ("FROZEN_HUMAN_REVIEW",), recovered["status"]))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
