#!/usr/bin/env python3
"""O.R.B.I.T. evaluation suite runner — one command, one verdict.

Usage:
    python tests/evaluation/run_evaluation.py [--json PATH]

Discovers every ``test_*.py`` module in this directory, executes its
``execute(EvaluationHarness())`` coroutine against a fresh harness, and
aggregates the per-check results into:

* a JSON report (default ``tests/evaluation/report.json``)
* a markdown summary table on stdout

Exit code is 0 only when every check of every test passes — the same gate
the submission pipeline uses. The suite is fully hermetic: no network, no
Google credentials, no cost (see harness.py for the scripted-specialist
design).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.harness import CheckResult, EvaluationHarness  # noqa: E402

TEST_DIR = Path(__file__).resolve().parent
DEFAULT_REPORT = TEST_DIR / "report.json"


def discover_test_modules() -> list[str]:
    """Every test_*.py sibling, alphabetically — deterministic order."""
    return sorted(p.stem for p in TEST_DIR.glob("test_*.py"))


async def run_all(json_path: Path, markdown_path: Path | None = None) -> int:
    from agents import __version__ as fleet_version  # after harness env setup

    _silence_genai_teardown_noise()

    modules = discover_test_modules()
    print(f"ORBIT Evaluation Suite — {len(modules)} tests, hermetic mode (scripted specialists)")
    print("=" * 78)

    report: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "fleet_version": fleet_version,
        "python": platform.python_version(),
        "mode": "offline-scripted",
        "tests": [],
    }
    failures = 0

    for stem in modules:
        module = importlib.import_module(f"tests.evaluation.{stem}")
        name = getattr(module, "NAME", stem)
        description = getattr(module, "DESCRIPTION", "")
        started = time.perf_counter()
        try:
            checks: list[CheckResult] = await module.execute(EvaluationHarness())
            error = None
        except Exception as exc:  # noqa: BLE001 — a crashing test IS a failing test
            checks = []
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = round((time.perf_counter() - started) * 1000, 1)

        passed = error is None and all(c.passed for c in checks)
        failures += 0 if passed else 1
        status = "PASS" if passed else "FAIL"
        print(f"\n[{status}] {name} ({duration_ms:.0f} ms) — {description}")
        if error:
            print(f"       ERROR: {error}")
        for check in checks:
            marker = "  ✓" if check.passed else "  ✗"
            detail = f" — {check.detail}" if check.detail and not check.passed else ""
            print(f"{marker} {check.name}{detail}")

        report["tests"].append(
            {
                "name": name,
                "description": description,
                "passed": passed,
                "duration_ms": duration_ms,
                "error": error,
                "checks": [asdict(c) for c in checks],
            }
        )

    total_checks = sum(len(t["checks"]) for t in report["tests"])
    total_passed_checks = sum(1 for t in report["tests"] for c in t["checks"] if c["passed"])
    report["summary"] = {
        "tests": len(report["tests"]),
        "tests_passed": len(report["tests"]) - failures,
        "checks": total_checks,
        "checks_passed": total_passed_checks,
        "all_passed": failures == 0,
    }

    verdict = "ALL GREEN" if failures == 0 else f"{failures} FAILING"
    rows = ["| Test | Result | Checks | Duration |", "|---|---|---|---|"]
    for t in report["tests"]:
        result = "PASS" if t["passed"] else "**FAIL**"
        passed_checks = sum(1 for c in t["checks"] if c["passed"])
        rows.append(
            f"| {t['name']} | {result} | {passed_checks}/{len(t['checks'])} | {t['duration_ms']:.0f} ms |"
        )
    rows.append("")
    rows.append(f"**Verdict: {verdict}** ({report['summary']['checks_passed']}/{total_checks} checks)")
    markdown = "\n".join(rows)
    report["markdown_summary"] = markdown

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if markdown_path is not None:
        with markdown_path.open("a", encoding="utf-8") as handle:
            handle.write("### O.R.B.I.T. evaluation suite\n\n" + markdown + "\n")

    print("\n" + "=" * 78)
    print(markdown)
    print(f"JSON report: {json_path}")

    return 0 if failures == 0 else 1


def _silence_genai_teardown_noise() -> None:
    loop = asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def handler(loop_, context):
        exc = context.get("exception")
        future = context.get("future")
        coro_name = getattr(getattr(future, "get_coro", lambda: None)(), "__qualname__", "") or ""
        if (
            isinstance(exc, AttributeError)
            and "_async_httpx_client" in str(exc)
            and "aclose" in coro_name
        ):
            return
        (default or loop_.default_exception_handler)(context)

    loop.set_exception_handler(handler)


def main() -> None:
    # Windows consoles default to cp1252 and choke on arrows/em-dashes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT, help="JSON report output path")
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Append the markdown summary table to this file (e.g. $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run_all(args.json, args.markdown)))


if __name__ == "__main__":
    main()
