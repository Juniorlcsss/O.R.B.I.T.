"""Evaluation — negotiation is skipped when the counterparty cannot manoeuvre.

Uncontrolled debris has no operator and no propulsion. Asking it to yield
right-of-way is meaningless, and worse, an LLM negotiation officer handed
that situation can return ``standoff`` — a deadlock with an object that
holds no position — which then escalates a HIGH-risk conjunction to a human
for arbitration that no human can perform either.

This is the common case in real conjunction data, so the fleet must decide
it structurally rather than hope the model reasons its way there. The
mission must skip the negotiation stage, record why, and proceed to
unilateral avoidance with the safety gates still fully in force.

The paired case — a manoeuvrable counterparty still reaching negotiation —
is covered by the guard tests (hallucination, PII, policy ceiling), which
all run against ``SIM_COORDINATION_TARGET``.
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

NAME = "debris_skips_negotiation"
DESCRIPTION = "Debris counterparty → negotiation stage skipped, unilateral avoidance, gates intact"


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "SIM_PROTECTED_ASSET", "FENGYUN_1C_DEB"

    specialists = harness.scripted_specialists(
        triage_factory=lambda: triage_payload(sat, debris),
        astro_factory=real_screening_payload(sat, debris),
        diplomat_factory=lambda: negotiation_payload("standoff", our_dv=0.0),
        safety_factory=lambda: verdict_payload(True),
    )
    pipeline = harness.build_pipeline(*specialists)
    outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})

    skipped = [e for e in outcome.audit_events if e["event_type"] == "NEGOTIATION_SKIPPED"]
    checks.append(harness.require(
        "negotiation_skipped_event", bool(skipped),
        str([e["event_type"] for e in outcome.audit_events][:12]),
    ))
    checks.append(harness.check(
        "reason_is_counterparty_cannot_manoeuvre",
        bool(skipped) and skipped[0]["payload"].get("reason") == "COUNTERPARTY_CANNOT_MANOEUVRE",
        skipped[0]["payload"].get("reason") if skipped else "no event",
    ))

    # The scripted standoff must not have reached the mission outcome.
    checks.append(harness.check(
        "did_not_end_in_standoff",
        outcome.final_status != "HIGH_RISK_STANDOFF_HUMAN_DISPATCH",
        outcome.final_status,
    ))

    # The negotiation officer must not have been invoked at all.
    invocations = [
        e for e in outcome.audit_events
        if e["event_type"] == "AGENT_INVOCATION"
        and e.get("agent_name") == "negotiation_officer"
    ]
    checks.append(harness.check(
        "negotiation_officer_never_invoked", not invocations,
        f"{len(invocations)} invocation(s)",
    ))

    # Skipping negotiation must not skip the safety pipeline: Model Armour
    # still has to inspect the burn before anything is executed.
    inspections = [e for e in outcome.audit_events if e["event_type"] == "MANEUVER_INSPECTION"]
    checks.append(harness.check(
        "armor_still_inspected", bool(inspections),
        f"{len(inspections)} inspection(s)",
    ))

    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
