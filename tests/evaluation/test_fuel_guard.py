"""Evaluation 6 — Strategic Fuel Guard: a 6% tank cannot fund an 8 m/s dodge.

The satellite's live memory-bank state is seeded to 6% fuel. A HIGH-risk
mission then proposes the standard 8 m/s burn (0.5 points per m/s → would
leave 2%), below the 5% strategic reserve. Model Armour must REJECT with
STRATEGIC_RESERVE_VIOLATION and the tank must remain untouched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    negotiation_payload,
    real_screening_payload,
    triage_payload,
    verdict_payload,
)

NAME = "fuel_guard"
DESCRIPTION = "6% fuel + 8 m/s dodge (projects 2% < 5% reserve) → STRATEGIC_RESERVE_VIOLATION"


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "LANCASTER_ORBIT_1", "FENGYUN_1C_DEB"

    await harness.memory_bank.update_satellite_state(sat, delta_v_expended=188.0, new_fuel=6.0)
    seeded = await harness.satellite_fuel(sat)
    checks.append(harness.require("fuel_seeded_6pct", abs(seeded - 6.0) < 1e-9, f"seeded={seeded}%"))

    specialists = harness.scripted_specialists(
        triage_factory=lambda: triage_payload(sat, debris),
        astro_factory=real_screening_payload(sat, debris),  # recommended 8.0
        diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=8.0),
        safety_factory=lambda: verdict_payload(True),
    )
    pipeline = harness.build_pipeline(*specialists)
    outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})

    checks.append(harness.require(
        "final_status", outcome.final_status == "MANEUVER_BLOCKED_BY_ARMOR", outcome.final_status,
    ))

    inspections = [e for e in outcome.audit_events if e["event_type"] == "MANEUVER_INSPECTION"]
    violations = [v for i in inspections for v in i["payload"]["violations"]]
    checks.append(harness.check(
        "reserve_violation",
        any(v.startswith("STRATEGIC_RESERVE_VIOLATION") for v in violations),
        str(violations),
    ))

    fuel_after = await harness.satellite_fuel(sat)
    checks.append(harness.check("tank_untouched", abs(fuel_after - 6.0) < 1e-9, f"fuel after={fuel_after}%"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
