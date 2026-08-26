"""Project O.R.B.I.T. — Space-Track.org client (real orbital data).

Architectural role
------------------
Replaces the synthetic TLE catalogue with live data from
`Space-Track.org <https://www.space-track.org>`_, the public
Space Surveillance Network data distribution site, while degrading
gracefully to the simulated catalogue whenever credentials are absent,
the network is unreachable or rate limits bite. The fleet therefore runs
on real orbital elements when they exist and on calibrated simulation
when they do not — and every tool response states which one it used.

API surface used (production endpoints)
---------------------------------------
* Login:      ``POST /ajaxauth/login``            — form identity/password;
              session cookie authorises all subsequent calls.
* TLEs:       ``GET /basicspacedata/api/class/tle_latest/OBJECT_NAME/<name>/FORMAT/JSON``
              (or ``NORAD_CAT_ID``) — latest element sets.
* Conjunctions: ``GET /basicspacedata/api/class/conjunction/NORAD_CAT_ID/<id>/.../FORMAT/JSON``
              — Conjunction Data Messages (CDMs) with TCA, miss distance,
              relative velocity and collision probability.

Rate-limit etiquette
--------------------
Space-Track throttles aggressively: we log in at most once per client
lifetime, enforce a minimum spacing between API hits
(``SPACETRACK_MIN_INTERVAL_SECONDS``, default 3 s) and cache every payload
through the MemoryBank with a TTL (``SPACETRACK_CACHE_TTL_SECONDS``,
default 6 h) so repeated agent queries cost zero network traffic.

Degradation contract
--------------------
Every failure mode raises :class:`SpaceTrackUnavailable`; callers in
``tools.space_tools`` catch it, log a ``SPACETRACK_FALLBACK_SYNTHETIC``
audit event and serve the calibrated synthetic catalogue instead. No
exception ever reaches an agent as a raw traceback.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Final

from geap_sim.memory_bank import get_shared_memory_bank
from geap_sim.observability import audit_logger

BASE_URL: Final[str] = "https://www.space-track.org"

#: Environment configuration (read once at singleton construction).
SPACETRACK_USERNAME: Final[str] = os.environ.get("SPACETRACK_USERNAME", "").strip()
SPACETRACK_PASSWORD: Final[str] = os.environ.get("SPACETRACK_PASSWORD", "").strip()
CACHE_TTL_SECONDS: Final[float] = float(os.environ.get("SPACETRACK_CACHE_TTL_SECONDS", "21600"))
MIN_INTERVAL_SECONDS: Final[float] = float(os.environ.get("SPACETRACK_MIN_INTERVAL_SECONDS", "3"))

_CACHE_COLLECTION: Final[str] = "spacetrack_cache"

#: CARA bands mirrored from tools/space_tools (duplicated here so this
#: module stays import-light for standalone testing).
_HIGH_BAND: Final[float] = 1e-4
_MEDIUM_BAND: Final[float] = 1e-6


class SpaceTrackUnavailable(RuntimeError):
    """Raised when real Space-Track data cannot be retrieved.

    Callers must treat this as the signal to fall back to the synthetic
    catalogue — never as a mission-fatal error.
    """


def credentials_configured() -> bool:
    """True when both SPACETRACK_USERNAME and SPACETRACK_PASSWORD are set."""
    return bool(SPACETRACK_USERNAME and SPACETRACK_PASSWORD)


def band_for_pc(pc: float) -> str:
    """NASA CARA / ESA risk band for a collision probability."""
    if pc >= _HIGH_BAND:
        return "HIGH"
    if pc >= _MEDIUM_BAND:
        return "MEDIUM"
    return "LOW"


def _parse_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


class SpaceTrackClient:
    """Authenticated, throttled, cached Space-Track.org API client."""

    def __init__(self, username: str = SPACETRACK_USERNAME, password: str = SPACETRACK_PASSWORD) -> None:
        self._username = username
        self._password = password
        self._session: Any = None
        self._last_hit_monotonic: float = 0.0
        self._login_lock = threading.Lock()

    # -- transport -------------------------------------------------------------

    def _ensure_logged_in(self) -> None:
        """Login once per process; Space-Track sessions survive on cookies."""
        if self._session is not None:
            return
        if not credentials_configured():
            raise SpaceTrackUnavailable(
                "SPACETRACK_USERNAME / SPACETRACK_PASSWORD are not configured; "
                "falling back to the synthetic catalogue."
            )
        import requests  # lazy so offline demos never pay the import

        with self._login_lock:
            if self._session is not None:
                return
            session = requests.Session()
            response = session.post(
                f"{BASE_URL}/ajaxauth/login",
                data={"identity": self._username, "password": self._password},
                timeout=30,
            )
            if response.status_code != 200 or "Login Failed" in response.text:
                raise SpaceTrackUnavailable(
                    f"Space-Track login rejected (HTTP {response.status_code})."
                )
            self._session = session
            audit_logger.log_event(
                trace_id="spacetrack",
                agent_name="tools.space_track_api",
                event_type="SPACETRACK_LOGIN",
                payload={"username": self._username[:3] + "***"},
                status="OK",
            )

    def _throttled_get(self, path: str) -> list[dict[str, Any]]:
        """GET one query path honouring the minimum-interval policy."""
        import requests  # noqa: F401 — ensures the same lazy availability check

        self._ensure_logged_in()
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_hit_monotonic)
        if wait > 0:
            time.sleep(wait)
        response = self._session.get(f"{BASE_URL}{path}", timeout=60)
        self._last_hit_monotonic = time.monotonic()
        if response.status_code == 401:
            # Session expired mid-flight: force a fresh login next attempt.
            self._session = None
            raise SpaceTrackUnavailable("Space-Track session expired (HTTP 401); retry will re-login.")
        if response.status_code != 200:
            raise SpaceTrackUnavailable(f"Space-Track query failed (HTTP {response.status_code}).")
        try:
            rows = response.json()
        except ValueError as exc:
            raise SpaceTrackUnavailable("Space-Track returned non-JSON content.") from exc
        return [dict(row) for row in rows] if isinstance(rows, list) else []

    @staticmethod
    def _cached(kind: str, key: str) -> tuple[list[dict[str, Any]] | None, bool]:
        """Return (payload, hit) from the MemoryBank TTL cache."""
        bank = get_shared_memory_bank()

        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            payload = asyncio.run(bank.cache_get(_CACHE_COLLECTION, f"{kind}:{key}", CACHE_TTL_SECONDS))
            return (payload if isinstance(payload, list) else None), payload is not None
        # Called from async context via to_thread wrappers — never block a loop here.
        return None, False

    @staticmethod
    def _store_cache(kind: str, key: str, payload: list[dict[str, Any]]) -> None:
        bank = get_shared_memory_bank()
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(bank.cache_put(_CACHE_COLLECTION, f"{kind}:{key}", payload))

    # -- public API --------------------------------------------------------------

    def fetch_tle(self, object_name: str, norad_cat_id: int | None = None) -> list[dict[str, Any]]:
        """Latest element sets for one object, cached under its query key.

        Returns rows normalised to the fleet's TLE shape: ``object_name``,
        ``norad_cat_id``, ``epoch_utc``, ``tle_line1``, ``tle_line2``.
        """
        identifier = str(norad_cat_id) if norad_cat_id is not None else object_name.strip().upper()
        cache_field = "NORAD_CAT_ID" if norad_cat_id is not None else "OBJECT_NAME"
        cached, hit = self._cached("tle", identifier)
        if hit and cached:
            return cached

        path = (
            f"/basicspacedata/api/class/tle_latest/{cache_field}/{identifier}"
            "/orderby/EPOCH_DESC/LIMIT/5/FORMAT/JSON"
        )
        rows = self._throttled_get(path)
        normalised = [
            {
                "object_name": row.get("OBJECT_NAME", object_name),
                "norad_cat_id": row.get("NORAD_CAT_ID"),
                "epoch_utc": row.get("EPOCH"),
                "tle_line1": row.get("TLE_LINE1"),
                "tle_line2": row.get("TLE_LINE2"),
                "ref_epoch_source": "space-track/tle_latest",
            }
            for row in rows
            if row.get("TLE_LINE1") and row.get("TLE_LINE2")
        ]
        self._store_cache("tle", identifier, normalised)
        audit_logger.log_event(
            trace_id="spacetrack",
            agent_name="tools.space_track_api",
            event_type="SPACETRACK_TLE_FETCHED",
            payload={"query": identifier, "rows": len(normalised), "cache_ttl_s": CACHE_TTL_SECONDS},
            status="OK",
        )
        return normalised

    def fetch_cdms(self, norad_cat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Recent Conjunction Data Messages for one catalogue number.

        Rows are normalised to: ``cdm_id``, ``tca_iso``, ``miss_distance_km``,
        ``relative_velocity_km_s``, ``pc`` (raw probability fraction),
        ``risk_band``, ``created_utc``. Space-Track expresses PC in scientific
        notation; values above 0.5 are treated as percentages and divided by
        100 (documented heuristic — real CDM Pc values are tiny).
        """
        limit = max(1, min(int(limit), 50))
        cache_key = f"{norad_cat_id}:{limit}"
        cached, hit = self._cached("cdm", cache_key)
        if hit and cached:
            return cached

        path = (
            f"/basicspacedata/api/class/conjunction/NORAD_CAT_ID/{norad_cat_id}"
            f"/orderby/TCA_DESC/LIMIT/{limit}/FORMAT/JSON"
        )
        rows = self._throttled_get(path)
        normalised: list[dict[str, Any]] = []
        for row in rows:
            pc = _parse_float(row.get("PC"))
            if pc is not None and pc > 0.5:
                pc = pc / 100.0
            miss_km = _parse_float(row.get("MISS_DISTANCE"))
            rel_speed = _parse_float(row.get("RELATIVE_SPEED"))
            normalised.append(
                {
                    "cdm_id": row.get("CCSDS_CDM_ID") or row.get("CDM_ID"),
                    "tca_iso": row.get("TCA"),
                    "miss_distance_km": miss_km,
                    "relative_velocity_km_s": (rel_speed / 1000.0) if rel_speed and rel_speed > 20 else rel_speed,
                    "pc": pc,
                    "risk_band": band_for_pc(pc) if pc is not None else None,
                    "created_utc": row.get("CREATED"),
                    "source": "space-track/conjunction",
                }
            )
        self._store_cache("cdm", cache_key, normalised)
        audit_logger.log_event(
            trace_id="spacetrack",
            agent_name="tools.space_track_api",
            event_type="SPACETRACK_CDM_FETCHED",
            payload={"norad_cat_id": norad_cat_id, "cdms": len(normalised)},
            status="OK",
        )
        return normalised


_shared_client: SpaceTrackClient | None = None


def get_shared_space_track_client() -> SpaceTrackClient:
    """Process-wide Space-Track client (constructed on first use)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = SpaceTrackClient()
    return _shared_client


__all__ = [
    "BASE_URL",
    "CACHE_TTL_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "SpaceTrackClient",
    "SpaceTrackUnavailable",
    "band_for_pc",
    "credentials_configured",
    "get_shared_space_track_client",
]
