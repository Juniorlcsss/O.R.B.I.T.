# O.R.B.I.T. Performance Benchmarks
*Generated 2026-08-26T00:26:37.637805+00:00 on the hermetic offline harness (LLM/network latency excluded by design).*

| Benchmark | Samples | Mean | p50 | p95 | p99 |
|---|---|---|---|---|---|
| SGP4 conjunction screening | 150 | 0.61 ms | 0.6 ms | 0.68 ms | 1.04 ms |
| Model Armor 4-check inspection | 200 | 0.09 ms | 0.09 ms | 0.11 ms | 0.14 ms |
| Memory-bank state read | 300 | 0.0 ms | 0.0 ms | 0.01 ms | 0.01 ms |
| Memory-bank burn write | 300 | 0.03 ms | 0.03 ms | 0.04 ms | 0.06 ms |
| End-to-end mission (scripted, offline) | 40 | 23.9 ms | 4.34 ms | 5.18 ms | 784.48 ms |
| `import app` cold-start floor (subprocess) | 3 | 2053.18 ms | 1974.97 ms | 2198.73 ms | 2198.73 ms |
