"""GEAP simulation — persistent mission memory (Firestore-backed).

Simulates the GEAP **Memory Bank**: durable, cross-session context for the
fleet. Two Firestore collections model the domain:

* ``satellites/{sat_id}``        — live vehicle state: fuel percentage,
  thruster health, cumulative delta-v expended, last-updated stamp.
* ``conjunctions/{conjunction_id}`` — immutable screening history: risk band,
  Pc, miss distance, TCA and the action ultimately taken.

Local-dev resilience
--------------------
Production code paths hit real Firestore through ``AsyncFirestoreClient``;
if credentials are absent and no ``FIRESTORE_EMULATOR_HOST`` is configured —
the typical hackathon-laptop situation — construction fails and the bank
transparently degrades to an in-process dictionary backend. Callers cannot
tell the difference; every method signature and return shape is identical.
Backend selection is audited at startup so judges can see which mode ran.

The selected backend is controlled by ``ORBIT_MEMORY_BACKEND``
(``auto`` default | ``firestore`` | ``memory``).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Final

from geap_sim.observability import audit_logger

SATELLITES_COLLECTION: Final[str] = "satellites"
CONJUNCTIONS_COLLECTION: Final[str] = "conjunctions"

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


class MemoryBank:
    """Persistent satellite state + conjunction history (Firestore or memory).

    All methods are async so the call sites inside the agent pipeline remain
    identical regardless of which backend is active.
    """

    def __init__(self) -> None:
        self._backend: Final[str] = self._select_backend()
        self._memory_store: dict[tuple[str, str], dict[str, Any]] = {}
        self._client: Any = None
        if self._backend == "firestore":
            from google.cloud.firestore import AsyncFirestoreClient

            self._client = AsyncFirestoreClient()
        audit_logger.log_event(
            trace_id="startup",
            agent_name="geap_sim.memory_bank",
            event_type="MEMORY_BANK_BACKEND_SELECTED",
            payload={"backend": self._backend},
            status=self._backend.upper(),
        )

    # -- backend selection ---------------------------------------------------

    def _select_backend(self) -> str:
        mode = os.getenv("ORBIT_MEMORY_BACKEND", "auto").strip().lower()
        if mode == "memory":
            return "memory"
        try:
            from google.cloud.firestore import AsyncFirestoreClient  # noqa: F401
        except ImportError:
            if mode == "firestore":
                raise RuntimeError(
                    "ORBIT_MEMORY_BACKEND=firestore but google-cloud-firestore "
                    "is not installed. Install it or unset the override."
                )
            return "memory"
        if not (os.getenv("FIRESTORE_EMULATOR_HOST") or os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")):
            if mode == "firestore":
                return "firestore"  # explicit override: let auth errors surface loudly at call time
            # Auto mode without credentials/emulator/project → degrade quietly.
            return "memory"
        return "firestore"

    @property
    def backend_name(self) -> str:
        """Active persistence backend: ``firestore`` or ``memory``."""
        return self._backend

    # -- satellites ----------------------------------------------------------

    async def get_satellite_state(self, sat_id: str) -> dict[str, Any]:
        """Return the current state document for a satellite.

        Missing documents resolve to nominal defaults (100% fuel/health)
        rather than raising, so first-contact alerts always proceed.
        """
        key = safe_document_id(sat_id).upper()
        if self._backend == "firestore":
            snapshot = await self._client.collection(SATELLITES_COLLECTION).document(key).get()
            stored: dict[str, Any] = dict(snapshot.to_dict() or {})
        else:
            stored = dict(self._memory_store.get((SATELLITES_COLLECTION, key), {}))

        state = {**_DEFAULT_STATE_TEMPLATE, **stored}
        state["sat_id"] = key
        state["fuel_percentage"] = max(0.0, min(100.0, float(state["fuel_percentage"])))
        state["thruster_health"] = max(0.0, min(100.0, float(state["thruster_health"])))
        state["total_dv_expended"] = max(0.0, float(state["total_dv_expended"]))
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
        """Persist one screening/outcome record keyed by conjunction ID."""
        key = safe_document_id(conjunction_id)
        record: dict[str, Any] = {
            "recorded_utc": _utc_now_iso(),
            **event_data,
            "conjunction_id": key,
        }
        await self._write(CONJUNCTIONS_COLLECTION, key, record)
        return record

    async def get_historical_conjunctions(self, sat_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Most-recent-first conjunction history for one satellite."""
        key = safe_document_id(sat_id).upper()
        limit = max(1, int(limit))
        if self._backend == "firestore":
            query = (
                self._client.collection(CONJUNCTIONS_COLLECTION)
                .where("sat_id", "==", key)
                .order_by("recorded_utc", direction="DESCENDING")
                .limit(limit)
            )
            return [dict(doc.to_dict() or {}) async for doc in await query.get()]
        matches = [
            dict(doc)
            for (collection, doc_key), doc in sorted(self._memory_store.items())
            if collection == CONJUNCTIONS_COLLECTION and doc.get("sat_id") == key
        ]
        matches.sort(key=lambda doc: str(doc.get("recorded_utc", "")), reverse=True)
        return matches[:limit]

    # -- internals -------------------------------------------------------------

    async def _write(self, collection: str, doc_key: str, data: dict[str, Any]) -> None:
        if self._backend == "firestore":
            await self._client.collection(collection).document(doc_key).set(data, merge=True)
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
    "FUEL_PERCENT_PER_DV_MPS",
    "MemoryBank",
    "SATELLITES_COLLECTION",
    "estimate_fuel_after_burn",
    "get_shared_memory_bank",
    "safe_document_id",
]
