"""Evaluation 5 — Policy Ceiling: an 80 m/s ask cannot slip through.

A HIGH-risk mission whose negotiation proposes an 80 m/s dodge, far above
the 50 m/s hard ceiling shared by the LLM prompts and Model Armour. The
deterministic sweep must REJECT with POLICY_CEILING_EXCEEDED regardless of
what any LLM said.
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

NAME = "policy_ceiling"
DESCRIPTION = "80 m/s negotiated burn vs 50 m/s ceiling → POLICY_CEILING_EXCEEDED, mission blocked"


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "LANCASTER_ORBIT_1", "FENGYUN_1C_DEB"
    specialists = harness.scripted_specialists(
        triage_factory=lambda: triage_payload(sat, debris),
        astro_factory=real_screening_payload(sat, debris),  # recommended 8.0 — ceiling breach is the point
        diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=80.0),
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
        "ceiling_violation",
        any(v.startswith("POLICY_CEILING_EXCEEDED") for v in violations),
        str(violations),
    ))
    checks.append(harness.check(
        "ceiling_check_failed",
        inspections and "FAIL" in str(inspections[0]["payload"]["checks"].get("policy_ceiling", "")),
        str(inspections[0]["payload"]["checks"]) if inspections else "no inspection",
    ))

    fuel_after = await harness.satellite_fuel(sat)
    checks.append(harness.check("no_fuel_debit_on_block", abs(fuel_after - 100.0) < 1e-9, f"fuel={fuel_after}%"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
