"""CHAOS — network partition between the fleet and a specialist provider.

DESTRUCTIVE TEST — simulates transport-layer failures.

Two failure modes are exercised against the astrodynamics specialist:

1. **Refused connections** — the provider socket rejects instantly
   (``ConnectionError``). Expected: breaker retries, trips, mission
   degrades to HUMAN_DISPATCH_DEGRADED.
2. **Black-hole stall** — the provider accepts then hangs for seconds
   before failing. Expected: same terminal degradation; the measured wall
   time is reported so operators can see the latency cost of waiting out a
   partition (per-call timeouts on LLM invocations remain a documented
   production hardening item — breaker policy currently bounds *failures*,
   and the SDK's own request timeouts bound stalls).

Run:  python tests/chaos/chaos_network_partition.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.orchestrator as orchestrator_module  # noqa: E402
from google.adk.agents.base_agent import BaseAgent  # noqa: E402
from pydantic import ConfigDict  # noqa: E402
from tests.evaluation.harness import EvaluationHarness, ScriptedAgent, triage_payload  # noqa: E402


class PartitionedAgent(BaseAgent):
    """Raises like a dead network path: optionally stalls first."""

    stall_seconds: float = 0.0

    model_config = ConfigDict(arbitrary_types_allowed=True)

    async def _run_async_impl(self, ctx):
        if self.stall_seconds:
            await asyncio.sleep(self.stall_seconds)
        raise ConnectionError("CHAOS: network partition — provider unreachable")
        yield  # pragma: no cover


NAME = "network_partition"
IS_DESTRUCTIVE = True


async def execute(harness: EvaluationHarness):
    checks = []
    sat, debris = "SIM_PROTECTED_ASSET", "FENGYUN_1C_DEB"
    original_backoff = orchestrator_module.BREAKER_BACKOFF_SECONDS
    orchestrator_module.BREAKER_BACKOFF_SECONDS = (0.01, 0.01, 0.01)

    try:
        live_triage = ScriptedAgent(
            name="alert_triage", output_key="orbit_alert_triage", payload_factory=lambda: triage_payload(sat, debris)
        )

        refused = PartitionedAgent(name="astrodynamics_specialist", stall_seconds=0.0)
        pipeline_refused = harness.build_pipeline(live_triage, refused)
        started = time.perf_counter()
        outcome_refused = await harness.run_mission(pipeline_refused, {"sat_id": sat, "debris_id": debris})
        refused_s = time.perf_counter() - started

        stalled = PartitionedAgent(name="astrodynamics_specialist", stall_seconds=1.5)
        pipeline_stalled = harness.build_pipeline(live_triage, stalled)
        started = time.perf_counter()
        outcome_stalled = await harness.run_mission(pipeline_stalled, {"sat_id": sat, "debris_id": debris})
        stalled_s = time.perf_counter() - started
    finally:
        orchestrator_module.BREAKER_BACKOFF_SECONDS = original_backoff

    checks.append(harness.require("refused_degrades_cleanly", outcome_refused.final_status == "HUMAN_DISPATCH_DEGRADED", outcome_refused.final_status))
    checks.append(harness.require("stalled_degrades_cleanly", outcome_stalled.final_status == "HUMAN_DISPATCH_DEGRADED", outcome_stalled.final_status))
    checks.append(harness.check("refused_fast_under_patched_backoff", refused_s < 3.0, f"{refused_s:.2f}s"))
    print(f"       [chaos observation] refused={refused_s:.2f}s stalled(1.5s hang)={stalled_s:.2f}s "
          f"→ per-call LLM timeouts remain a production hardening item")
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    print(f"[{'PASS' if all(r.passed for r in results) else 'FAIL'}] chaos/{NAME}")
    for r in results:
        print(("  ✓" if r.passed else "  ✗"), r.name, r.detail)
    sys.exit(0 if all(r.passed for r in results) else 1)
