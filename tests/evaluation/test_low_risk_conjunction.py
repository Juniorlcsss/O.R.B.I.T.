"""Evaluation 2 — LOW-risk conjunction: log and close, nothing else.

Input:  A screening answer far outside every risk band (Pc 1e-9,
        miss distance 1500 km).
Expect: LOGGED_NO_ACTION_REQUIRED with zero fuel change and no negotiation
        or armour activity anywhere in the audit trail.
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

NAME = "low_risk_conjunction"
DESCRIPTION = "LOW band (Pc 1e-9, miss 1500 km) logs only — no burn, no negotiation, no armour"


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "LANCASTER_ORBIT_1", "COSMOS_2251_DEB"
    specialists = harness.scripted_specialists(
        triage_factory=lambda: triage_payload(sat, debris),
        astro_factory=lambda: screening_payload("LOW", pc=1e-9, miss_distance_km=1500.0, recommended_dv_mps=0.0, dv_direction="none"),
        diplomat_factory=lambda: (_ for _ in ()).throw(AssertionError("negotiation must not be invoked for LOW risk")),
        safety_factory=lambda: verdict_payload(True),
    )
    pipeline = harness.build_pipeline(*specialists)
    outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})

    checks.append(harness.require(
        "final_status", outcome.final_status == "LOGGED_NO_ACTION_REQUIRED", outcome.final_status,
    ))

    fuel_after = await harness.satellite_fuel(sat)
    checks.append(harness.check("no_fuel_change", abs(fuel_after - 100.0) < 1e-9, f"fuel={fuel_after}%"))

    types_seen = {e["event_type"] for e in outcome.audit_events}
    checks.append(harness.check(
        "negotiation_never_invoked",
        "MANEUVER_INSPECTION" not in types_seen,
        f"unexpected armour inspection in trail: {sorted(types_seen)}",
    ))
    decision = outcome.state.get("orbit_execution_decision") or {}
    checks.append(harness.check(
        "decision_is_log_only", decision.get("decision") == "LOGGED_NO_ACTION_REQUIRED", str(decision),
    ))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
