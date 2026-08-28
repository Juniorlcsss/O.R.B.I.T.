"""CHAOS — rapid-fire load: 100 conjunction alerts, concurrently.

DESTRUCTIVE TEST — hammers the fleet at demo-breaking volume.

Fires 100 full missions through the real pipeline (scripted specialists,
isolated memory bank each) with unbounded concurrency. Every mission must
reach a terminal status, no coroutine may raise, and per-mission isolation
must hold (each trace gets its own outcome). Throughput is reported so
regressions in orchestration overhead are visible.

Run:  python tests/chaos/chaos_rapid_fire.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    negotiation_payload,
    real_screening_payload,
    triage_payload,
    verdict_payload,
)

NAME = "rapid_fire_100_alerts"
IS_DESTRUCTIVE = True
TOTAL_ALERTS = 100

TERMINAL_STATUSES = {
    "EXECUTION_AUTHORIZED",
    "MANEUVER_BLOCKED_BY_ARMOR",
    "REJECTED_BY_MODEL_ARMOR_OPERATOR_ALERTED",
    "HELD_FOR_HUMAN_REVIEW",
    "LOGGED_NO_ACTION_REQUIRED",
    "HUMAN_DISPATCH_DEGRADED",
    "HIGH_RISK_STANDOFF_HUMAN_DISPATCH",
    "EDGE_AUTONOMOUS_DODGE_EXECUTED",
    "EDGE_AUTONOMY_HOLD_HUMAN_DISPATCH",
}


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "SIM_PROTECTED_ASSET", "FENGYUN_1C_DEB"

    pipelines = []
    for _ in range(TOTAL_ALERTS):
        specialists = harness.scripted_specialists(
            triage_factory=lambda: triage_payload(sat, debris),
            astro_factory=real_screening_payload(sat, debris),  # real SGP4 math per mission
            diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=8.0),
            safety_factory=lambda: verdict_payload(True),
        )
        pipelines.append(harness.build_pipeline(*specialists))

    async def fire(pipeline):
        return await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})

    started = time.perf_counter()
    results = await asyncio.gather(*(fire(p) for p in pipelines), return_exceptions=True)
    elapsed = time.perf_counter() - started

    errors = [r for r in results if isinstance(r, BaseException)]
    outcomes = [r for r in results if not isinstance(r, BaseException)]
    terminal = [o for o in outcomes if o.final_status in TERMINAL_STATUSES]

    checks.append(harness.require("zero_exceptions", not errors, f"{len(errors)} raised: {errors[:1]}"))
    checks.append(harness.require(
        "all_missions_reached_terminal_status",
        len(terminal) == TOTAL_ALERTS,
        f"{len(terminal)}/{TOTAL_ALERTS} terminal ({set(o.final_status for o in outcomes if o.final_status not in TERMINAL_STATUSES)})",
    ))
    traces = {o.trace_id for o in outcomes}
    checks.append(harness.check("unique_trace_per_mission", len(traces) == TOTAL_ALERTS, f"{len(traces)} unique traces"))
    throughput = TOTAL_ALERTS / max(elapsed, 1e-9)
    print(f"       [chaos observation] {TOTAL_ALERTS} missions in {elapsed:.2f}s → {throughput:.0f} missions/s (scripted specialists)")
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    print(f"[{'PASS' if all(r.passed for r in results) else 'FAIL'}] chaos/{NAME}")
    for r in results:
        print(("  ✓" if r.passed else "  ✗"), r.name, r.detail)
    sys.exit(0 if all(r.passed for r in results) else 1)
