#!/usr/bin/env python3
"""O.R.B.I.T. performance benchmarks — evidence, not vibes.

Measures the fleet's hot paths against the hermetic harness (scripted
specialists, in-memory bank) so numbers reflect *orchestration* cost:

* SGP4 screening            — one full conjunction screen (propagation +
                              three-stage TCA refinement + Chan Pc).
* Model Armour inspection   — the four-check deterministic sweep.
* Memory-bank read / write  — Firestore-shaped document I/O.
* End-to-end mission        — triage→screen→negotiate→verdict→armour→
                              persist, i.e. the /api/conjunction_alert
                              handler minus LLM latency.
* Import cold-start         — `import app` wall time as a local floor for
                              Cloud Run cold starts (true platform cold
                              start requires a deployed service; measured
                              separately when ORBIT_BENCH_URL is provided).

If ``ORBIT_BENCH_URL`` points at a running deployment (e.g.
http://localhost:8080), live HTTP latency for POST /api/conjunction_alert
is benchmarked too and included in the report.

Usage:
    python tests/benchmarks/performance.py [--save]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BENCH_DIR = Path(__file__).resolve().parent


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * len(ordered) - 1)))
    return ordered[index]


def summarise(label: str, values_ms: list[float]) -> dict:
    return {
        "benchmark": label,
        "samples": len(values_ms),
        "mean_ms": round(statistics.fmean(values_ms), 2),
        "p50_ms": round(percentile(values_ms, 50), 2),
        "p95_ms": round(percentile(values_ms, 95), 2),
        "p99_ms": round(percentile(values_ms, 99), 2),
    }


def bench_sync(func, samples: int) -> list[float]:
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        func()
        timings.append((time.perf_counter() - started) * 1000.0)
    return timings


async def main(save: bool) -> int:
    # Import-order matters: pulling the harness (and thus agents.*) first
    # mirrors production and avoids the model_armor→agents.safety cycle.
    from tests.evaluation.harness import EvaluationHarness, negotiation_payload, real_screening_payload, triage_payload, verdict_payload
    from geap_sim.memory_bank import get_shared_memory_bank
    from geap_sim.model_armor import ModelArmor
    from tools.space_tools import screen_conjunction

    rows: list[dict] = []

    # --- 1. SGP4 screening -----------------------------------------------------
    rows.append(summarise("SGP4 conjunction screening", bench_sync(lambda: screen_conjunction("SIM_PROTECTED_ASSET", "FENGYUN_1C_DEB"), 150)))

    # --- 2. Model Armour -------------------------------------------------------
    armor = ModelArmor(memory_bank=get_shared_memory_bank())
    negotiation = {"action": "we_dodge", "our_dv_mps": 8.0, "their_dv_mps": 0.0, "sat_id": "SIM_PROTECTED_ASSET"}
    verdict = {"approved": True, "expected_delta_v_mps": 8.0}

    async def armour_once() -> None:
        report = await armor.inspect_maneuver_request(dict(negotiation), dict(verdict))
        assert report.status in ("APPROVED", "REJECTED")

    await armour_once()  # warm-up (first call may build clients)
    armour_timings = []
    for _ in range(200):
        started = time.perf_counter()
        await armour_once()
        armour_timings.append((time.perf_counter() - started) * 1000.0)
    rows.append(summarise("Model Armor 4-check inspection", armour_timings))

    # --- 3. Memory bank read / write --------------------------------------------
    bank = get_shared_memory_bank()

    async def read_once() -> None:
        await bank.get_satellite_state("SIM_PROTECTED_ASSET")

    async def write_once(i: int) -> None:
        await bank.update_satellite_state("BENCH_SAT", delta_v_expended=0.01, new_fuel=99.9)

    await read_once(); await write_once(0)  # warm-up
    read_timings, write_timings = [], []
    for i in range(300):
        started = time.perf_counter(); await read_once(); read_timings.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter(); await write_once(i); write_timings.append((time.perf_counter() - started) * 1000.0)
    rows.append(summarise("Memory-bank state read", read_timings))
    rows.append(summarise("Memory-bank burn write", write_timings))

    # --- 4. End-to-end scripted mission ------------------------------------------
    harness = EvaluationHarness()
    sat, debris = "SIM_PROTECTED_ASSET", "FENGYUN_1C_DEB"
    mission_timings: list[float] = []
    for _ in range(40):
        specialists = harness.scripted_specialists(
            triage_factory=lambda: triage_payload(sat, debris),
            astro_factory=real_screening_payload(sat, debris),
            diplomat_factory=lambda: negotiation_payload("we_dodge", our_dv=8.0),
            safety_factory=lambda: verdict_payload(True),
        )
        pipeline = harness.build_pipeline(*specialists)
        started = time.perf_counter()
        await harness.run_mission(pipeline, {"sat_id": sat, "debris_id": debris})
        mission_timings.append((time.perf_counter() - started) * 1000.0)
    rows.append(summarise("End-to-end mission (scripted, offline)", mission_timings))

    # --- 5. Import cold-start floor ----------------------------------------------
    repo_root = BENCH_DIR.parents[1]
    import_times = []
    for _ in range(3):
        started = time.perf_counter()
        subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=str(repo_root), check=True, capture_output=True,
        )
        import_times.append((time.perf_counter() - started) * 1000.0)
    rows.append(summarise("`import app` cold-start floor (subprocess)", import_times))

    # --- Optional: live HTTP against a running deployment -------------------------
    bench_url = os.environ.get("ORBIT_BENCH_URL")
    http_row = None
    if bench_url:
        try:
            import urllib.request

            body = json.dumps({
                "sat_id": sat, "debris_id": debris,
                "alert_source": "BENCH", "priority": "URGENT",
                "raw_message": "Benchmark alert",
            }).encode()
            api_key = os.environ.get("ORBIT_API_KEY", "")
            timings_http = []
            for _ in range(10):
                started = time.perf_counter()
                request = urllib.request.Request(
                    f"{bench_url.rstrip('/')}/api/conjunction_alert", data=body,
                    headers={"Content-Type": "application/json", "X-API-KEY": api_key},
                )
                urllib.request.urlopen(request, timeout=120).read()
                timings_http.append((time.perf_counter() - started) * 1000.0)
            http_row = summarise(f"POST /api/conjunction_alert (live @ {bench_url})", timings_http)
        except Exception as exc:  # noqa: BLE001 — live bench is best-effort
            print(f"[bench] live HTTP skipped: {exc}")

    all_rows = ([http_row] if http_row else []) + rows

    # --- Report -------------------------------------------------------------------
    generated = datetime.now(timezone.utc).isoformat()
    print(f"\nORBIT Performance Benchmarks — {generated}")
    print("| Benchmark | Samples | Mean | p50 | p95 | p99 |")
    print("|---|---|---|---|---|---|")
    for row in all_rows:
        print(f"| {row['benchmark']} | {row['samples']} | {row['mean_ms']} ms | {row['p50_ms']} ms | {row['p95_ms']} ms | {row['p99_ms']} ms |")

    payload = {"generated_utc": generated, "note": "offline-scripted harness; LLM/network latency excluded by design", "results": all_rows}
    (BENCH_DIR / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if save:
        lines = [
            "# O.R.B.I.T. Performance Benchmarks",
            f"*Generated {generated} on the hermetic offline harness (LLM/network latency excluded by design).*",
            "",
            "| Benchmark | Samples | Mean | p50 | p95 | p99 |",
            "|---|---|---|---|---|---|",
        ]
        for row in all_rows:
            lines.append(f"| {row['benchmark']} | {row['samples']} | {row['mean_ms']} ms | {row['p50_ms']} ms | {row['p95_ms']} ms | {row['p99_ms']} ms |")
        (BENCH_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nMarkdown report saved: {BENCH_DIR / 'report.md'}")
    print(f"JSON report: {BENCH_DIR / 'report.json'}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true", help="also write report.md next to this script")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.save)))
