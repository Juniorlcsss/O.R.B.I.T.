"""Evaluation 1 — calibrated HIGH-risk conjunction, happy path to uplink.

Input:  LANCASTER_ORBIT_1 × FENGYUN_1C_DEB (the calibrated scenario whose
        real SGP4 screening lands in the HIGH band).
Expect: EXECUTION_AUTHORIZED, fuel decremented by the executed burn,
        conjunction persisted with its final status, all four Model Armour
        checks PASS and a complete audit chain under one trace ID.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from geap_sim.observability import audit_logger  # noqa: E402
from tests.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    negotiation_payload,
    real_screening_payload,
    triage_payload,
    verdict_payload,
)

NAME = "high_risk_conjunction"
DESCRIPTION = "Calibrated HIGH pair runs end-to-end: authorised, fuel debited, logged, armour all-PASS"

DV_MPS = 8.0


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "SIM_PROTECTED_ASSET","FENGYUN_1C_DEB"
    specialists = harness.scripted_specialists(
        triage_factory=lambda: triage_payload(sat, debris),
        astro_factory=real_screening_payload(sat, debris),
        diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=DV_MPS),
        safety_factory=lambda: verdict_payload(True),
    )
    pipeline = harness.build_pipeline(*specialists)
    outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris, "priority": "URGENT"})

    checks.append(harness.require(
        "final_status", outcome.final_status == "EXECUTION_AUTHORIZED",
        f"expected EXECUTION_AUTHORIZED, got {outcome.final_status}",
    ))

    inspections = [e for e in outcome.audit_events if e["event_type"] == "MANEUVER_INSPECTION"]
    checks.append(harness.require("armor_inspection_ran", len(inspections) == 1, f"found {len(inspections)}"))
    if inspections:
        payload_checks = inspections[0]["payload"]["checks"]
        passed_all = all(str(v).startswith("PASS") for v in payload_checks.values())
        checks.append(harness.check(
            "armor_all_four_checks_pass",
            passed_all and set(payload_checks) == {"hallucination_guard", "policy_ceiling", "fuel_guard", "secret_scan"},
            str(payload_checks),
        ))
        checks.append(harness.check(
            "armor_status_approved", inspections[0]["status"] == "APPROVED", inspections[0]["status"],
        ))

    fuel_after = await harness.satellite_fuel(sat)
    expected_fuel = round(100.0 - DV_MPS * 0.5, 4)
    checks.append(harness.require(
        "fuel_decremented",
        abs(fuel_after - expected_fuel) < 1e-6,
        f"expected {expected_fuel}%, got {fuel_after}%",
    ))

    decision = outcome.state.get("orbit_execution_decision") or {}
    cid = decision.get("conjunction_id")
    doc = await harness.read_conjunction(cid) if cid else None
    checks.append(harness.require("conjunction_persisted", doc is not None, f"no doc for {cid}"))
    if doc:
        checks.append(harness.check(
            "conjunction_final_status_recorded",
            doc.get("final_status") == "EXECUTION_AUTHORIZED" and float(doc.get("our_dv_mps") or 0) == DV_MPS,
            str({k: doc.get(k) for k in ("final_status", "our_dv_mps")}),
        ))

    types_seen = {e["event_type"] for e in outcome.audit_events}
    required_stages = {"MISSION_STATUS", "MANEUVER_INSPECTION"}
    checks.append(harness.check(
        "audit_chain_complete",
        required_stages <= types_seen
        and all(e["trace_id"] == outcome.trace_id for e in outcome.audit_events),
        f"{len(outcome.audit_events)} events; stages={sorted(types_seen)}",
    ))

    # The authorised mission queues an autonomous Veo debrief in the
    # background — give it a moment, then prove it landed on the same trace.
    await asyncio.sleep(0.6)
    late_types = {e["event_type"] for e in audit_logger.get_events_by_trace(outcome.trace_id)}
    checks.append(harness.check(
        "debrief_autonomously_generated",
        {"DEBRIEF_QUEUED", "DEBRIEF_READY"} <= late_types,
        sorted(late_types),
    ))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
