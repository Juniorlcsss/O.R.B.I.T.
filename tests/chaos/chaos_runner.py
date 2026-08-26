#!/usr/bin/env python3
"""O.R.B.I.T. chaos runner — DESTRUCTIVE tests, gated behind an explicit flag.

These tests simulate production failures (dead agents, corrupted
persistence, network partitions, alert floods). They are safe against the
hermetic in-process harness but are kept out of the default evaluation run
because their whole point is to stress the system past its comfort zone.

Usage:
    python tests/chaos/chaos_runner.py                       # refuses
    python tests/chaos/chaos_runner.py --i-know-this-is-destructive

Exit code 0 only when every chaos scenario holds.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CHAOS_DIR = Path(__file__).resolve().parent


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-know-this-is-destructive", action="store_true")
    args = parser.parse_args()

    if not args.i_know_this_is_destructive:
        print("REFUSING: chaos scenarios are destructive by design.")
        print("Re-run with --i-know-this-is-destructive to proceed.")
        sys.exit(2)

    from tests.evaluation.harness import EvaluationHarness  # env-safe import order

    modules = sorted(p.stem for p in CHAOS_DIR.glob("chaos_*.py") if p.stem != "chaos_runner")
    print(f"ORBIT Chaos Suite — {len(modules)} destructive scenario(s)")
    print("=" * 78)

    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "mode": "chaos", "scenarios": []}
    failures = 0
    for stem in modules:
        module = importlib.import_module(f"tests.chaos.{stem}")
        name = getattr(module, "NAME", stem)
        started = time.perf_counter()
        try:
            checks = asyncio.run(module.execute(EvaluationHarness()))
            error = None
        except Exception as exc:  # noqa: BLE001 — a crashing chaos test is a failing one
            checks, error = [], f"{type(exc).__name__}: {exc}"
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        passed = error is None and all(c.passed for c in checks)
        failures += 0 if passed else 1
        print(f"\n[{'PASS' if passed else 'FAIL'}] {name} ({duration_ms:.0f} ms)")
        if error:
            print(f"       ERROR: {error}")
        for check in checks:
            print(("  ✓" if check.passed else "  ✗"), check.name, check.detail)
        report["scenarios"].append(
            {"name": name, "passed": passed, "duration_ms": duration_ms, "error": error, "checks": [asdict(c) for c in checks]}
        )

    (CHAOS_DIR / "report.json").write_text(__import__("json").dumps(report, indent=2), encoding="utf-8")
    verdict = "ALL SCENARIOS HELD" if failures == 0 else f"{failures} SCENARIO(S) FAILED"
    print("\n" + "=" * 78 + f"\nVerdict: {verdict}")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
