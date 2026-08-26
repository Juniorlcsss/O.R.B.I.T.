"""CHAOS — kill the astrodynamics specialist mid-fleet.

DESTRUCTIVE TEST — do not run against a production service.

The astrodynamics specialist's provider dies on every invocation during a
HIGH-risk mission (the flight analogue of the orbital-mechanics compute
node losing power). The pipeline must retry under breaker policy, trip,
open structured human dispatch and terminate in HUMAN_DISPATCH_DEGRADED —
never guess a risk band, never touch fuel, never authorise anything.

Run:  python tests/chaos/chaos_kill_agent.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.orchestrator as orchestrator_module  # noqa: E402
from tests.evaluation.harness import EvaluationHarness, FailingAgent, ScriptedAgent, triage_payload  # noqa: E402

NAME = "kill_astrodynamics_specialist"
IS_DESTRUCTIVE = True


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "LANCASTER_ORBIT_1", "FENGYUN_1C_DEB"

    original_backoff = orchestrator_module.BREAKER_BACKOFF_SECONDS
    orchestrator_module.BREAKER_BACKOFF_SECONDS = (0.01, 0.01, 0.01)
    try:
        live_triage = ScriptedAgent(
            name="alert_triage",
            output_key="orbit_alert_triage",
            payload_factory=lambda: triage_payload(sat, debris),
        )
        dead_astro = FailingAgent(name="astrodynamics_specialist", error_message="CHAOS: compute node destroyed mid-orbit")
        pipeline = harness.build_pipeline(live_triage, dead_astro)
        outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})
    finally:
        orchestrator_module.BREAKER_BACKOFF_SECONDS = original_backoff

    checks.append(harness.require("degrades_not_guesses", outcome.final_status == "HUMAN_DISPATCH_DEGRADED", outcome.final_status))
    trips = [e for e in outcome.audit_events if e["event_type"] == "CIRCUIT_BREAKER_TRIPPED"]
    checks.append(harness.check("breaker_tripped_for_dead_agent", trips and trips[0]["agent_name"] == "astrodynamics_specialist", str(trips)))
    checks.append(harness.check("no_fuel_touched", abs(await harness.satellite_fuel(sat) - 100.0) < 1e-9))

    dispatch = outcome.state.get("orbit_human_dispatch_payload")
    checks.append(harness.check("operator_paged_with_reason", bool(dispatch and dispatch.get("reason"))))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    print(f"[{'PASS' if all(r.passed for r in results) else 'FAIL'}] chaos/{NAME}")
    for r in results:
        print(("  ✓" if r.passed else "  ✗"), r.name, r.detail)
    sys.exit(0 if all(r.passed for r in results) else 1)
