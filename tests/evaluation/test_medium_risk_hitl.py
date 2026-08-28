"""Evaluation 3 — MEDIUM-risk conjunction: advisory only, human decides.

Input:  A screening answer inside the MEDIUM band (Pc between 1e-6 and
        1e-4).
Expect: HELD_FOR_HUMAN_REVIEW with the human-in-the-loop flag set, an
        advisory armour review present, and absolutely no autonomous
        execution or fuel change.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    negotiation_payload,
    screening_payload,
    triage_payload,
    verdict_payload,
)

NAME = "medium_risk_hitl"
DESCRIPTION = "MEDIUM band held for human review — advisory logged, zero autonomous execution"


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "SIM_PROTECTED_ASSET", "COSMOS_2251_DEB"
    specialists = harness.scripted_specialists(
        triage_factory=lambda: triage_payload(sat, debris),
        astro_factory=lambda: screening_payload("MEDIUM", pc=5e-6, miss_distance_km=2.5, recommended_dv_mps=0.0, dv_direction="none"),
        diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=5.0),
        safety_factory=lambda: verdict_payload(True),
    )
    pipeline = harness.build_pipeline(*specialists)
    outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})

    checks.append(harness.require(
        "final_status", outcome.final_status == "HELD_FOR_HUMAN_REVIEW", outcome.final_status,
    ))

    decision = outcome.state.get("orbit_execution_decision") or {}
    checks.append(harness.check(
        "hitl_flag_set",
        decision.get("human_in_loop_required") is True and decision.get("decision") == "HELD_FOR_HUMAN_REVIEW",
        str(decision),
    ))
    checks.append(harness.check(
        "advisory_review_present",
        isinstance(decision.get("armor_advisory_available"), bool) and bool(decision.get("armor_advisory_available")),
        f"advisory_available={decision.get('armor_advisory_available')}",
    ))

    types_seen = {e["event_type"] for e in outcome.audit_events}
    checks.append(harness.check(
        "advisory_path_skips_deterministic_sweep",
        "MANEUVER_INSPECTION" not in types_seen,
        f"deterministic sweep must only run pre-execution: {sorted(types_seen)}",
    ))
    checks.append(harness.check("no_execution_authorised", "MISSION_TRIGGERED_FROM_WATCH" not in types_seen))

    fuel_after = await harness.satellite_fuel(sat)
    checks.append(harness.check("no_fuel_change", abs(fuel_after - 100.0) < 1e-9, f"fuel={fuel_after}%"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
