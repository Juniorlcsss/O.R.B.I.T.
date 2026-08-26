"""Evaluation 8 — Circuit breaker: a dead provider degrades, never guesses.

The alert-triage specialist is replaced with an agent whose provider always
raises. The pipeline must retry exactly three times (with backoff shortened
for test speed), trip the breaker, open structured human dispatch and
terminate in HUMAN_DISPATCH_DEGRADED — with the TRIPPED audit record proving
the retry count.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.orchestrator as orchestrator_module  # noqa: E402
from agents.orchestrator import (  # noqa: E402
    NEGOTIATION_OUTPUT_KEY,
    SCREENING_OUTPUT_KEY,
    VERDICT_OUTPUT_KEY,
)
from tests.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    FailingAgent,
    ScriptedAgent,
    negotiation_payload,
    real_screening_payload,
    verdict_payload,
)

NAME = "circuit_breaker"
DESCRIPTION = "Dead triage provider → 3 attempts with backoff → breaker trips → HUMAN_DISPATCH_DEGRADED"


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "LANCASTER_ORBIT_1", "FENGYUN_1C_DEB"

    original_backoff = orchestrator_module.BREAKER_BACKOFF_SECONDS
    orchestrator_module.BREAKER_BACKOFF_SECONDS = (0.01, 0.01, 0.01)  # speed: behaviour unchanged
    try:
        astro = ScriptedAgent(
            name="astrodynamics_specialist", output_key=SCREENING_OUTPUT_KEY, payload_factory=real_screening_payload(sat, debris)
        )
        diplomat = ScriptedAgent(
            name="negotiation_officer",
            output_key=NEGOTIATION_OUTPUT_KEY,
            payload_factory=lambda: negotiation_payload("we_dodge", 8.0),
        )
        safety = ScriptedAgent(name="safety_officer", output_key=VERDICT_OUTPUT_KEY, payload_factory=verdict_payload(True))
        dead_triage = FailingAgent(name="alert_triage", error_message="simulated LLM endpoint outage")

        pipeline = harness.build_pipeline(triage=dead_triage, astro=astro, diplomat=diplomat, safety=safety)
        started = time.perf_counter()
        outcome = await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})
        elapsed = time.perf_counter() - started
    finally:
        orchestrator_module.BREAKER_BACKOFF_SECONDS = original_backoff

    checks.append(harness.require(
        "final_status", outcome.final_status == "HUMAN_DISPATCH_DEGRADED", outcome.final_status,
    ))

    trips = [e for e in outcome.audit_events if e["event_type"] == "CIRCUIT_BREAKER_TRIPPED"]
    checks.append(harness.require("breaker_tripped_once", len(trips) == 1, f"trips={len(trips)}"))
    if trips:
        checks.append(harness.check(
            "three_attempts_recorded",
            trips[0]["status"] == "TRIPPED" and trips[0]["payload"].get("attempts") == 3,
            str(trips[0]["payload"]),
        ))
        checks.append(harness.check("tripped_agent_is_triage", trips[0]["agent_name"] == "alert_triage", trips[0]["agent_name"]))

    dispatch = outcome.state.get("orbit_human_dispatch_payload")
    checks.append(harness.check(
        "structured_dispatch_opened",
        isinstance(dispatch, dict) and dispatch.get("level") == "CRITICAL" and "triage" in str(dispatch.get("reason", "")).lower(),
        str(dispatch.get("reason", ""))[:80],
    ))
    checks.append(harness.check("fast_with_patched_backoff", elapsed < 5.0, f"{elapsed:.2f}s"))
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
