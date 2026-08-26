# Project O.R.B.I.T.

**Orchestrated Routing & Ballistic Incident Tracking**

*Fortified Enterprise Fleet Track — All Things Agentic Hackathon 2026*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-informational)
![Google ADK](https://img.shields.io/badge/Google_ADK-2.7.1-blue)
![Cloud Run](https://img.shields.io/badge/Cloud_Run-deployable-brightgreen)

---

## 🎯 The Problem: Kessler Syndrome

Space debris moves at **17,000 mph** (≈ 7.6 km/s in low Earth orbit). A single
collision creates thousands of fragments, each becoming a new projectile in a
self-sustaining cascade — the Kessler Syndrome.

University CubeSat programs and small satellite operators bear exactly the same
collision-avoidance responsibilities as NASA-sized control rooms, with none of
the staffing:

> Conjunction screening requires interpreting messy Two-Line Element sets,
> running SGP4 propagations, computing collision probabilities, negotiating
> with other operators over *who* moves, and executing fuel-budgeted maneuvers
> — all under time pressure, around the clock.

---

## 🚀 The Solution: O.R.B.I.T.

A multi-agent fleet built on **Google ADK** that autonomously runs the entire
conjunction-response pipeline:

1. **Triages** messy inbound alerts into validated mission dossiers.
2. **Screens conjunctions** with real SGP4 propagation and a three-stage
   time-of-closest-approach refinement.
3. **Assesses collision probability** using Chan's first-order Gaussian method
   against NASA CARA / ESA risk bands.
4. **Negotiates dodge responsibility** with external constellation operators
   under fuel-budget diplomacy rules, with HMAC-SHA256 acknowledgements.
5. **Gates every maneuver twice**: an LLM Safety Officer verdict followed by a
   deterministic Model Armor sweep — hallucination guard, policy ceiling,
   strategic fuel reserve, secret/PII scan.
6. **Persists state** across sessions via a Firestore-backed memory bank, with
   every decision correlated by a mission-scoped trace ID on an OTel-style
   audit trail.

### Verified behaviour

| Check | Result |
|---|---|
| Calibrated HIGH-risk scenario (`LANCASTER_ORBIT_1` × `FENGYUN_1C_DEB`) | **89 m miss @ TCA, Pc = 7.51e-4 → HIGH** |
| Hallucination Guard | 13.9 m/s payload vs 10.0 m/s approved → REJECTED |
| Policy Ceiling | 80 m/s request vs 50 m/s ceiling → REJECTED |
| Strategic Fuel Reserve | burn projecting 2.75% < 5% reserve → REJECTED |
| Secret/PII sweep | planted AWS key + email caught by pattern label; content never echoed |
| End-to-end degraded mode (no LLM credentials) | triage breaker trips after 3 attempts → structured `HUMAN_DISPATCH_DEGRADED` + full audit replay |
| Edge autonomy wiring | `gemma_edge_autopilot` live in `/api/agent_tree` with its single `emergency_dodge` tool; ROM rule executes above Pc 1e-3, holds below it, caps an 80 m/s request to the 50 m/s ceiling and refuses fuel-reserve breaches |
| Edge autonomy end-to-end (no LLM reachable) | breaker trigger → `EDGE_AUTONOMY_ENGAGED` → `EDGE_LLM_UNAVAILABLE` → ROM verdict → `EDGE_AUTONOMOUS_DODGE_EXECUTED`, 12.0 m/s uplinked, Memory Bank fuel debited, debrief attached — every line tagged `EDGE_AUTONOMOUS` |
| Autonomous Veo debrief | terminal mission → debrief `READY` attached to the conjunction record; simulated reconstruction renders fully offline, honest about its mode |
| Lyria event cues | all 5 cue types served as valid 22.05 kHz mono WAV from `/api/audio/{event_type}` (0.32 s–1.70 s, 14–75 kB), memoised after first render; unknown types rejected with the available list |
| Vector recall | HIGH-context queries rank historical HIGH encounters first (cosine 0.74 vs 0.39); recall tool returns *"Based on 3 similar past conjunctions, the executed delta-v range was 9.5–9.5 m/s"* |
| Persistent watches | duplicate WATCH ignored via idempotent `watch_id`; HIGH re-screen → `AWAITING_HUMAN_APPROVAL` gated behind explicit approval; LOW decline auto-closes; a fresh instance resumes open watches (crash recovery) |
| Space-Track degradation | missing credentials raise `SpaceTrackUnavailable`; `fetch_real_tle` / `fetch_conjunction_screening` fall back to synthetic data with logged warning and provenance tags (`space-track/v1` vs `simulated_catalogue/v1`) |
| API surface | all endpoint smoke checks passing against real `google-adk`, including `/api/debrief/{id}`, `/api/audio/{event_type}` and the watch lifecycle (`POST /api/watches`, approval, close) |

---

## 📊 Judging Criteria → Evidence Map

### Innovation & Operational Utility (40%)

| Criterion | Evidence |
|-----------|----------|
| "Unlikely Hero" outside standard corporate roles | University CubeSat mission controllers and small-operator teams — real people with NASA-sized responsibility and no control room |
| Complex enough to warrant multi-agent system | 4 specialists + 1 deterministic orchestrator, each with distinct tools, models and responsibilities |
| Intelligently delegate tasks | `FleetCommanderPipeline` branches on validated risk bands: LOW → log & close; MEDIUM → advisory review held for human-in-the-loop; HIGH → full negotiation + dual-gate execution |
| "Twist" — autonomous action over chat | HIGH-risk missions run end-to-end without a human: negotiate with external fleets, pass two independent safety gates, authorise uplink, persist fleet state — then hand back an auditable decision record |

### Architectural Discipline & Tech Stack (30%)

| Criterion | Evidence |
|-----------|----------|
| Strict separation of concerns | Each agent owns a scoped tool roster; the Safety Officer owns **zero** tools; the orchestrator owns zero tools and delegates everything |
| Zero-trust enforcement | `AgentRegistry` manifests are deny-by-default; boot-time attestation verifies every declared tool **plus five negative controls**, or the process refuses to start |
| Failure-tolerant routing | Circuit breakers: 3 attempts, 1 s/2 s/4 s exponential backoff, JSON-schema validation between attempts, persisted failure counters; tripped breakers degrade to `HUMAN_DISPATCH_DEGRADED` instead of guessing |
| Model Armor guardrails | 4 deterministic checks executed *after* the LLM verdict and *before* any persistence/uplink: hallucination guard, policy ceiling, fuel reserve, secret sweep |
| Observability | Every mission emits one `trace_id`; `AuditLogger` writes OTel-style JSON to stdout → Cloud Logging; `GET /api/armor_report/{trace_id}` replays the entire reasoning chain |

### Demo & Production Readiness (30%)

| Criterion | Evidence |
|-----------|----------|
| Live execution proof | Deploys to Cloud Run via `./deploy.sh`; `/api/conjunction_alert` returns structured mission outcomes |
| Architecture transparency | `GET /api/agent_tree` renders the live fleet hierarchy — models, tools, temperatures included |
| Reproducible setup | `requirements.txt`, `.env.example`, Dockerfile layer caching, idempotent deploy script |
| Google Cloud proof | Cloud Run service, Firestore collections (`satellites/`, `conjunctions/`), Cloud Logging audit lines with severity levels |

---

## 🛠️ Tech Stack

| Component | Technology |
|-----------|------------|
| AI Models | Gemini 2.5 Pro (alert triage), Gemini 2.5 Flash (astrodynamics + negotiation), Gemini 3.5 Flash default for safety verdicts (env-tunable), **Gemma 3 (onboard edge autonomy)** |
| Generative Media | **Veo 3** (autonomous mission-debrief video), **Lyria 2** (mission-control audio cues) |
| Agent Framework | Google Agent Development Kit (ADK) 2.7.1 — mission pipeline + long-running watch supervisor |
| Orbital Mechanics | python-sgp4 propagation, three-stage TCA refinement, Chan's first-order Gaussian Pc; **live Space-Track.org elements & CDMs with synthetic fallback** |
| Agent Memory | Firestore state + **semantic vector recall of past conjunctions** (Vertex `text-embedding-005` / deterministic local embedder) |
| Cloud Infrastructure | Cloud Run, Firestore (async client), Cloud Logging |
| Command Center | React + Vite, CesiumJS globe, Server-Sent Events live feed |
| API Framework | FastAPI, Uvicorn |
| Security | Constant-time API-key middleware, zero-trust Agent Registry, dual-layer maneuver gating |
| Observability | Mission-scoped trace IDs, OTel-style structured JSON logging |

---

## Advanced Architecture

Three production-grade capabilities take O.R.B.I.T. beyond a reactive
pipeline: the fleet remembers what it has seen, monitors what it cannot
yet resolve, and runs on real orbital data when it can get it.

### 1. Vector Search Memory Bank — *the fleet learns*

Every logged conjunction is semantically embedded (`text-embedding-005`
on Vertex AI when credentials exist, a deterministic local hashing
embedder otherwise — always audited at startup) and stored alongside its
outcome. Before recommending a delta-v, the AstrodynamicsAgent calls
`recall_similar_conjunctions(...)` and cites precedent in its reasoning:

> *"Based on 3 similar past conjunctions, the executed delta-v range was 9.5–9.5 m/s (best similarity 0.83)."*

`MemoryBank.find_similar_conjunctions(context, top_k)` ranks stored
encounters by cosine similarity and returns them **with the actions that
resolved them** — Session State → Vector Search → Managed Cloud Memory,
with the production `FindNearest` KNN path documented in-code.

### 2. Long-Running Persistent Watches — *the fleet monitors* — `agents/watcher.py`

Some conjunctions need multi-day assessment while tracking improves. The
`WatchCommander` supervisor persists watch state through the MemoryBank
and re-screens each pair every N hours:

- **Idempotency** — one canonical `watch_id` per satellite×debris pair;
  duplicate WATCH commands are audited and ignored.
- **Crash recovery** — on startup the service reloads every open watch
  from persistent storage and resumes overdue checks on the first tick.
- **Human approval** — risk rising to HIGH parks the watch in
  `AWAITING_HUMAN_APPROVAL`; only an explicit
  `POST /api/watches/{id}/approval` routes it into a full fleet mission.
- **Auto-decline** — risk falling to LOW closes the watch automatically.

```bash
curl -X POST "$API/api/watches" -H "X-API-Key: $KEY" \
  -d '{"sat_id": "LANCASTER_ORBIT_1", "debris_id": "FENGYUN_1C_DEB", "interval_hours": 12}'
```

### 3. Real Space-Track.org Integration — *real orbital data* — `tools/space_track_api.py`

`SpaceTrackClient` authenticates against Space-Track.org, fetches live
TLE element sets and Conjunction Data Messages, enforces rate-limit
etiquette (one login per process, minimum request spacing, MemoryBank TTL
cache defaulting to 6 h) and exposes two fleet tools:
`fetch_real_tle(satellite_id)` and
`fetch_conjunction_screening(satellite_id)`. Without
`SPACETRACK_USERNAME` / `SPACETRACK_PASSWORD` both tools degrade loudly
to the calibrated synthetic catalogue — every response states its provenance.

> The fleet doesn't start from scratch, doesn't sleep, and doesn't pretend
> simulation is telemetry.

---

## 🎁 Additional AI Integrations

Three extra Google models give the fleet senses beyond Gemini reasoning:
an onboard brain for when Earth goes quiet, a documentarian for when the
mission ends, and a soundtrack for while it happens.

```mermaid
flowchart TB
    subgraph space["🛰️ Space segment — autonomous when Earth is out of reach"]
        EDGE["Gemma Edge Autopilot<br/>exactly 1 tool: emergency_dodge()<br/>Pc > 1e-3 · dv ≤ 50 m/s · fuel ≥ 5% · 30 s window"]
        SAT[("CubeSat")]
        EDGE --- SAT
    end

    subgraph ground["🌍 Ground segment — Google ADK fleet"]
        FC["FleetCommanderPipeline<br/>(deterministic control plane)"]
    end

    subgraph media["🎬 Autonomous reporting & sound"]
        VEO["Veo 3 mission debriefs<br/>GET /api/debrief/{conjunction_id}"]
        LYRIA["Lyria audio cues<br/>GET /api/audio/{event_type}"]
    end

    FC -->|"negotiation or armour breaker trips on HIGH risk"| EDGE
    FC -.->|"terminal mission status"| VEO
    FC -.->|"SSE audit stream"| LYRIA
```

### 1. Gemma Edge Autopilot — `agents/edge_agent.py`

When a HIGH-risk conjunction is mid-flight and the ground pipeline cannot
finish (negotiation or armour circuit breaker trips), the mission hands over
to a satellite-side **Gemma** agent — the flight analogue of losing downlink
during an incident. It holds exactly one tool (`emergency_dodge`), one
30-second decision window, and stricter physics than Model Armor applies:
Pc must exceed 1e-3, the burn stays under the 50 m/s ceiling and never eats
into the 5% strategic fuel reserve. If inference itself is unavailable, a
hardcoded ROM rule decides instead — the spacecraft never waits for a model.
Every edge decision is audited with the **`EDGE_AUTONOMOUS`** tag, and the
feature is kill-switchable via `ORBIT_ENABLE_EDGE_AUTONOMY=0`.

### 2. Veo Mission Debriefs — `tools/debrief_generator.py`

The fleet doesn't just solve problems — it documents them. When a mission
terminates (`EXECUTION_AUTHORIZED`, `MANEUVER_BLOCKED`, or an autonomous
edge dodge), a background task renders a cinematic summary of the encounter,
generates a debrief video with **Veo 3**, and attaches it to the conjunction
record in Firestore. `GET /api/debrief/{conjunction_id}` serves generation
status plus the artifact, and the command center surfaces a MISSION DEBRIEF
button the moment a conjunction resolves.

### 3. Lyria Mission-Control Audio — `tools/audio_generator.py`

You can hear the fleet think. Each key event class owns a generated audio
identity — rising alert (`ALERT_DETECTED`), resolving chord
(`MANEUVER_AUTHORIZED`), low cautionary drone (`MANEUVER_BLOCKED`), urgent
triple-beep (`HUMAN_DISPATCH`) and an edge-autonomy chirp
(`EDGE_AUTONOMOUS`) — produced by **Lyria 2** and served as memoised WAV
from `GET /api/audio/{event_type}`. The MissionFeed plays the matching cue
as events stream in over SSE.

> **Honesty note:** Veo and Lyria calls are gated behind
> `ORBIT_ENABLE_REAL_VEO=1` / `ORBIT_ENABLE_REAL_LYRIA=1`. Without them (or
> without Vertex credentials) both integrations degrade to clearly-labelled
> deterministic simulations — an SVG reconstruction of the encounter and a
> procedural synth cue respectively — so the demo works anywhere and never
> pretends a mock is a real render.

---

## Testing & Evaluation

The suite is **hermetic by design**: the real `FleetCommanderPipeline`,
Model Armour, memory bank and audit trail run unmodified while the four
specialist LLMs are substituted with schema-valid scripted agents (see
`tests/evaluation/harness.py`). No network, no credentials, no cost —
and every orchestration guarantee below is exercised by production code.

### Automated evaluation — one command

```bash
python tests/evaluation/run_evaluation.py        # exits non-zero on any failure
```

Latest full-suite result (**42/42 checks green**, total ≈ 1.5 s):

| Test | Result | Checks | Duration |
|---|---|---|---|
| circuit_breaker | PASS | 6/6 | 860 ms |
| fuel_guard | PASS | 4/4 | 7 ms |
| hallucination_guard | PASS | 5/5 | 5 ms |
| high_risk_conjunction | PASS | 9/9 | 608 ms |
| low_risk_conjunction | PASS | 4/4 | 3 ms |
| medium_risk_hitl | PASS | 6/6 | 3 ms |
| pii_sweep | PASS | 4/4 | 4 ms |
| policy_ceiling | PASS | 4/4 | 5 ms |

Highlights proven per test: the calibrated HIGH pair executes end-to-end
with all four armour checks PASSing, fuel debited exactly 0.5 %/m·s⁻¹ and
the debrief auto-generated on the same trace; LOW risk never invokes
negotiation or armour; MEDIUM always lands behind a human; planted payload
drift (`13.9 vs 8.0 m/s`), an `80 m/s` ceiling breach, a reserve-breaching
burn at 6 % fuel and a fake AWS key are each blocked with the correct
violation label — the key never once echoed into the audit trail; and a
dead provider trips after exactly three attempts into structured human
dispatch.

### Chaos engineering — destructive, gated

```bash
python tests/chaos/chaos_runner.py --i-know-this-is-destructive
```

Four scenarios, all currently holding:

| Scenario | System behaviour under attack |
|---|---|
| `chaos_kill_agent` — orbital-mechanics node destroyed mid-fleet | breaker retries ×3 → trips → `HUMAN_DISPATCH_DEGRADED`; zero fuel touched |
| `chaos_corrupt_state` — negative / NaN / garbage vehicle records | read-boundary sanitisation (`SATELLITE_STATE_CORRUPTED_SANITISED`) keeps armour arithmetic finite: negative fuel → blocked at reserve, NaN → clean execution |
| `chaos_network_partition` — refused sockets & black-hole stalls | both degrade cleanly; measured: refused 0.04 s vs 1.5 s hang → 4.55 s (per-call LLM timeouts remain a documented hardening item) |
| `chaos_rapid_fire` — 100 concurrent missions | 100/100 terminal, unique trace per mission, **~160 missions/s** orchestration throughput |

Chaos engineering paid for itself immediately: the corruption scenario
exposed that a `NaN` fuel value could slip the original clamp chain, which
is why `MemoryBank.get_satellite_state` now sanitises non-finite and
out-of-range fields at the read boundary and audits every repair.

### Performance benchmarks

```bash
python tests/benchmarks/performance.py --save   # writes report.md / report.json
```

| Benchmark | Samples | Mean | p50 | p95 | p99 |
|---|---|---|---|---|---|
| SGP4 conjunction screening | 150 | 0.61 ms | 0.60 ms | 0.68 ms | 1.04 ms |
| Model Armor 4-check inspection | 200 | 0.09 ms | 0.09 ms | 0.11 ms | 0.14 ms |
| Memory-bank state read | 300 | <0.01 ms | <0.01 ms | 0.01 ms | 0.01 ms |
| Memory-bank burn write | 300 | 0.03 ms | 0.03 ms | 0.04 ms | 0.06 ms |
| End-to-end mission (offline harness) | 40 | 23.9 ms | 4.34 ms | 5.18 ms | 784 ms |
| `import app` cold-start floor | 3 | 2053 ms | 1975 ms | 2199 ms | 2199 ms |

Reading the table: a complete conjunction screen costs well under a
millisecond of CPU, the entire deterministic safety gate costs less than
a tenth of one, and a mission's orchestration overhead sits in single-digit
milliseconds — meaning end-to-end latency in production is dominated by
LLM inference, exactly where it should be dominated. The p99 mission
outlier is first-run module warm-up inside the measurement loop. The
`import app` figure is a local floor for Cloud Run cold starts (true
platform cold start depends on container pull + runtime and is measured on
the deployed service); set `ORBIT_BENCH_URL=http://localhost:8080` against
a running instance to append live HTTP latency to the same report.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- A Google Cloud project with **Cloud Run**, **Firestore** and **Vertex AI** enabled
- `gcloud` CLI installed and authenticated

### Local development

```bash
git clone <repo-url>
cd ORBIT
pip install -r requirements.txt

cp .env.example .env
# edit .env: set GOOGLE_CLOUD_PROJECT and ORBIT_API_KEY

uvicorn app:app --reload --port 8080
```

No GCP credentials? Everything still runs: the memory bank falls back to
in-process storage, and missions that need LLMs degrade through the circuit
breaker to a clean `HUMAN_DISPATCH_DEGRADED` response — the failure-tolerant
path is itself demonstrable offline.

### Deploy to Cloud Run

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
export ORBIT_API_KEY="$(openssl rand -hex 32)"

chmod +x deploy.sh
./deploy.sh
```

The script builds the image, creates a least-privilege service account
(`roles/datastore.user` + `roles/aiplatform.user`), deploys scale-to-zero with
a 3-instance cost cap, and prints a ready-to-paste curl command.

---

## 🧪 Testing the Fleet

### Trigger a HIGH-risk conjunction

```bash
curl -X POST "https://<your-service>.run.app/api/conjunction_alert" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{
    "sat_id": "LANCASTER_ORBIT_1",
    "debris_id": "FENGYUN_1C_DEB",
    "alert_source": "SPACE_TRACK_API",
    "priority": "URGENT",
    "raw_message": "Conjunction warning: LANCASTER_ORBIT_1 approaching debris field from Fengyun-1C anti-satellite test."
  }'
```

Response:

```json
{
  "trace_id": "9f1c…",
  "status": "EXECUTION_AUTHORIZED",
  "risk_band": "HIGH",
  "miss_distance_km": 0.0889,
  "pc": 0.000751,
  "action_taken": "they_dodge",
  "armor_violations": null,
  "conjunction_id": "LANCASTER_ORBIT_1-X-FENGYUN_1C_DEB-TCA-…"
}
```

### Fetch the autonomous mission debrief

```bash
curl "https://<your-service>.run.app/api/debrief/<conjunction_id>" \
  -H "X-API-Key: <your-api-key>"
```

### Hear the fleet

```bash
curl -o alert.wav "https://<your-service>.run.app/api/audio/ALERT_DETECTED" \
  -H "X-API-Key: <your-api-key>"
```

### Prove the architecture

```bash
curl "https://<your-service>.run.app/api/agent_tree" \
  -H "X-API-Key: <your-api-key>"
```

### Replay a mission's complete reasoning chain

```bash
curl "https://<your-service>.run.app/api/armor_report/<trace_id>" \
  -H "X-API-Key: <your-api-key>"
```

Every triage result, screening number, negotiation outcome, safety verdict,
armor check and breaker transition — correlated by one trace ID.

---

## 📁 Project Structure

```
ORBIT/
├── app.py                    # FastAPI application (15 endpoints, security, audit, watch supervisor)
├── Dockerfile                # Slim production container for Cloud Run
├── deploy.sh                 # Idempotent deployment + least-privilege SA bootstrap
├── logging.json              # Structured JSON logging config for uvicorn
├── requirements.txt          # Pinned dependencies
├── .env.example              # Environment template (never commit .env itself)
├── agents/
│   ├── __init__.py           # Fleet exports + __version__
│   ├── orchestrator.py       # FleetCommanderPipeline (BaseAgent), circuit breakers, edge fallback
│   ├── astro.py              # Astrodynamics specialist (SGP4 + vector recall + real data)
│   ├── diplomat.py           # Negotiation officer (external fleets)
│   ├── safety.py             # Safety Officer (zero tools, fail-closed)
│   ├── edge_agent.py         # Gemma Edge Autopilot (one tool: emergency_dodge, EDGE_AUTONOMOUS)
│   └── watcher.py            # WatchCommander: persistent multi-day watches (idempotent, crash-safe)
├── tools/
│   ├── __init__.py
│   ├── space_tools.py        # TLE synthesis, SGP4 screening, negotiation, recall & real-data tools
│   ├── space_track_api.py    # Space-Track.org client (auth, TLEs, CDMs, TTL cache, rate limits)
│   ├── debrief_generator.py  # Veo 3 mission-debrief video + simulated reconstruction
│   └── audio_generator.py    # Lyria 2 event cues + offline procedural synth
├── frontend/                 # React command center (CesiumJS globe, SSE feed, debrief viewer)
└── geap_sim/
    ├── __init__.py
    ├── memory_bank.py        # Firestore state + vector recall + watch persistence + API cache
    ├── agent_registry.py     # Zero-trust manifests + boot attestation
    ├── model_armor.py        # Deterministic 4-check guardrail middleware
    └── observability.py      # OTel-style AuditLogger + JsonFormatter
```

---

## ⚠️ Known Limitations

This is a hackathon prototype, and we would rather show you its edges than
hide them:

- **Simulated space catalogue.** The TLE catalogue is synthetic (calibrated so
  the demo scenario screens as a genuine HIGH conjunction under real SGP4);
  counterparty fleets live at `*.example` endpoints.
- **Acknowledgements are format-checked, not trust-anchored.** The HMAC-SHA256
  MAC proves integrity of our simulation, not a real counterparty's signature.
- **Linear fuel model.** 0.5 percentage points per m/s — a documented stand-in
  for the rocket equation with live mass data.
- **Single-worker sessions.** `InMemorySessionService` keeps the demo cheap;
  horizontal scaling needs ADK's database-backed session service.
- **Audit replay is process-local.** `/api/armor_report` reads a bounded ring
  buffer; the durable trail is Cloud Logging.
- **App-level API keys rather than OIDC/IAP.** Deliberate for judge
  accessibility; production would front this with proper identity.
- **Safety-verdict model tier is env-configurable** and defaults to
  `gemini-3.5-flash`; swapping tiers is a one-line change.
- **Veo + Lyria artifacts are simulated unless explicitly enabled.** The
  debrief reconstruction and audio cues render deterministically offline;
  real Vertex AI generation requires `ORBIT_ENABLE_REAL_VEO=1` /
  `ORBIT_ENABLE_REAL_LYRIA=1` plus credentials.
- **Edge autonomy is a demo envelope, not flight certification.** The Gemma
  autopilot's thresholds (Pc > 1e-3, dv ceiling, fuel reserve) mirror the
  ground policy but nothing here is CCSDS-qualified hardware.
- **Vector recall uses a deterministic hashing embedder offline.** It
  clusters templated situation descriptions well; upgrade to the audited
  Vertex `text-embedding-005` path by adding credentials (automatic).
- **Watch crash-recovery is cross-process only on Firestore.** The
  in-memory backend proves the resume logic within one process; durable,
  multi-instance recovery needs the real Firestore backend.
- **Space-Track CDM Pc parsing includes a documented heuristic** (values
  above 0.5 are treated as percentages); verify against live payloads when
  credentials are configured.

---

## 🔮 Future Work

- **Swap `geap_sim` for the actual GEAP platform** — managed identity, agent
  registry and Model Armor replace the simulations one module at a time; the
  seams are already isolated.
- **True on-device Gemma** — quantised GGUF inference on the flight computer
  so edge autonomy works with zero connectivity, no Vertex round-trip.
- **Conjunction storms** — `LoopAgent` continuous monitoring and multi-object
  deconfliction when one maneuver creates new conjunctions downstream.
- **Production hardening** — Terraform IaC, CI with a pytest suite, OIDC
  end-to-end, Firestore-backed sessions for multi-instance deployments.

---

## 🤖 Development Methodology

This project was built with an **AI-augmented development workflow**: Claude,
Cursor and similar assistants were used as accelerators for scaffolding,
test generation and documentation polish — disclosed here in accordance with
hackathon rules.

All architectural decisions, orbital-mechanics calibration, safety-policy
design and GEAP integration choices were made by the human developer. Using
AI tools to build an agentic system is not a shortcut here — it is the modern
workflow this hackathon exists to celebrate.

---

## 👥 Team

**Jonathan Randall** — solo developer
*Lancaster University*

Devpost handle & contact: see submission page.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

*Built for the All Things Agentic Hackathon 2026 — Fortified Enterprise Fleet Track.*
