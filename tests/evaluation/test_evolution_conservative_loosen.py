"""Evaluation (Phase 10) — conservative loosening on genuine over-reaction evidence.

Seeds 12 clearly-marked synthetic over-reactions, proposes a modest
pc_high_threshold raise, and expects the full learning loop to APPLY it:
evidence-justified direction, magnitude inside the step cap, Meta-Critic
approval, clamp clean, diff persisted to the evolution history.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.outcome import OutcomeSimulator  # noqa: E402
from tests.evaluation.harness import EvaluationHarness  # noqa: E402

NAME = "evolution_conservative_loosen"
DESCRIPTION = "12 over-reactions → evidence-backed pc_high raise applied, diff logged"


async def execute(harness: EvaluationHarness):
    checks = []
    await OutcomeSimulator(harness.memory_bank).seed_over_reactions(12)

    def analyst():
        return {
            "proposed_policy": {
                "pc_high_threshold": 1.6e-4, "pc_medium_threshold": 1e-6,
                "preferred_miss_distance_km": 1.0, "delta_v_efficiency_bias": 0.5,
                "fuel_reserve_floor_pct": 5.0,
            },
            "no_change": False, "reasoning": "12/12 recent outcomes were over-reactions at miss >8 km",
            "confidence": 0.85, "expected_effect": "~fewer HIGH alerts for harmless encounters",
        }

    def critic():
        return {"verdict": "APPROVE", "reasoning": "direction and magnitude are proportionate",
                "gaming_suspicion_score": 0.15, "safety_concerns": []}

    before = await harness.policy_store.load()
    report = await harness.run_evolution_cycle(harness.fresh_evolution_engine(analyst, critic))
    after = await harness.policy_store.load()

    checks.append(harness.require(
        "applied", report["status"] in ("APPLIED", "CLAMPED_APPLIED"),
        f"{report['status']}: {report.get('reasoning', '')[:120]}",
    ))
    checks.append(harness.check("threshold_loosened", float(after.pc_high_threshold) > float(before.pc_high_threshold),
                                f"{before.pc_high_threshold:.3g} -> {after.pc_high_threshold:.3g}"))
    checks.append(harness.check("diff_logged", len(report.get("diff_summary", [])) >= 1, str(report.get("diff_summary"))))

    history = await harness.memory_bank.get_evolution_history(5)
    checks.append(harness.check("cycle_persisted", any(c.get("applied") for c in history), f"{len(history)} cycles"))
    checks.append(harness.check("version_bumped", after.policy_version == before.policy_version + 1,
                                f"v{before.policy_version} -> v{after.policy_version}"))

    from geap_sim.observability import audit_logger
    applied_events = [e for e in audit_logger.get_events_since(0)
                      if e["event_type"] == "CYCLE_APPLIED" and e["trace_id"] == report["trace_id"]]
    checks.append(harness.check("audit_cycle_applied_on_trace", bool(applied_events)))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
