"""Evaluation (Phase 10) — the evolved policy genuinely changes screening.

The linchpin proof: a FIXED Pc value classifies differently under two
saved policies. Pc = 7.51e-4 (the calibrated scenario) is HIGH under the
default thresholds and MEDIUM once pc_high_threshold evolves above it —
demonstrating that an applied cycle changes the very next decision, not
just the logs.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.policy import EVOLUTION_ENVELOPE, PolicyStore, ScreeningPolicy  # noqa: E402
from geap_sim.memory_bank import MemoryBank  # noqa: E402
from tools.space_tools import invalidate_policy_cache, screen_conjunction  # noqa: E402

NAME = "policy_changes_screening"
DESCRIPTION = "Fixed Pc 7.51e-4: HIGH under default policy → MEDIUM under evolved pc_high=9e-4"

FIXED_PC = 7.51e-4


async def _band_under_policy(store: PolicyStore, pc_high: float) -> tuple[str, dict]:
    await store.save(await store.load())  # ensure versioned save path is exercised
    current = await store.load()
    evolved = current.model_copy(update={"pc_high_threshold": pc_high})
    await store.save(evolved)
    invalidate_policy_cache()
    screened = screen_conjunction("SIM_PROTECTED_ASSET", "FENGYUN_1C_DEB")
    return screened["risk_level"], screened["policy_thresholds"]


async def execute(harness: EvaluationHarness):
    checks = []
    # The global screening path reads the shared store; drive IT directly.
    from evolution.policy import get_shared_policy_store

    store = get_shared_policy_store()

    # Restore pristine defaults first (other suites may have saved policies).
    defaults = ScreeningPolicy()
    await store.save(defaults)
    invalidate_policy_cache()

    band_default, thresholds_default = await _band_under_policy(store, 1e-4)
    checks.append(harness.require(
        "default_band_is_high", band_default == "HIGH",
        f"band={band_default}, thresholds={thresholds_default}",
    ))

    # Evolved policy: pc_high above the fixed Pc — inside the envelope.
    assert 9.0e-4 <= EVOLUTION_ENVELOPE["pc_high_threshold"][1]
    band_evolved, thresholds_evolved = await _band_under_policy(store, 9.0e-4)
    checks.append(harness.require(
        "evolved_band_is_medium", band_evolved == "MEDIUM",
        f"band={band_evolved}, thresholds={thresholds_evolved}",
    ))
    checks.append(harness.check(
        "same_fixed_pc_different_outcome", band_default != band_evolved,
        f"{FIXED_PC:.2e}: {band_default} → {band_evolved}",
    ))
    checks.append(harness.check(
        "screen_reports_live_thresholds",
        abs(thresholds_evolved["high"] - 9.0e-4) < 1e-12, str(thresholds_evolved),
    ))

    # Restore defaults so later suites see stock behaviour.
    await store.save(ScreeningPolicy())
    invalidate_policy_cache()
    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
