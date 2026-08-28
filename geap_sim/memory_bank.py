"""GEAP simulation — persistent mission memory (Firestore-backed).

Simulates the GEAP **Memory Bank**: durable, cross-session context for the
fleet. Three Firestore collections model the domain:

* ``satellites/{sat_id}``        — live vehicle state: fuel percentage,
  thruster health, cumulative delta-v expended, last-updated stamp.
* ``conjunctions/{conjunction_id}`` — immutable screening history: risk band,
  Pc, miss distance, TCA and the action ultimately taken — plus a semantic
  ``context_embedding`` so the fleet can recall similar past encounters.
* ``watches/{watch_id}``         — long-running conjunction watch state that
  survives process restarts (see ``agents/watcher.py``).

Vector recall (Session State → Vector Search hierarchy)
-------------------------------------------------------
Every logged conjunction is embedded — Vertex AI ``text-embedding-005``
when credentials exist, otherwise a deterministic local hashing embedder —
and stored alongside the event. ``find_similar_conjunctions`` embeds a new
situation and returns the most similar past events *with their outcomes*,
which is how the AstrodynamicsAgent grounds recommendations in fleet
history instead of starting from scratch. Production note: on real
Firestore this becomes a ``FindNearest`` K-nearest-neighbours query over a
``VectorEmbedding`` field (or Vertex AI Vector Search / Vertex Vector
Search index for fleet-scale corpora); the scan-based search here is the
portable simulation of exactly that query.

Local-dev resilience
--------------------
Production code paths hit real Firestore through ``AsyncClient``;
if credentials are absent and no ``FIRESTORE_EMULATOR_HOST`` is configured —
the typical hackathon-laptop situation — construction fails and the bank
transparently degrades to an in-process dictionary backend. Callers cannot
tell the difference; every method signature and return shape is identical.
Backend selection is audited at startup so judges can see which mode ran.

The selected backend is controlled by ``ORBIT_MEMORY_BACKEND``
(``auto`` default | ``firestore`` | ``memory``).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Final

from geap_sim.observability import audit_logger

SATELLITES_COLLECTION: Final[str] = "satellites"
CONJUNCTIONS_COLLECTION: Final[str] = "conjunctions"
WATCHES_COLLECTION: Final[str] = "watches"
OUTCOMES_COLLECTION: Final[str] = "mission_outcomes"
EVOLUTION_COLLECTION: Final[str] = "evolution_cycles"
META_COLLECTION: Final[str] = "evolution_meta"

#: Dimensionality of the deterministic local hashing embedder. Vertex AI
#: embeddings (768-d for text-embedding-005) take precedence whenever the
#: client initialises; mixed-dimension vectors are simply skipped at query
#: time rather than compared incorrectly.
_LOCAL_EMBED_DIMS: Final[int] = 256

_VERTEX_EMBED_MODEL: Final[str] = os.environ.get("ORBIT_EMBED_MODEL_ID", "text-embedding-005")

#: Singleton state for the embedding backend selection (audited once).
_embedding_backend: str | None = None

#: Simulated specific-impulse mapping: fuel-percentage points burned per
#: m/s of delta-v. Deliberately simple and documented; a real mission would
#: integrate the rocket equation with live mass data.
FUEL_PERCENT_PER_DV_MPS: Final[float] = 0.5

_DEFAULT_STATE_TEMPLATE: Final[dict[str, Any]] = {
    "fuel_percentage": 100.0,
    "thruster_health": 100.0,
    "total_dv_expended": 0.0,
}

_SAFE_DOC_ID: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]")


def estimate_fuel_after_burn(current_fuel_percent: float, dv_mps: float) -> float:
    """Project the fuel percentage remaining after a burn (never below 0)."""
    projected = float(current_fuel_percent) - abs(float(dv_mps)) * FUEL_PERCENT_PER_DV_MPS
    return max(0.0, round(projected, 4))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_document_id(raw: str) -> str:
    """Coerce arbitrary text into a Firestore-safe document ID."""
    return _SAFE_DOC_ID.sub("-", raw).strip("-.")[:150] or "unnamed"


# ---------------------------------------------------------------------------
# Vector recall — embeddings for "similar past conjunctions"
# ---------------------------------------------------------------------------


def build_conjunction_context(record: dict[str, Any]) -> str:
    """Canonical semantic description of one conjunction situation.

    This exact string is what gets embedded, so recall quality depends on
    keeping it stable over time. Format follows the fleet's memory-webinar
    template; ``debris_class`` is derived from the catalogue identifier.
    """
    debris_id = str(record.get("debris_id", "")).upper()
    if "_DEB" in debris_id or "DEBRIS" in debris_id:
        debris_class = f"debris field ({debris_id.split('_')[0].lower()} family)"
    elif "COSMOS" in debris_id:
        debris_class = "defunct cosmos-family payload debris"
    else:
        debris_class = f"tracked object ({debris_id.lower()})"
    fuel = record.get("fuel_percentage")
    fuel_text = f"{float(fuel):.1f}" if isinstance(fuel, (int, float)) else "unknown"
    return (
        f"{str(record.get('risk_band', 'UNKNOWN')).upper()} conjunction at "
        f"{float(record.get('miss_distance_km') or 0.0):.4f}km, Pc={float(record.get('pc') or 0.0):.2e}, "
        f"satellite fuel={fuel_text}%, debris type={debris_class}"
    )


def _hash_embed(text: str) -> list[float]:
    """Deterministic local embedding: hashed unigrams + bigrams, L2-normalised.

    No model download, no network, byte-stable across restarts — the honest
    fallback that keeps vector recall functional offline. Weaker than a
    neural encoder but perfectly adequate for clustering templated
    situation descriptions like ours.
    """
    tokens = re.findall(r"[a-z0-9]+(?:\.[0-9e+-]+)?", text.lower())
    grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    vec = [0.0] * _LOCAL_EMBED_DIMS
    for gram in grams:
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % _LOCAL_EMBED_DIMS
        sign = 1.0 if digest[0] & 1 else -1.0
        vec[index] += sign
    norm = math.sqrt(sum(component * component for component in vec)) or 1.0
    return [round(component / norm, 6) for component in vec]


def _vertex_embed(text: str) -> list[float]:
    """Vertex AI text embedding via google-genai (production path)."""
    from google import genai

    client = genai.Client(vertexai=True)
    response = client.models.embed_content(model=_VERTEX_EMBED_MODEL, contents=[text])
    values = list(response.embeddings[0].values)
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 7) for value in values]


async def embed_context(text: str) -> list[float]:
    """Embed one context string: Vertex AI when available, local hash otherwise.

    The chosen backend is audited exactly once per process so operators can
    see which memory tier is live (Session State → Vector Search → Managed
    Cloud Memory).
    """
    global _embedding_backend
    if _embedding_backend is None:
        try:
            probe = await asyncio.to_thread(_vertex_probe)
            _embedding_backend = "vertex_ai_text_embedding" if probe else "local_hash_embedding"
        except Exception:  # noqa: BLE001 — any failure means offline tier
            _embedding_backend = "local_hash_embedding"
        audit_logger.log_event(
            trace_id="memory-bank",
            agent_name="geap_sim.memory_bank",
            event_type="EMBEDDING_BACKEND_SELECTED",
            payload={"backend": _embedding_backend, "model": _VERTEX_EMBED_MODEL if "vertex" in _embedding_backend else "blake2_hashing"},
            status=_embedding_backend.upper(),
        )
    if _embedding_backend == "vertex_ai_text_embedding":
        try:
            return await asyncio.to_thread(_vertex_embed, text)
        except Exception:  # noqa: BLE001 — degrade mid-flight too
            pass
    return _hash_embed(text)


def _vertex_probe() -> bool:
    """Cheap liveness check that the Vertex embedding path can initialise."""
    from google import genai

    genai.Client(vertexai=True)
    return True


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity; vectors must share dimensionality."""
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return dot / (na * nb)


def _credential_diagnostics() -> dict[str, Any]:
    """Report what the process can actually see, for startup auditing.

    ``GOOGLE_APPLICATION_CREDENTIALS`` is never read by this module directly:
    it is the Application Default Credentials mechanism, consumed by
    ``google-auth`` when the Firestore client is constructed. We only record
    whether it points at a file that exists, because a relative path (e.g.
    ``./service-account.json``) resolves against the *current working
    directory*, which differs between local ``uvicorn`` runs and Cloud Run.
    """
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    return {
        "google_cloud_project": os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        "credentials_path": cred_path,
        "credentials_file_found": bool(cred_path) and os.path.isfile(cred_path),
        "emulator_host": os.getenv("FIRESTORE_EMULATOR_HOST") or None,
    }


def _audit_firestore_failure(stage: str, exc: BaseException) -> None:
    """Log why Firestore was unavailable — audit trail *and* stderr.

    Previously this failure was swallowed by a bare ``except ImportError``,
    so a wrong client symbol was indistinguishable from a laptop with no
    credentials. Both now name themselves.
    """
    detail = f"{type(exc).__name__}: {exc}"
    audit_logger.log_event(
        trace_id="startup",
        agent_name="geap_sim.memory_bank",
        event_type="MEMORY_BANK_FIRESTORE_UNAVAILABLE",
        payload={"stage": stage, "error": detail, **_credential_diagnostics()},
        status="DEGRADED",
    )
    print(f"[memory_bank] Firestore unavailable at {stage}: {detail}", file=sys.stderr)


class MemoryBank:
    """Persistent satellite state + conjunction history (Firestore or memory).

    All methods are async so the call sites inside the agent pipeline remain
    identical regardless of which backend is active.
    """

    def __init__(self) -> None:
        self._backend: str = self._select_backend()
        self._memory_store: dict[tuple[str, str], dict[str, Any]] = {}
        self._clients: dict[int, Any] = {}
        if self._backend == "firestore":
            try:
                from google.cloud.firestore import AsyncClient

                AsyncClient()
            except Exception as exc:  # noqa: BLE001 — degrade, but never silently
                self._backend = "memory"
                _audit_firestore_failure("client_init", exc)
        audit_logger.log_event(
            trace_id="startup",
            agent_name="geap_sim.memory_bank",
            event_type="MEMORY_BANK_BACKEND_SELECTED",
            payload={"backend": self._backend, **_credential_diagnostics()},
            status=self._backend.upper(),
        )

    # -- backend selection ---------------------------------------------------

    def _select_backend(self) -> str:
        mode = os.getenv("ORBIT_MEMORY_BACKEND", "auto").strip().lower()
        if mode == "memory":
            return "memory"
        try:
            from google.cloud.firestore import AsyncClient  # noqa: F401
        except Exception as exc:  # noqa: BLE001 — surface the real reason
            _audit_firestore_failure("import", exc)
            if mode == "firestore":
                raise RuntimeError(
                    "ORBIT_MEMORY_BACKEND=firestore but google.cloud.firestore."
                    f"AsyncClient could not be imported: {exc!r}"
                ) from exc
            return "memory"
        if not (os.getenv("FIRESTORE_EMULATOR_HOST") or os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")):
            if mode == "firestore":
                return "firestore"  # explicit override: let auth errors surface loudly at call time
            # Auto mode without credentials/emulator/project → degrade quietly.
            return "memory"
        return "firestore"

    def _db(self) -> Any:
        """Return a Firestore client bound to the *currently running* loop.

        Must only be called from async context on the firestore backend.
        """
        loop_key = id(asyncio.get_running_loop())
        client = self._clients.get(loop_key)
        if client is None:
            from google.cloud.firestore import AsyncClient

            client = AsyncClient()
            self._clients[loop_key] = client
        return client

    @property
    def backend_name(self) -> str:
        """Active persistence backend: ``firestore`` or ``memory``."""
        return self._backend

    # -- satellites ----------------------------------------------------------

    async def get_satellite_state(self, sat_id: str) -> dict[str, Any]:
        """Return the current state document for a satellite.

        Missing documents resolve to nominal defaults (100% fuel/health)
        rather than raising, so first-contact alerts always proceed.
        Corrupted numeric fields (negative, non-numeric, NaN/Inf — e.g.
        after a partial write or external tampering) are sanitised to safe
        defaults at this read boundary and audited, so downstream armour
        arithmetic can never poison itself with non-finite values.
        """
        key = safe_document_id(sat_id).upper()
        if self._backend == "firestore":
            snapshot = await self._db().collection(SATELLITES_COLLECTION).document(key).get()
            stored: dict[str, Any] = dict(snapshot.to_dict() or {})
        else:
            stored = dict(self._memory_store.get((SATELLITES_COLLECTION, key), {}))

        state = {**_DEFAULT_STATE_TEMPLATE, **stored}
        state["sat_id"] = key
        sanitised: list[str] = []
        for field_name, default, low, high in (
            ("fuel_percentage", 100.0, 0.0, 100.0),
            ("thruster_health", 100.0, 0.0, 100.0),
            ("total_dv_expended", 0.0, 0.0, float("inf")),
        ):
            original = state.get(field_name)
            try:
                value = float(original)
                if not math.isfinite(value):
                    raise ValueError("non-finite")
            except (TypeError, ValueError):
                value, corrupted = default, True
            else:
                if value < low:
                    value, corrupted = low, True
                elif value > high:
                    value, corrupted = high, True
                else:
                    corrupted = False
            if corrupted:
                sanitised.append(f"{field_name}:{original!r}->{value}")
            state[field_name] = round(value, 4) if field_name != "total_dv_expended" else round(max(0.0, value), 4)

        if sanitised:
            audit_logger.log_event(
                trace_id="memory-bank",
                agent_name="geap_sim.memory_bank",
                event_type="SATELLITE_STATE_CORRUPTED_SANITISED",
                payload={"sat_id": key, "fields": sanitised},
                status="DEGRADED",
            )
        state.setdefault("last_updated", None)
        return state

    async def update_satellite_state(self, sat_id: str, delta_v_expended: float, new_fuel: float) -> dict[str, Any]:
        """Record a burn: accumulate delta-v, set new fuel, refresh stamp."""
        key = safe_document_id(sat_id).upper()
        current = await self.get_satellite_state(key)
        updated: dict[str, Any] = {
            "sat_id": key,
            "fuel_percentage": max(0.0, min(100.0, round(float(new_fuel), 4))),
            "thruster_health": current["thruster_health"],
            "total_dv_expended": round(current["total_dv_expended"] + max(0.0, float(delta_v_expended)), 4),
            "last_updated": _utc_now_iso(),
        }
        await self._write(SATELLITES_COLLECTION, key, updated)
        audit_logger.log_event(
            trace_id="memory-bank",
            agent_name="geap_sim.memory_bank",
            event_type="SATELLITE_STATE_UPDATED",
            payload=updated,
            status="EXECUTED",
        )
        return updated

    # -- conjunction history ---------------------------------------------------

    async def log_conjunction_event(self, conjunction_id: str, event_data: dict[str, Any]) -> dict[str, Any]:
        """Persist one screening/outcome record keyed by conjunction ID.

        As part of the write, the situation is semantically embedded and the
        vector stored on the document — this is the moment the fleet learns,
        so every future decision can recall this encounter.
        """
        key = safe_document_id(conjunction_id)
        record: dict[str, Any] = {
            "recorded_utc": _utc_now_iso(),
            **event_data,
            "conjunction_id": key,
        }
        try:
            if record.get("fuel_percentage") is None:
                state = await self.get_satellite_state(str(record.get("sat_id", "")))
                record["fuel_percentage"] = state["fuel_percentage"]
            record["context_text"] = build_conjunction_context(record)
            record["context_embedding"] = await embed_context(record["context_text"])
            record["embedding_dims"] = len(record["context_embedding"])
        except Exception as exc:  # noqa: BLE001 — recall is an enhancement; logging must never fail
            audit_logger.log_event(
                trace_id="memory-bank",
                agent_name="geap_sim.memory_bank",
                event_type="VECTOR_EMBEDDING_FAILED",
                payload={"conjunction_id": key, "error_type": type(exc).__name__},
                status="DEGRADED",
            )
        await self._write(CONJUNCTIONS_COLLECTION, key, record)
        return record

    async def find_similar_conjunctions(self, current_context: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Top-k most similar past conjunctions *with their outcomes*.

        Embeds ``current_context`` with the active backend and scans stored
        vectors (production Firestore would issue a FindNearest KNN query or
        hit Vertex AI Vector Search — see module docstring). Vectors from a
        different embedding space than the query are skipped rather than
        compared incorrectly. Similarity is cosine distance in [-1, 1].
        """
        top_k = max(1, min(int(top_k), 25))
        query_vector = await embed_context(current_context)

        candidates: list[dict[str, Any]] = []
        if self._backend == "firestore":
            docs = [dict(doc.to_dict() or {}) async for doc in self._db().collection(CONJUNCTIONS_COLLECTION).stream()]
        else:
            docs = [dict(doc) for (collection, _), doc in sorted(self._memory_store.items()) if collection == CONJUNCTIONS_COLLECTION]

        for doc in docs:
            vector = doc.get("context_embedding")
            if not isinstance(vector, list) or len(vector) != len(query_vector):
                continue
            score = _cosine_similarity(query_vector, vector)
            if score <= -1.0:
                continue
            candidates.append(
                {
                    "conjunction_id": doc.get("conjunction_id"),
                    "sat_id": doc.get("sat_id"),
                    "debris_id": doc.get("debris_id"),
                    "risk_band": doc.get("risk_band"),
                    "pc": doc.get("pc"),
                    "miss_distance_km": doc.get("miss_distance_km"),
                    "action_taken": doc.get("action_taken"),
                    "final_status": doc.get("final_status"),
                    "our_dv_mps": doc.get("our_dv_mps"),
                    "tca_iso": doc.get("tca_iso"),
                    "recorded_utc": doc.get("recorded_utc"),
                    "context_text": doc.get("context_text"),
                    "similarity": round(score, 4),
                }
            )
        candidates.sort(key=lambda item: item["similarity"], reverse=True)
        return candidates[:top_k]

    async def get_historical_conjunctions(self, sat_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Most-recent-first conjunction history for one satellite."""
        key = safe_document_id(sat_id).upper()
        limit = max(1, int(limit))
        if self._backend == "firestore":
            from google.cloud.firestore_v1.base_query import FieldFilter

            query = self._db().collection(CONJUNCTIONS_COLLECTION).where(
                filter=FieldFilter("sat_id", "==", key)
            )
            docs = [dict(doc.to_dict() or {}) async for doc in query.stream()]
            docs.sort(key=lambda d: str(d.get("recorded_utc", "")), reverse=True)
            return docs[:limit]
        matches = [
            dict(doc)
            for (collection, doc_key), doc in sorted(self._memory_store.items())
            if collection == CONJUNCTIONS_COLLECTION and doc.get("sat_id") == key
        ]
        matches.sort(key=lambda doc: str(doc.get("recorded_utc", "")), reverse=True)
        return matches[:limit]

    async def get_conjunction_event(self, conjunction_id: str) -> dict[str, Any] | None:
        """Fetch one conjunction record by ID; ``None`` when unknown."""
        key = safe_document_id(conjunction_id)
        if self._backend == "firestore":
            snapshot = await self._db().collection(CONJUNCTIONS_COLLECTION).document(key).get()
            stored = snapshot.to_dict()
            return dict(stored) if stored else None
        return self._memory_store.get((CONJUNCTIONS_COLLECTION, key))

    async def append_conjunction_fields(self, conjunction_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Merge ``fields`` into an existing conjunction document.

        Used by background writers (e.g. the Veo debrief task) that attach
        artifacts to a record the pipeline already persisted. Returns the
        merged document, or ``None`` when the conjunction does not exist.
        """
        current = await self.get_conjunction_event(conjunction_id)
        if current is None:
            return None
        merged = {**current, **fields}
        await self._write(CONJUNCTIONS_COLLECTION, safe_document_id(conjunction_id), merged)
        return merged

    # -- long-running watches (crash-recoverable state) ------------------------

    async def upsert_watch(self, watch_id: str, watch_data: dict[str, Any]) -> dict[str, Any]:
        """Create or overwrite one conjunction-watch document."""
        key = safe_document_id(watch_id)
        record: dict[str, Any] = {**watch_data, "watch_id": key}
        await self._write(WATCHES_COLLECTION, key, record)
        return record

    async def get_watch(self, watch_id: str) -> dict[str, Any] | None:
        """Fetch one watch by ID; ``None`` when unknown."""
        key = safe_document_id(watch_id)
        if self._backend == "firestore":
            snapshot = await self._db().collection(WATCHES_COLLECTION).document(key).get()
            stored = snapshot.to_dict()
            return dict(stored) if stored else None
        return self._memory_store.get((WATCHES_COLLECTION, key))

    async def list_watches(self, status: str | None = None) -> list[dict[str, Any]]:
        """All watches, optionally filtered by status (oldest first)."""
        if self._backend == "firestore":
            query = self._db().collection(WATCHES_COLLECTION)
            if status:
                from google.cloud.firestore_v1.base_query import FieldFilter

                query = query.where(filter=FieldFilter("status", "==", status))
            docs = [dict(doc.to_dict() or {}) async for doc in query.stream()]
        else:
            docs = [
                dict(doc)
                for (collection, _), doc in sorted(self._memory_store.items())
                if collection == WATCHES_COLLECTION and (status is None or doc.get("status") == status)
            ]
        docs.sort(key=lambda doc: str(doc.get("created_utc", "")))
        return docs

    # -- generic TTL cache (Space-Track rate-limit shield) ----------------------

    async def cache_put(self, collection: str, cache_key: str, payload: Any) -> dict[str, Any]:
        """Store one cached API payload with its fetch timestamp."""
        key = f"{collection}:{safe_document_id(cache_key)}".replace(":", "-")
        record = {"cached_utc": _utc_now_iso(), "cache_key": cache_key, "payload": payload}
        await self._write(collection, key, record)
        return record

    async def cache_get(self, collection: str, cache_key: str, max_age_seconds: float) -> Any | None:
        """Cached payload if present and younger than ``max_age_seconds``."""
        key = f"{collection}:{safe_document_id(cache_key)}".replace(":", "-")
        if self._backend == "firestore":
            snapshot = await self._db().collection(collection).document(key).get()
            stored = snapshot.to_dict()
            record = dict(stored) if stored else None
        else:
            record = self._memory_store.get((collection, key))
        if not record:
            return None
        try:
            fetched = datetime.fromisoformat(str(record.get("cached_utc")))
            age = datetime.now(timezone.utc) - fetched
            if age.total_seconds() > max_age_seconds:
                return None
        except ValueError:
            return None
        return record.get("payload")

    # -- generic single-doc API (policy store & evolution meta) ------------------

    async def put_doc(self, collection: str, doc_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Create or overwrite one document by ID."""
        key = safe_document_id(doc_id)
        record = {**data, "doc_key": key}
        await self._write(collection, key, record)
        return record

    async def get_doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """Fetch one document by ID; ``None`` when unknown."""
        key = safe_document_id(doc_id)
        if self._backend == "firestore":
            snapshot = await self._db().collection(collection).document(key).get()
            stored = snapshot.to_dict()
            return dict(stored) if stored else None
        return self._memory_store.get((collection, key))

    # -- mission outcomes (self-evolution feedback signal) ------------------------

    async def log_outcome(self, outcome: dict[str, Any], outcome_id: str | None = None) -> dict[str, Any]:
        """Persist one mission outcome for the learning loop."""
        key = safe_document_id(outcome_id or f"{outcome.get('conjunction_id', 'outcome')}-{_utc_now_iso()}")
        record: dict[str, Any] = {"recorded_utc": _utc_now_iso(), **outcome, "outcome_id": key}
        await self._write(OUTCOMES_COLLECTION, key, record)
        return record

    async def get_recent_outcomes(self, limit: int = 20) -> list[dict[str, Any]]:
        """Newest-first mission outcomes (the analyst's evidence base)."""
        limit = max(1, int(limit))
        if self._backend == "firestore":
            query = (
                self._db().collection(OUTCOMES_COLLECTION)
                .order_by("recorded_utc", direction="DESCENDING")
                .limit(limit)
            )
            return [dict(doc.to_dict() or {}) async for doc in query.stream()]
        docs = [dict(doc) for (collection, _), doc in sorted(self._memory_store.items(), reverse=True) if collection == OUTCOMES_COLLECTION]
        docs.sort(key=lambda d: str(d.get("recorded_utc", "")), reverse=True)
        return docs[:limit]

    # -- evolution cycle history ---------------------------------------------------

    async def log_evolution_cycle(self, cycle: dict[str, Any]) -> dict[str, Any]:
        """Persist one completed/rejected evolution cycle (full audit diff)."""
        key = safe_document_id(f"cycle-{cycle.get('trace_id', 'unknown')}")
        record: dict[str, Any] = {"recorded_utc": _utc_now_iso(), **cycle, "cycle_id": key}
        await self._write(EVOLUTION_COLLECTION, key, record)
        return record

    async def get_evolution_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Newest-first evolution cycles — the before/after audit trail."""
        limit = max(1, int(limit))
        if self._backend == "firestore":
            query = (
                self._db().collection(EVOLUTION_COLLECTION)
                .order_by("recorded_utc", direction="DESCENDING")
                .limit(limit)
            )
            return [dict(doc.to_dict() or {}) async for doc in query.stream()]
        docs = [dict(doc) for (collection, _), doc in sorted(self._memory_store.items(), reverse=True) if collection == EVOLUTION_COLLECTION]
        docs.sort(key=lambda d: (str(d.get("recorded_utc", "")), str(d.get("cycle_id", ""))), reverse=True)
        return docs[:limit]

    # -- evolution meta state (freeze flag, rejection counters) --------------------

    async def set_meta(self, meta_key: str, value: Any) -> dict[str, Any]:
        """Write one evolution control value (frozen flag, counters...)."""
        return await self.put_doc(META_COLLECTION, meta_key, {"value": value})

    async def get_meta(self, meta_key: str, default: Any = None) -> Any:
        """Read one evolution control value; ``default`` when unset."""
        doc = await self.get_doc(META_COLLECTION, meta_key)
        return doc.get("value", default) if doc else default

    # -- internals -------------------------------------------------------------

    async def _write(self, collection: str, doc_key: str, data: dict[str, Any]) -> None:
        if self._backend == "firestore":
            await self._db().collection(collection).document(doc_key).set(data, merge=True)
        else:
            self._memory_store[(collection, doc_key)] = dict(data)


_shared_memory_bank: MemoryBank | None = None


def get_shared_memory_bank() -> MemoryBank:
    """Process-wide MemoryBank singleton (constructed on first use)."""
    global _shared_memory_bank
    if _shared_memory_bank is None:
        _shared_memory_bank = MemoryBank()
    return _shared_memory_bank


__all__ = [
    "CONJUNCTIONS_COLLECTION",
    "EVOLUTION_COLLECTION",
    "FUEL_PERCENT_PER_DV_MPS",
    "MemoryBank",
    "META_COLLECTION",
    "OUTCOMES_COLLECTION",
    "SATELLITES_COLLECTION",
    "WATCHES_COLLECTION",
    "append_conjunction_fields",
    "build_conjunction_context",
    "embed_context",
    "estimate_fuel_after_burn",
    "get_conjunction_event",
    "get_shared_memory_bank",
    "safe_document_id",
]
