"""CHAOS — corrupt the persistent satellite state and see what survives.

DESTRUCTIVE TEST — writes garbage into the memory bank.

Seeds three corrupted vehicle records — negative fuel, NaN fuel, a
non-numeric string in the delta-v counter — then drives a full we-dodge
mission. The system must (a) not crash, (b) sanitise every read back into
physically-sane ranges (audited via SATELLITE_STATE_CORRUPTED_SANITISED)
and (c) keep Model Armour arithmetic finite so the armour verdict stays
trustworthy.

Run:  python tests/chaos/chaos_corrupt_state.py
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from geap_sim.memory_bank import SATELLITES_COLLECTION  # noqa: E402
from tests.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    negotiation_payload,
    real_screening_payload,
    triage_payload,
    verdict_payload,
)

NAME = "corrupt_satellite_state"
IS_DESTRUCTIVE = True


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "LANCASTER_ORBIT_1", "FENGYUN_1C_DEB"
    from geap_sim.observability import audit_logger
    from geap_sim.memory_bank import _DEFAULT_STATE_TEMPLATE

    # Each corruption is written onto the PRIMARY satellite document — the
    # one the armour's fuel guard will actually read.
    scenarios = [
        ("NEGATIVE_FUEL", {"fuel_percentage": -42.0}, ("MANEUVER_BLOCKED_BY_ARMOR",)),          # clamps to 0% → reserve breach
        ("NAN_FUEL", {"fuel_percentage": float("nan")}, ("EXECUTION_AUTHORIZED",)),             # sanitised to 100% → burn proceeds
        ("STRING_DV", {"total_dv_expended": "garbage"}, ("EXECUTION_AUTHORIZED",)),             # counter reset to 0.0
    ]
    seq_before = audit_logger.latest_seq()

    for label, patch, acceptable in scenarios:
        await harness.memory_bank._write(
            SATELLITES_COLLECTION,
            sat,
            {**_DEFAULT_STATE_TEMPLATE, "sat_id": sat, **patch},
        )
        specialists = harness.scripted_specialists(
            triage_factory=lambda: triage_payload(sat, debris),
            astro_factory=real_screening_payload(sat, debris),  # recommended 8.0
            diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=8.0),
            safety_factory=lambda: verdict_payload(True),
        )
        pipeline = harness.build_pipeline(*specialists)
        outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})

        checks.append(harness.check(
            f"{label.lower()}_no_crash",
            outcome.final_status in ("EXECUTION_AUTHORIZED", "MANEUVER_BLOCKED_BY_ARMOR", "HUMAN_DISPATCH_DEGRADED"),
            outcome.final_status,
        ))
        checks.append(harness.check(f"{label.lower()}_handled_per_policy", outcome.final_status in acceptable, outcome.final_status))

    sanitised_events = [e for e in audit_logger.get_events_since(seq_before) if e["event_type"] == "SATELLITE_STATE_CORRUPTED_SANITISED"]
    checks.append(harness.check("corruption_detected_and_audited", len(sanitised_events) >= len(scenarios), f"{len(sanitised_events)} sanitisation events"))

    fuel_after = await harness.satellite_fuel(sat)
    checks.append(harness.check(
        "state_left_finite_and_bounded",
        math.isfinite(fuel_after) and 0.0 <= fuel_after <= 100.0,
        f"fuel={fuel_after}",
    ))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    print(f"[{'PASS' if all(r.passed for r in results) else 'FAIL'}] chaos/{NAME}")
    for r in results:
        print(("  ✓" if r.passed else "  ✗"), r.name, r.detail)
    sys.exit(0 if all(r.passed for r in results) else 1)
