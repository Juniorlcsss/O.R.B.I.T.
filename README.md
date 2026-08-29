
# Project O.R.B.I.T.

**Orchestrated Routing & Ballistic Incident Tracking**

*Fortified Enterprise Fleet Track, All Things Agentic Hackathon 2026*

[![Tests](https://github.com/Juniorlcsss/ORBIT/actions/workflows/tests.yml/badge.svg)](https://github.com/Juniorlcsss/ORBIT/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-informational)
![Google ADK](https://img.shields.io/badge/Google_ADK-2.7.1-blue)
![Gemini](https://img.shields.io/badge/Gemini-3.5%20%2F%203.7%20Flash-4285F4)
![Vertex AI](https://img.shields.io/badge/Vertex_AI-global_endpoint-orange)
![Cloud Run](https://img.shields.io/badge/Cloud_Run-live-brightgreen)

O.R.B.I.T. is a fleet of Google ADK agents that runs the whole satellite
collision-avoidance loop with nobody in it: read the alert, propagate the
orbits, compute collision probability, decide who moves, clear the manoeuvre
through two independent safety gates, sign the command, and write the entire
chain to an audit trail.

## Submission links

| | |
|---|---|
| Live command center | https://orbit-command-center-ch7bwuuvpa-uc.a.run.app |
| Demo video (approx. 4 min) | _add video URL_ |
| Architecture diagram | <img width="1472" height="1369" alt="orbit architecture" src="https://github.com/user-attachments/assets/7849dcef-44d2-4397-b991-609677f644f2" /> |
| Track | Fortified Enterprise Fleet |

### Required stack

| Requirement | How O.R.B.I.T. meets it |
|---|---|
| Gemini 3.5 or newer, via Gemini API or Vertex AI | Every reasoning agent runs `gemini-3.5-flash` or `gemini-3.7-flash` through Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `global` endpoint). Confirm against the running service with `GET /api/agent_tree` |
| At least one Google agent framework | Google ADK 2.7.1: `LlmAgent`, `BaseAgent`, `Runner`, `Event`/`EventActions`, `FunctionTool` and `InvocationContext` across 13 modules |
| At least one Google Cloud infrastructure service | Cloud Run (two services), Firestore (Memory Bank), Cloud Logging (audit trail), Secret Manager |
| Bonus: additional Google AI models | Gemma 4 for onboard edge autonomy, Veo 3 for mission debriefs, Lyria 2 for mission-control audio |

### Fortified Enterprise Fleet track

| What the track asks for | Where it is |
|---|---|
| Agents catalogued for cross-department discovery | 15 versioned manifests in `geap_sim/agent_registry.py`, published live at `GET /api/agent_tree` with each agent's model, tools, temperature and identity scope |
| Context held safely across weeks of asynchronous operation | `WatchCommander` re-screens a pair every N hours for as long as the encounter stays open, with idempotent watch IDs and crash recovery; the Firestore Memory Bank carries state and precedent between sessions |
| Production data touched without violating policy | Live Space-Track data behind a self-limiting rate-budget client; per-secret Secret Manager access for two least-privilege service accounts; Model Armor's secret and PII sweep between every LLM verdict and any persistence or uplink |

### Where to look

| Judging criterion | Sections |
|---|---|
| Innovation and operational utility | [What the fleet does](#what-the-fleet-does), [Fleet Admiral](#fleet-admiral), [The coordination gap](#the-coordination-gap) |
| Architectural discipline and tech stack | [Architectural discipline](#architectural-discipline), [GEAP pillar coverage](#geap-pillar-coverage), [Testing and evaluation](#testing-and-evaluation) |
| Demo and production readiness | [Spin-up instructions](#spin-up-instructions), [Deploying to Cloud Run](#deploying-to-cloud-run), [Exercising a deployed fleet](#exercising-a-deployed-fleet), [Known limitations](#known-limitations) |

---

## The problem

Debris in low Earth orbit moves at roughly 7.6 km/s. One collision produces
thousands of fragments, each of which becomes a new projectile, which is the
cascade Donald Kessler described in 1978.

A university CubeSat programme carries the same collision-avoidance duty as a
national agency and has none of the staffing to discharge it. Screening a
conjunction means parsing element sets, running SGP4 propagations, computing a
collision probability, working out whether you or the other operator should
move, agreeing it with them, and executing a fuel-budgeted burn, all against a
time of closest approach that does not wait for office hours.

Each of those steps is well defined on its own. Joined up and automated, for an
operator without a control room, they are not. That gap is what this project
fills.

---

## What the fleet does

An alert enters `POST /api/conjunction_alert` and the pipeline runs to a
terminal decision without further input:

1. **Triage** turns a messy inbound alert into a validated mission dossier.
2. **Screening** propagates both objects with SGP4 and refines the time of
   closest approach in three stages. Where 18th Space Defense Squadron has
   published a CDM for the pair, the CDM takes precedence over our propagation.
3. **Risk assessment** computes collision probability with Chan's first-order
   Gaussian method and bands it against NASA CARA and ESA thresholds.
4. **Debate** replaces the single specialist recommendation on HIGH-risk cases
   with three strategists arguing under a deterministic moderator.
5. **Coordination** asks the counterparty to move where a counterparty exists
   and can move. Where it cannot, the stage is skipped structurally and the
   mission proceeds to unilateral avoidance.
6. **Safety** gates the manoeuvre twice: an LLM Safety Officer verdict followed
   by four deterministic Model Armor checks that run after the verdict and
   before anything is persisted or uplinked.
7. **Execution** signs the command with HMAC-SHA256, debits fuel from the
   Memory Bank, persists the conjunction record and renders a debrief.

Every stage emits audit events under one mission-scoped trace ID, replayable in
full through `GET /api/armor_report/{trace_id}`.

***arch*** *System architecture: ground segment, space segment, the Google Cloud
services behind them and the orbital data sources feeding the pipeline.*

***arch*** *Mission pipeline sequence: alert through triage, screening, debate,
coordination, safety gating and execution, including the two degradation paths.*

---

## Architectural discipline

**Decoupling.** Each agent owns a scoped tool roster. The Safety Officer owns
zero tools, deliberately, so its verdict cannot be a function of anything it
fetched. The orchestrator owns zero tools and delegates everything. `geap_sim/`
isolates each Gemini Enterprise Agent Platform seam behind its own module so it
can be swapped for the managed service one at a time.

**State and memory.** A Firestore-backed Memory Bank holds satellite state,
conjunction history and watch state, with per-event-loop client binding.
Semantic vector recall ranks past encounters by cosine similarity and returns
them with the actions that resolved them, so the astrodynamics agent cites
precedent instead of reasoning from nothing.

**Credentials.** No secrets in the repository, enforced by a deny-by-default
`.gitignore`. Application Default Credentials locally, the injected runtime
service account on Cloud Run. Two least-privilege service accounts, per-secret
Secret Manager grants, constant-time API-key comparison, and a deploy script
that refuses to publish an unauthenticated service.

**Failure handling.** Circuit breakers retry three times with 1/2/4 second
backoff and JSON-schema validation between attempts, then degrade to a
structured `HUMAN_DISPATCH_DEGRADED` response instead of guessing. Space-Track,
Vertex embeddings, Veo, Lyria and Firestore each degrade to a labelled fallback
and audit the reason.

**Zero trust.** `AgentRegistry` manifests are deny-by-default with a per-agent
`identity_scope`. Boot attestation verifies all 15 manifests and their declared
tools, plus five negative controls proving the registry denies unregistered tool
use, or the process refuses to start.

### GEAP pillar coverage

| Pillar | Module | What it does here |
|---|---|---|
| Agent Registry | `geap_sim/agent_registry.py` | 15 versioned manifests with identity scopes, declared tool rosters and boot attestation; discovery through `GET /api/agent_tree` |
| Agent Runtime | `agents/watcher.py` | Long-running asynchronous watches with a supervisor loop, crash recovery and multi-day re-screening |
| Memory Bank | `geap_sim/memory_bank.py` | Firestore-persisted state, conjunction history, watch state and semantic vector recall |
| Agent Identity | `geap_sim/agent_registry.py` | Every agent carries a scoped identity; unmanifested agents cannot boot |
| Agent Gateway | `orbit.api.gateway` middleware in `app.py` | Single ingress: constant-time key enforcement, trace-ID assignment, per-request policy logging |
| Model Armor | `geap_sim/model_armor.py` | Hallucination guard, policy ceiling, fuel reserve and secret/PII sweep between the verdict and any action |
| Agent Observability | `geap_sim/observability.py` | OTel-style structured audit events and full reasoning-chain replay |

---

## How the pieces work

### Multi-agent debate

HIGH-risk conjunctions go to a panel of three strategists instead of a single
specialist: `fuel_minimizer` (fuel is lifespan, find the smallest credible
burn), `safety_maximizer` (a near miss is a failure) and `reassess` (burning is
irreversible, propose hold and rescreen). All three run at temperature 0.6.

The `DebateModerator` is an ADK `BaseAgent` and never an LLM. It runs round zero
in parallel, then critique rounds. Four checks police every argument: a
hallucination cross-check, where each cited Pc, miss distance, TCA and delta-v
must match the real screening within tolerance; a physics check; a
policy-envelope check; and SHA-256 loop detection, which freezes any strategist
that repeats itself verbatim. Round count and wall-clock time are both capped. A judge at temperature 0 adjudicates only when several
validated proposals remain, and may only choose among them. If nothing valid
survives, the classic single-specialist recommendation is emitted with
`fallback_used=True`.

The debate can fail. The mission cannot fail because of the debate.

### Self-evolving screening policy

Resolved missions land outcome records. A Learning Analyst reviews the current
`ScreeningPolicy` against them and must justify every proposed change with cited
counts; thin evidence must produce no change. Then:

- Five deterministic heuristics inspect the proposal before any LLM sees it:
  convenience drift, attention drift, safety erosion (always CRITICAL),
  oscillation across cycles, and unjustified magnitude on thin evidence.
- An adversarial Meta-Critic at temperature 0 assumes the proposer is guilty and
  returns APPROVE, CLAMP or REJECT with a 0 to 1 suspicion score.
- Any CRITICAL flag, a REJECT verdict, or suspicion at or above 0.7 kills the
  cycle outright.
- A hard envelope clamp applies to everything, approved proposals included: each
  parameter bounded to its safe range, limited to 20 percent of that range per
  cycle, and ordering-checked. No LLM output is ever saved raw.
- Three consecutive rejections freeze evolution until a human calls
  `POST /api/evolution/unfreeze`.

Because `screen_conjunction` reads the live policy for its risk bands, an
applied cycle changes the next screening decision. A fixed Pc of 7.51e-4
classifies HIGH under the default policy and MEDIUM once the threshold evolves
past it, which is what `test_policy_changes_screening` asserts.

On the live endpoint, `APPLIED` is not guaranteed and should not be. Whether a
cycle applies depends on what the analyst proposes on the day. The deterministic
proof that the apply path works end to end is
`tests/evaluation/test_evolution_conservative_loosen.py`, which scripts the
analyst, so the assertion tests the engine and not the weather.

### Fleet Admiral

The mission pipeline answers one question well: given this conjunction, what
should this satellite do. It is deliberately blind to the rest of the
constellation, because a pipeline that re-reasons about the whole fleet on every
alert cannot be made deterministic.

Real operators do not receive one alert. A fragmentation event produces a burst
across many assets at once, and fuel is the constraint that couples them.
Spending 12 m/s on a satellite at 8 percent fuel to clear an encounter that a
sibling at 90 percent could also clear spends the wrong vehicle's remaining
life. `agents/admiral.py` owns that coupling and nothing else.

Four constraints keep it safe, not just clever. It is deterministic
arithmetic with no LLM, because propellant allocation across a fleet should not
be re-sampled on every invocation. It cannot weaken a gate, because it decides
which satellites enter the pipeline and never what the pipeline may do once they
are in. It can only subtract, because its two outcomes are `dodge` and
`hold_and_reassess`, so it can decline a manoeuvre but never authorise one. And
a single-alert batch is a no-op, because there is no constellation to optimise
with one satellite in it.

Each assigned mission runs in its own ADK session. The pipeline writes screening
and verdict payloads to fixed session-state keys, so a shared session would let
mission N+1 read mission N's screening.

### Live Space-Track data

`SpaceTrackClient` authenticates against Space-Track.org, fetches element sets
and Conjunction Data Messages, and exposes `fetch_real_tle(satellite_id)` and
`fetch_conjunction_screening(satellite_id)`. Two details of the live API deserve their own paragraph: both are easy to get
wrong, and both fail silently behind a graceful-degradation path.

The `tle`, `tle_latest` and `tle_publish` classes have been removed. Element
sets come from the GP class filtered with `decay_date/null-val`, so a decayed
catalogue number yields nothing at all instead of a stale, unpropagable elset.
Conjunctions come from `cdm_public`. The request action is `query`, not `api`,
and sort predicates are lowercase with a space: `orderby/EPOCH%20desc`.

A CDM names two objects and ours may be either one, so `fetch_cdms` queries
`SAT_1_ID` and `SAT_2_ID` separately and merges, recording which object is the
counterparty, since that is the only party coordination can address.

Rate-limit etiquette is enforced, not hoped for: one login per process,
minimum request spacing, a rolling budget guard that fails closed into synthetic
data before it can breach the published 30 per minute and 300 per hour ceilings,
and a Memory Bank TTL cache defaulting to six hours. Exceeding those ceilings
risks account suspension, so a would-be breach is treated as an outage rather
than a retry. `python scripts/spacetrack_probe.py` verifies a live account and
prints the raw `cdm_public` column names, so the field mapping is confirmed
against the service instead of assumed from documentation.

### Persistent watches

Some conjunctions need multi-day assessment while tracking improves. The
`WatchCommander` supervisor persists watch state through the Memory Bank and
re-screens each pair every N hours. One canonical `watch_id` per pair makes
duplicate WATCH commands idempotent. On startup the service reloads every open
watch and resumes overdue checks on the first tick. Risk rising to HIGH parks
the watch in `AWAITING_HUMAN_APPROVAL` until an explicit
`POST /api/watches/{id}/approval`; risk falling to LOW closes it automatically.

```bash
curl -X POST "$API/api/watches" -H "X-API-Key: $KEY" \
  -d '{"sat_id": "SIM_PROTECTED_ASSET", "debris_id": "FENGYUN_1C_DEB", "interval_hours": 12}'
```

### The coordination gap

The most interesting finding in this project is not about agents.

Conjunction data is standardised: CDMs issued by 18/19 SDS, served through
Space-Track.org, in a documented CCSDS format. The coordination that follows is
not. "Will you move, or shall we" happens over email and phone calls between
assessment desks. Some operators coordinate through the Space Data Association,
NASA runs CARA for its own assets, SpaceX auto-manoeuvres Starlink. None of that
is a protocol a small operator can call.

`tools/coordination.py` does not pretend the gap is closed. From one screening
it emits a CCSDS-CDM-shaped KVN message carrying the keywords an assessment desk
reads first, and the coordination request a human would send. Covariance blocks
are omitted deliberately, because the screening uses a fixed covariance
assumption and fabricated covariance would be worse than none.
`channel: "protocol"` means a machine counterparty answered, which is the future
state this project proposes. `channel: "human"` means no machine counterparty
exists, which is the world today, and the mission records that it is awaiting an
out-of-band reply. Nothing fabricates a counterparty's agreement.

Two corrections the real feed forced. Payload-versus-payload conjunctions are
rare but not absent, roughly one in ten encounters, and those are the only ones
where negotiation is meaningful; a live STELLA / DMSP 5D-2 F9 pair screened HIGH
at Pc 6.3e-4. And `OBJECT_TYPE = PAYLOAD` does not mean manoeuvrable, since
STELLA is a passive laser-ranging sphere and DMSP 5D-2 F9 is defunct, so the
fields in `tools/space_track_api.py` are named `possibly_manoeuvrable` and
`manoeuvrability: "unknown" | "none"`.

The fleet therefore skips negotiation structurally when the counterparty cannot
manoeuvre, instead of hoping a model reasons its way there. Asking an LLM to
negotiate with a fragment of Fengyun-1C reliably produces a standoff, a deadlock
with an object that holds no position, which then escalates a HIGH-risk
conjunction for an arbitration no human can perform either.
`NEGOTIATION_SKIPPED / COUNTERPARTY_CANNOT_MANOEUVRE` is audited and every
safety gate stays in force.

### Consent is not authentication

The Safety Officer's rule R2 requires a signature of at least 64 hex characters
on every manoeuvre command. Two different claims were being conflated by one
signature-shaped field. `ack_signature` asserts that the counterparty agreed,
which is consent. `command_signature` asserts that this command is ours and
unaltered, which is authentication.

On the common path, debris, there is no counterparty to consent, so the fleet
leaves the acknowledgement empty instead of forging an agreement that never
happened. `geap_sim/command_signing.py` computes an HMAC-SHA256 over the fields
that determine the physical effect of the burn. Signing our own command proves
our integrity and still claims nothing about theirs.

---

## Additional Google models

***arch*** *Space segment, ground segment and generative media: the Gemma edge
autopilot and its handover trigger, the Fleet Admiral to pipeline fan-out, and
the Veo and Lyria branches off terminal mission state.*

**Gemma 4 edge autopilot** (`agents/edge_agent.py`). When a HIGH-risk
conjunction is mid-flight and the ground pipeline cannot finish, the mission
hands over to a satellite-side Gemma agent, which is the flight analogue of
losing downlink during an incident. It holds exactly one tool
(`emergency_dodge`), one 30-second decision window, and stricter physics than
Model Armor applies: Pc above 1e-3, a burn under the 50 m/s ceiling, and no
encroachment on the 5 percent strategic reserve. If inference itself is
unavailable a hardcoded ROM rule decides instead, because the spacecraft never
waits for a model. Every edge decision is tagged `EDGE_AUTONOMOUS` and the
feature is kill-switchable with `ORBIT_ENABLE_EDGE_AUTONOMY=0`.

**Veo 3 mission debriefs** (`tools/debrief_generator.py`). When a mission
terminates, a background task renders a summary of the encounter, generates a
debrief video and attaches it to the conjunction record in Firestore.
`GET /api/debrief/{conjunction_id}` serves generation status plus the artifact.

**Lyria 2 audio cues** (`tools/audio_generator.py`). Five event classes own a
generated audio identity, served as memoised WAV from
`GET /api/audio/{event_type}` and played by the mission feed as events arrive
over SSE.

Veo and Lyria calls are gated behind `ORBIT_ENABLE_REAL_VEO=1` and
`ORBIT_ENABLE_REAL_LYRIA=1`. Without them, or without Vertex credentials, both
degrade to clearly labelled deterministic simulations, an SVG reconstruction and
a procedural synth cue, so the demo works anywhere and never presents a mock as
a real render.

---

## Tech stack

| Component | Technology |
|---|---|
| Reasoning models | Gemini 3.7 Flash (triage, meta-critic, debate judge), Gemini 3.5 Flash (astrodynamics, negotiation, safety verdicts, strategists, learning analyst), all through Vertex AI |
| Edge model | Gemma 4 |
| Generative media | Veo 3 (debrief video), Lyria 2 (event audio) |
| Agent framework | Google ADK 2.7.1 |
| Orbital mechanics | python-sgp4 propagation, three-stage TCA refinement, Chan's first-order Gaussian Pc |
| Orbital data | Space-Track.org GP element sets and `cdm_public` CDMs, with a calibrated synthetic catalogue for tests |
| Memory | Firestore async client plus semantic vector recall (Vertex `text-embedding-005`, deterministic local embedder offline) |
| Cloud | Cloud Run, Firestore, Cloud Logging, Secret Manager |
| API | FastAPI, Uvicorn, Server-Sent Events |
| Command center | React, Vite, CesiumJS |
| Security | Constant-time API-key middleware, zero-trust agent registry, dual-layer manoeuvre gating, HMAC-SHA256 command signing |

---

## Testing and evaluation

The evaluation suite is hermetic by design. The real pipeline, Model Armor,
Memory Bank and audit trail run unmodified while the four specialist LLMs are
substituted with schema-valid scripted agents (`tests/evaluation/harness.py`).
No network, no credentials, no cost, and every orchestration guarantee below is
exercised by production code.

```bash
python tests/evaluation/run_evaluation.py    # exits non-zero on any failure
```

Latest full run: **126 of 126 checks green across 21 scenarios.**

| Group | Scenarios | What it proves |
|---|---|---|
| Risk banding | `low_risk_conjunction`, `medium_risk_hitl`, `high_risk_conjunction` | LOW never invokes negotiation or armor; MEDIUM always lands behind a human; the calibrated HIGH pair executes end to end with all four armor checks passing, fuel debited at exactly 0.5 percent per m/s, and the debrief generated on the same trace |
| Safety gates | `hallucination_guard`, `policy_ceiling`, `fuel_guard`, `pii_sweep` | Payload drift (13.9 against 8.0 m/s), an 80 m/s ceiling breach, a reserve-breaching burn at 6 percent fuel and a planted AWS key are each blocked with the correct violation label, and the key is never echoed into the audit trail |
| Degradation | `circuit_breaker` | A dead provider trips after exactly three attempts into structured human dispatch |
| Debate | six `debate_*` scenarios | Convergence, a fabricated citation disqualifying its author, a verbatim repeater frozen mid-debate, all three settling, safe total collapse, and the downstream armor still rejecting an over-ceiling payload |
| Evolution | four `evolution_*` scenarios plus `policy_changes_screening` | Gaming rejected, envelope clamp applied to an approved proposal, freeze after repeat rejections, and an applied cycle changing the next screening decision |
| Constellation | `constellation_optimization` | A byte-stable plan on replay, and no action outside `{dodge, hold}` ever emitted |
| Coordination | `debris_skips_negotiation` | Negotiation skipped structurally when the counterparty cannot manoeuvre |

### Chaos engineering

```bash
python tests/chaos/chaos_runner.py --i-know-this-is-destructive
```

| Scenario | Behaviour under attack |
|---|---|
| `chaos_kill_agent` | Orbital-mechanics node destroyed mid-mission: breaker retries three times, trips, `HUMAN_DISPATCH_DEGRADED`, zero fuel touched |
| `chaos_corrupt_state` | Negative, NaN and garbage vehicle records: read-boundary sanitisation keeps armor arithmetic finite, with negative fuel blocked at the reserve and NaN executing cleanly |
| `chaos_network_partition` | Refused sockets and black-hole stalls both degrade cleanly; refused in 0.04 s against a 1.5 s hang becoming 4.55 s |
| `chaos_rapid_fire` | 100 concurrent missions, 100 terminal, a unique trace each, around 160 missions per second of orchestration throughput |

Chaos testing paid for itself on the first run. The corruption scenario exposed
that a NaN fuel value could slip the original clamp chain, which is why
`MemoryBank.get_satellite_state` now sanitises non-finite and out-of-range
fields at the read boundary and audits every repair.

### Benchmarks

```bash
python tests/benchmarks/performance.py --save
```

| Benchmark | Samples | Mean | p50 | p95 | p99 |
|---|---|---|---|---|---|
| SGP4 conjunction screening | 150 | 0.61 ms | 0.60 ms | 0.68 ms | 1.04 ms |
| Model Armor four-check inspection | 200 | 0.09 ms | 0.09 ms | 0.11 ms | 0.14 ms |
| Memory Bank state read | 300 | <0.01 ms | <0.01 ms | 0.01 ms | 0.01 ms |
| Memory Bank burn write | 300 | 0.03 ms | 0.03 ms | 0.04 ms | 0.06 ms |
| End-to-end mission (offline harness) | 40 | 23.9 ms | 4.34 ms | 5.18 ms | 784 ms |
| `import app` cold-start floor | 3 | 2053 ms | 1975 ms | 2199 ms | 2199 ms |

A complete screen costs well under a millisecond of CPU and the entire
deterministic safety gate costs less than a tenth of one, which means production
latency is dominated by LLM inference, exactly where it should be. The p99
mission outlier is first-run module warm-up inside the measurement loop. Set
`ORBIT_BENCH_URL` against a running instance to append live HTTP latency to the
same report.

---

## Spin-up instructions

Run it locally in five steps, or skip to [Deploying to Cloud Run](#deploying-to-cloud-run)
for the single-command cloud path. Everything below has been executed on a clean
checkout.

### 1. Prerequisites

- Python 3.11 or newer
- Node 20 or newer, for the command center only
- A Google Cloud project with Vertex AI, Firestore and Cloud Run enabled
- The `gcloud` CLI, authenticated
- Optional: a Space-Track.org account for live orbital data. Without one the
  fleet runs on the calibrated synthetic catalogue and says so on screen.

### 2. Two Vertex locations, on purpose

Mixing these up produces a confusing `404 NOT_FOUND`.

| What | Location | Why |
|---|---|---|
| Gemini 3.x reasoning | `global` | Gemini 3.x is served only from the global endpoint; regional endpoints return 404 for every 3.x model |
| Veo and Lyria | `us-central1` | Generative media is regional only and is not on the global endpoint, the exact mirror image |
| Cloud Run service | `us-central1` | Unrelated to either; this is just where the container runs |

`GOOGLE_CLOUD_LOCATION=global` covers the first, `ORBIT_MEDIA_LOCATION` the
second, `deploy.sh --region` the third.

### 3. Backend

```bash
git clone https://github.com/Juniorlcsss/ORBIT
cd ORBIT
pip install -r requirements.txt
cp .env.example .env
```

Set at minimum:

```bash
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/service-account.json
ORBIT_API_KEY=$(openssl rand -hex 32)
```

`GOOGLE_GENAI_USE_VERTEXAI=TRUE` is not optional. Without it the google-genai
SDK targets the Gemini Developer API, looks for a `GOOGLE_API_KEY` that does not
exist, and every agent degrades through the circuit breaker to
`HUMAN_DISPATCH_DEGRADED`.

```bash
uvicorn app:app --reload --port 8080
curl -s localhost:8080/health
# {"status":"healthy","firestore_connected":true,"memory_backend":"firestore","api_key_enforced":true}
```

Check two startup log lines before trusting a run.
`MEMORY_BANK_BACKEND_SELECTED` should say `FIRESTORE`, and there should be no
`API_KEY_ENFORCEMENT_DISABLED` line. If you see `MEMORY` and that warning
together the environment did not load, and the UI will still look healthy,
because with no API key the gateway stops enforcing auth.
`firestore_connected: false` means the Memory Bank degraded, and
`MEMORY_BANK_FIRESTORE_UNAVAILABLE` names the reason.

Without GCP credentials everything still runs. The Memory Bank falls back to
in-process storage, vector recall to a deterministic local embedder, and
missions that need LLMs degrade to a clean `HUMAN_DISPATCH_DEGRADED`, so the
failure-tolerant path is itself demonstrable offline.

### 4. Command center

```bash
cd frontend
npm install
cp .env.example .env.local     # then set VITE_ORBIT_API_KEY
npm run dev                    # http://localhost:5173, proxies /api to :8080
```

`VITE_ORBIT_API_KEY` must match `ORBIT_API_KEY` in the backend's root `.env`. If
it is missing the dev server loads fine but every panel stays empty and the
backend logs a wall of `AUTH_REJECTED` lines. Vite only exposes `VITE_`-prefixed
variables and only reads them at dev-server start, so restart after editing.
Point at a different backend with `ORBIT_BACKEND_URL`, which is deliberately not
`VITE_`-prefixed so it cannot reach the client bundle.

### 5. Why the deployed console holds no API key

Vite inlines every `VITE_*` variable into the JavaScript at build time, so
whatever it holds becomes a plaintext string literal any visitor can read. There
is no way to keep a secret in a browser bundle; the only question is whether you
have noticed. `VITE_ORBIT_API_KEY` is therefore a local-development variable
only. Three layers keep it out of the image. `.gitignore` keeps
`frontend/.env.local` out of the repo; `.dockerignore` keeps it out of the build
context; and `Dockerfile.frontend` runs `rm -f .env .env.*` before `npm run
build`, so editing either of the first two cannot quietly reopen the hole.

The resulting bundle contains no credential. esbuild folds `if (API_KEY)` to a
constant false and drops the header assignment entirely, so the browser does not
even send an `X-API-KEY`. nginx attaches it instead, from the runtime
environment:

```nginx
proxy_set_header X-API-KEY "${ORBIT_API_KEY}";
```

That is why the console runs on Cloud Run and not a static host. A static
host gives you no server-side hop. The browser has to call the API directly, so
it has to carry the credential, and two routes on that surface spend real money:
`POST /api/conjunction_alert` runs the fifteen-agent pipeline, and
`POST /api/evolution/trigger` runs a policy-mutating cycle. Those two carry their
own nginx rate limit, 10 per minute with burst 3, separate from the roomy bucket
the two-second telemetry poll needs. Treat it as a speed bump against runaway
loops, not a security control: its key is the client-supplied
`X-Forwarded-For`, which anyone can spoof. The hard cost ceiling is
`--max-instances` plus the Vertex AI quota.

Because the browser reaches `/api` through the same origin that served the page,
no CORS grant is involved in the real traffic path either.

### Accessibility and multimodal UX

The console is a three-panel dark console: mission feed on the left, CesiumJS
globe in the centre, fleet status and armor log on the right. Every
accessibility control lives in the gear menu and persists to `localStorage`.

Four colour palettes (default, deuteranopia, protanopia, tritanopia) plus
monochrome. Every semantic colour resolves through a CSS custom property, so
switching preset re-tints the interface and the Cesium entities in one step,
with no rebuild and no second source of truth. Colour is never the only channel:
any non-default preset turns on shape coding automatically, and monochrome
forces it, because a palette with no hue cannot encode risk band by colour.

Reduced motion is seeded from `prefers-reduced-motion` and overridable in either
direction. Effects that carry state settle into a static form instead of
vanishing, so the high-risk perimeter stops pulsing but stays drawn. Blinking
stays well outside the 3 to 55 Hz seizure-risk band of WCAG 2.3.1. High contrast
and a type scale with Atkinson Hyperlegible are also available. Space triggers
an alert, F fullscreens the globe, ? opens the shortcut reference and Esc closes
the open dialog; global shortcuts are suppressed while a dialog is open and
while focus is in a text field, because Space is both the alert trigger and the
space bar. All dialogs trap focus and restore it on close.

Audio is a real channel, not decoration: the Lyria cues give each event
class its own identity, so an operator watching the globe hears the difference
between an authorised manoeuvre and a blocked one.

The header strip is derived from live state, never hardcoded. `FLEET ONLINE`
follows the SSE connection. The fuel segment reports the worst margin across
owned assets, and reads `FUEL UNKNOWN` where no Memory Bank record exists
instead of inventing a number. The data segment reads `DATA LIVE`,
`DATA SIMULATED` or `DATA ACQUIRING`, because that distinction is load-bearing
and belongs on screen where an operator can see it. The
high-risk alarm is scoped to conjunctions involving an asset this fleet
commands; screening the whole public catalogue made it permanently red, and an
indicator that is always on carries no information.

---

## Deploying to Cloud Run

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
export ORBIT_API_KEY="$(openssl rand -hex 32)"

chmod +x deploy.sh
./deploy.sh
```

The script deploys two scale-to-zero services capped at three instances each:
`orbit-fleet-commander` for the API and `orbit-command-center` for the console.
It enables the six APIs it needs, creates the Firestore database if absent, sets
the Vertex routing variables, pins the model IDs, and narrows the backend's
`ORBIT_CORS_ORIGINS` from `*` to the console origin once that URL exists.
`ORBIT_SKIP_FRONTEND=1` deploys the API alone.

Two service accounts each hold only what they need. `orbit-fleet-sa` gets
`roles/datastore.user` and `roles/aiplatform.user`. The console runs as
`orbit-web-sa`, which can read one secret and nothing else, since it has no
business reaching Firestore or Vertex AI.

Three values go through Secret Manager rather than `--set-env-vars`: the API
key, the Space-Track password and the command-signing key. Environment variables
on a Cloud Run service are readable by anyone holding `roles/viewer` and are
printed in full by `gcloud run services describe`. Access is granted per secret,
so neither account can enumerate the project's others.

The script refuses to deploy three things that would otherwise fail silently:

| Missing | Why it refuses | Override |
|---|---|---|
| `ORBIT_API_KEY` | Would publish an unauthenticated endpoint | `ORBIT_ALLOW_UNAUTHENTICATED_API=1` |
| `SPACETRACK_USERNAME` / `SPACETRACK_PASSWORD` | `ORBIT_LIVE_MODE` defaults to `auto`, which resolves to simulated when credentials are absent, giving a demo that claims live orbital data while serving a synthetic catalogue | `ORBIT_ALLOW_SIMULATED_DATA=1` |
| Firestore database | `ORBIT_MEMORY_BACKEND=auto` degrades to an in-process dict, so the fleet learns nothing between requests | Created automatically |

On Cloud Run, leave `GOOGLE_APPLICATION_CREDENTIALS` unset. The runtime service
account is injected and ADC picks it up.

**Cost control.** Both services scale to zero and cap at three instances, which
is the hard ceiling on runaway spend alongside the Vertex AI quota. Set a
billing budget and alert on the project before any long demo session. Either
service can be deleted after judging without affecting the evidence, since the
demo video and this repository carry the proof it ran:

```bash
gcloud run services delete orbit-command-center  --region us-central1
gcloud run services delete orbit-fleet-commander --region us-central1
```

---

## Exercising a deployed fleet

```bash
API=https://<your-service>.run.app
KEY=<your-api-key>

curl -X POST "$API/api/conjunction_alert" \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"sat_id": "SIM_PROTECTED_ASSET", "debris_id": "FENGYUN_1C_DEB",
       "alert_source": "SPACE_TRACK_API", "priority": "URGENT",
       "raw_message": "Conjunction warning: SIM_PROTECTED_ASSET approaching Fengyun-1C debris."}'
```

```json
{
  "trace_id": "9f1c...",
  "status": "EXECUTION_AUTHORIZED",
  "risk_band": "HIGH",
  "miss_distance_km": 0.0889,
  "pc": 0.000751,
  "action_taken": "they_dodge",
  "armor_violations": null,
  "conjunction_id": "SIM_PROTECTED_ASSET-X-FENGYUN_1C_DEB-TCA-..."
}
```

```bash
curl "$API/api/agent_tree"                    -H "X-API-Key: $KEY"  # live fleet hierarchy
curl "$API/api/armor_report/<trace_id>"       -H "X-API-Key: $KEY"  # full reasoning replay
curl "$API/api/debate/transcript/<trace_id>"  -H "X-API-Key: $KEY"  # debate arguments and flags
curl "$API/api/debrief/<conjunction_id>"      -H "X-API-Key: $KEY"  # Veo debrief status
curl -o alert.wav "$API/api/audio/ALERT_DETECTED" -H "X-API-Key: $KEY"
```

`/api/agent_tree` renders every agent's model, tools and temperature straight
from the running process, so the architecture described here can be checked
against the deployment instead of taken on trust. `/api/armor_report` returns
every triage result, screening number, coordination outcome, safety verdict,
armor check and breaker transition under one trace ID.

---

## Project structure

```
ORBIT/
|- app.py                    # FastAPI app: 24 endpoints, gateway middleware, watch supervisor
|- deploy.sh                 # Idempotent deploy, least-privilege SAs, Secret Manager, preflight guards
|- Dockerfile                # Backend container
|- Dockerfile.frontend       # Console container: Vite build, nginx with SSE-safe proxying
|- agents/
|  |- orchestrator.py        # FleetCommanderPipeline (BaseAgent), circuit breakers, edge fallback
|  |- admiral.py             # Constellation control plane: deterministic fuel triage
|  |- astro.py               # Astrodynamics specialist: SGP4, vector recall, live data
|  |- diplomat.py            # Coordination officer
|  |- safety.py              # Safety Officer: zero tools, fail-closed
|  |- edge_agent.py          # Gemma edge autopilot: one tool, ROM fallback
|  |- watcher.py             # WatchCommander: idempotent, crash-safe multi-day watches
|- debate/                   # Strategists, deterministic moderator, judge, transcript models
|- evolution/                # Policy, envelope, gaming heuristics, analyst, meta-critic, engine
|- geap_sim/                 # Registry, memory bank, model armor, command signing, observability
|- tools/                    # SGP4 screening, Space-Track client, coordination, Veo, Lyria
|- frontend/                 # React command center: Cesium globe, SSE feed, accessibility
|- scripts/                  # spacetrack_probe.py and operational helpers
|- tests/                    # evaluation (21 scenarios), chaos (4), benchmarks
```

---

## Findings and learnings

Six things the build taught us that were not obvious when it started. Each links
to where the detail lives.

- **The hard problem is not screening, it is coordination.** Conjunction data is
  standardised and machine-readable; deciding who moves is still email and phone
  calls. See [The coordination gap](#the-coordination-gap).
- **The catalogue does not carry the fact you need.** `OBJECT_TYPE = PAYLOAD`
  says nothing about whether an object can manoeuvre, so the fields are named
  `possibly_manoeuvrable` and the fleet skips negotiation structurally rather
  than asking a model to negotiate with debris.
- **A published CDM beats your own propagation, badly.** The same pair screened
  LOW at 195.9 km from our SGP4 and HIGH at 71 m from the 18 SDS CDM. Any system
  that quietly prefers its own numbers will be confidently wrong.
- **Graceful degradation is where the silent failures hide.** Every fallback we
  added, Space-Track, embeddings, Firestore, media, could serve plausible
  fiction under a banner claiming live data. The fix was to make provenance a
  first-class value on screen and a deploy-time refusal in `deploy.sh`, not a
  log line.
- **Chaos testing found a bug the unit tests could not.** A NaN fuel value slid
  through the original clamp chain, which is why sanitisation now happens at the
  Memory Bank read boundary.
- **Self-improvement needs an adversary and a hard clamp, not one or the
  other.** The Meta-Critic catches narrative-level gaming that pattern matching
  misses; the deterministic envelope catches what a persuasive proposal talks
  the critic into. See
  [Self-evolving screening policy](#self-evolving-screening-policy).

---

## Known limitations

This is a hackathon prototype, and its edges are stated, not hidden.

- **Two catalogues, and only one is the command picture.** `/api/orbital_state`
  is live only. It serves objects and conjunctions built from Space-Track GP
  elsets and the public CDM feed, and returns `status: "unavailable"` with a
  reason instead of falling back to anything synthetic, because a
  plausible-but-fictional map looks exactly like a working one. The synthetic
  catalogue survives strictly as test fixtures, calibrated so the scenario
  screens as a genuine HIGH conjunction under real SGP4. Counterparty fleets
  live at `*.example` endpoints.
- **TLE screening is far coarser than a published CDM, so the CDM wins.**
  Verified live: the same pair screened LOW at 195.9 km from our own propagation
  while the 18 SDS CDM said HIGH at 71 m. TLEs carry roughly kilometre-scale
  error. Where an official CDM exists it takes precedence
  (`method: published_cdm/18sds`) and our propagation supplies only what the CDM
  omits, namely relative velocity at TCA. The consequence before a live demo: a
  mission screens HIGH only when Space-Track has actually released a CDM for
  that pair.
- **Acknowledgements are format-checked, not trust-anchored.** A counterparty
  `ack_signature` proves the integrity of our simulation, not a real operator's
  consent, and on the debris path it is left empty, not forged. Our own
  `command_signature` is a real HMAC and is verified.
- **Command signing falls back to a per-process key.** With
  `ORBIT_COMMAND_SIGNING_KEY` unset the fleet mints a random key at boot and
  audits the degradation. Signatures then hold within the process, so a tampered
  command still fails verification, but nobody outside can verify them and two
  replicas cannot verify each other.
- **Linear fuel model.** 0.5 percentage points per m/s, a documented stand-in
  for the rocket equation with live mass data.
- **Single-worker sessions.** `InMemorySessionService` keeps the demo cheap;
  horizontal scaling needs ADK's database-backed session service.
- **Audit replay is process-local.** `/api/armor_report` reads a bounded ring
  buffer; the durable trail is Cloud Logging.
- **App-level API keys rather than OIDC or IAP.** Deliberate for judge
  accessibility; production would front this with proper identity.
- **There is no `gemini-3.5-pro`.** The 3.x pro tier currently tops out at
  `gemini-3.1-pro-preview`, below this hackathon's 3.5 floor, so the
  heavy-reasoning roles run `gemini-3.7-flash`. Every model ID is env-tunable.
- **Vertex AI quota is the practical ceiling on live demos.** Back-to-back
  HIGH-risk missions can draw `429 RESOURCE_EXHAUSTED` on a fresh project. The
  breakers absorb it and degrade cleanly, but request a quota bump before a long
  session.
- **Veo and Lyria artifacts are simulated unless explicitly enabled.**
- **Edge autonomy is a demo envelope, not flight certification.** The thresholds
  mirror the ground policy but nothing here is CCSDS-qualified hardware.
- **Vector recall uses a deterministic hashing embedder offline.** It clusters
  templated situation descriptions well; adding credentials upgrades it to the
  audited Vertex `text-embedding-005` path automatically.
- **Watch crash recovery is cross-process only on Firestore.** The in-memory
  backend proves the resume logic within one process.
- **Live LLM output occasionally misses the schema on the first attempt.** The
  breaker's schema validation catches it and the retry succeeds. This is the
  breaker working, and it is visible in the audit trail.
- **Firestore composite indexes are deliberately avoided.** History filters
  server-side and sorts in Python so the project works on a brand-new database
  with no manual index creation. At fleet scale, create the composite index and
  restore the server-side `order_by`.
- **Space-Track CDM field mapping tolerates two vocabularies.** `cdm_public`
  uses `PC` and `MIN_RNG` while full CCSDS 508.0-B-1 uses
  `COLLISION_PROBABILITY` and `MISS_DISTANCE` in metres. Both are read with
  per-vocabulary unit conversion, and Pc parsing includes a documented heuristic
  treating values above 0.5 as percentages.
- **Space-Track rate limits are a hard operational ceiling.** 30 per minute and
  300 per hour, with account suspension as the penalty. The client self-limits
  below both (`SPACETRACK_MAX_PER_MINUTE=20`, `SPACETRACK_MAX_PER_HOUR=250`) and
  raises `SpaceTrackUnavailable` before it will breach one. Cached payloads are
  schema-versioned, because a stale cache silently served two fixes' worth of
  old data during development.

---

## Future work

- Swap `geap_sim` for the managed Gemini Enterprise Agent Platform, one module
  at a time; the seams are already isolated.
- Quantised on-device Gemma so edge autonomy works with zero connectivity and no
  Vertex round trip.
- Conjunction storms: `LoopAgent` continuous monitoring and multi-object
  deconfliction for when one manoeuvre creates new conjunctions downstream.
- Production hardening: Terraform, a frontend build step in CI, OIDC end to end,
  and Firestore-backed sessions for multi-instance deployments.

---

## Development methodology

This project was built with an AI-augmented workflow. Claude and similar
assistants were used for scaffolding, test generation and documentation polish,
disclosed here in accordance with hackathon rules. Architectural decisions,
orbital-mechanics calibration, safety-policy design and the GEAP integration
choices were made by the human developer.

## Team

**Jonathan Randall**, solo developer. Devpost handle and contact on the
submission page.

## License

MIT, see [LICENSE](LICENSE).
