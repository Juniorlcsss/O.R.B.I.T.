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
* Login:   ``POST /ajaxauth/login`` — form ``identity``/``password``; the
  session cookie authorises every subsequent call.
* Elsets:  ``GET /basicspacedata/query/class/gp/...`` — the General
  Perturbations class, which is the *current* home of the newest SGP4
  element set for each catalogued object. JSON responses carry the
  traditional ``TLE_LINE1`` / ``TLE_LINE2`` fields alongside the OMM
  keywords.
* Conjunctions: ``GET /basicspacedata/query/class/cdm_public/...`` — the
  publicly released subset of Conjunction Data Messages.

Three details of the request grammar bite anyone porting older code, and
all three are load-bearing here:

* The request *action* is ``query``, not ``api``. ``/basicspacedata/api/``
  is not a valid controller path.
* ``tle``, ``tle_latest`` and ``tle_publish`` have all been **removed**.
  The GP class replaces them; ``/format/tle/`` reproduces the old payload.
* REST predicates are lowercase (``orderby``, ``limit``, ``format``) and a
  sort direction is separated from its column by a space — ``EPOCH desc``,
  URL-encoded ``EPOCH%20desc`` — not by an underscore.

Rate-limit etiquette
--------------------
Space-Track throttles aggressively and suspends accounts that do not
behave. Published ceilings are 30 requests/minute and 300 requests/hour,
with per-class guidance of roughly one GP query per hour and one CDM query
per hour for a specific event. This client therefore:

* logs in at most once per process (sessions persist on a cookie);
* enforces a minimum spacing between hits
  (``SPACETRACK_MIN_INTERVAL_SECONDS``, default 3 s);
* tracks a rolling request window and refuses to exceed either published
  ceiling, raising :class:`SpaceTrackUnavailable` rather than gambling the
  account; and
* caches every payload through the MemoryBank with a long TTL
  (``SPACETRACK_CACHE_TTL_SECONDS``, default 6 h) so repeated agent queries
  cost zero network traffic.

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
from collections import deque
from typing import Any, Final
import requests

from geap_sim.memory_bank import get_shared_memory_bank
from geap_sim.observability import audit_logger

BASE_URL: Final[str] = "https://www.space-track.org"

#: Environment configuration (read once at singleton construction).
SPACETRACK_USERNAME: Final[str] = os.environ.get("SPACETRACK_USERNAME", "").strip()
SPACETRACK_PASSWORD: Final[str] = os.environ.get("SPACETRACK_PASSWORD", "").strip()
CACHE_TTL_SECONDS: Final[float] = float(os.environ.get("SPACETRACK_CACHE_TTL_SECONDS", "21600"))
MIN_INTERVAL_SECONDS: Final[float] = float(os.environ.get("SPACETRACK_MIN_INTERVAL_SECONDS", "3"))

MAX_REQUESTS_PER_MINUTE: Final[int] = int(os.environ.get("SPACETRACK_MAX_PER_MINUTE", "20"))
MAX_REQUESTS_PER_HOUR: Final[int] = int(os.environ.get("SPACETRACK_MAX_PER_HOUR", "250"))

_CACHE_COLLECTION: Final[str] = "spacetrack_cache"

_CACHE_SCHEMA: Final[str] = "v4"

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


def _first(row: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among ``keys``.

    Space-Track serves conjunction data under two vocabularies: the
    simplified ``cdm_public`` column names (``PC``, ``MIN_RNG``) and the
    CCSDS 508.0-B-1 keywords used by the full CDM format
    (``COLLISION_PROBABILITY``, ``MISS_DISTANCE``, ``RELATIVE_SPEED``).
    Rather than hard-code one and break silently against the other, every
    field is read through this tolerant lookup. ``scripts/spacetrack_probe.py``
    prints the keys the live account actually returns.
    """
    for key in keys:
        if key in row:
            value = row[key]
            if value not in (None, ""):
                return value
    return None


def _describe_object(*, norad_id: Any, name: Any, object_type: str, rcs: Any) -> dict[str, Any]:
    """Describe one side of a conjunction, honestly about what is unknown.

    ``OBJECT_TYPE`` separates payloads from debris and spent rocket bodies.
    It does **not** say whether a payload is alive. The catalogue lists as
    ``PAYLOAD`` a great many objects that cannot manoeuvre at all: defunct
    spacecraft that lost power decades ago, and passive targets such as the
    STELLA and LAGEOS geodetic spheres, which are solid balls with no
    propulsion by design.

    So debris and rocket bodies are known-unmanoeuvrable, while a payload is
    only *possibly* manoeuvrable. Treating "payload" as "will negotiate"
    would overstate what this data can support, and the whole point of the
    coordination path is that it must not claim agreement it does not have.
    """
    kind = str(object_type or "UNKNOWN").upper()
    is_payload = kind == "PAYLOAD"
    return {
        "norad_id": norad_id,
        "name": name,
        "object_type": kind,
        "rcs": rcs,
        "possibly_manoeuvrable": is_payload,
        "manoeuvrability": "unknown" if is_payload else "none",
    }


class SpaceTrackClient:
    """Authenticated, throttled, cached Space-Track.org API client."""

    def __init__(self, username: str = SPACETRACK_USERNAME, password: str = SPACETRACK_PASSWORD) -> None:
        self._username = username
        self._password = password
        self._session: Any = None
        self._login_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._hits: deque[float] = deque()

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
                    f"Space-Track login rejected (HTTP {response.status_code}). "
                    "Check SPACETRACK_USERNAME / SPACETRACK_PASSWORD."
                )
            self._session = session
            audit_logger.log_event(
                trace_id="spacetrack",
                agent_name="tools.space_track_api",
                event_type="SPACETRACK_LOGIN",
                payload={"username": self._username[:3] + "***"},
                status="OK",
            )

    def _reserve_slot(self) -> None:
        """Block for the minimum interval; refuse to breach a published ceiling.

        Exceeding Space-Track's documented rate limits risks suspension of
        the account, so a would-be breach fails closed into the synthetic
        fallback instead of being sent.
        """
        with self._rate_lock:
            now = time.monotonic()
            while self._hits and now - self._hits[0] > 3600.0:
                self._hits.popleft()
            in_last_hour = len(self._hits)
            in_last_minute = sum(1 for hit in self._hits if now - hit <= 60.0)
            if in_last_hour >= MAX_REQUESTS_PER_HOUR:
                raise SpaceTrackUnavailable(
                    f"Space-Track hourly request budget exhausted "
                    f"({in_last_hour}/{MAX_REQUESTS_PER_HOUR}); serving cached or synthetic data."
                )
            if in_last_minute >= MAX_REQUESTS_PER_MINUTE:
                raise SpaceTrackUnavailable(
                    f"Space-Track per-minute request budget exhausted "
                    f"({in_last_minute}/{MAX_REQUESTS_PER_MINUTE}); serving cached or synthetic data."
                )
            wait = MIN_INTERVAL_SECONDS - (now - self._hits[-1]) if self._hits else 0.0
            if wait > 0:
                time.sleep(wait)
            self._hits.append(time.monotonic())

    def _throttled_get(self, path: str) -> list[dict[str, Any]]:
        """GET one query path honouring the rate-limit policy."""
        self._ensure_logged_in()
        self._reserve_slot()
        response = self._session.get(f"{BASE_URL}{path}", timeout=60)
        if response.status_code == 401:
            # Session expired mid-flight: force a fresh login next attempt.
            self._session = None
            raise SpaceTrackUnavailable("Space-Track session expired (HTTP 401); retry will re-login.")
        if response.status_code == 429:
            raise SpaceTrackUnavailable("Space-Track rate limit hit (HTTP 429); backing off to synthetic data.")
        if response.status_code != 200:
            raise SpaceTrackUnavailable(
                f"Space-Track query failed (HTTP {response.status_code}) for {path}"
            )
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
            payload = asyncio.run(
                bank.cache_get(_CACHE_COLLECTION, f"{_CACHE_SCHEMA}:{kind}:{key}", CACHE_TTL_SECONDS)
            )
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
            asyncio.run(bank.cache_put(_CACHE_COLLECTION, f"{_CACHE_SCHEMA}:{kind}:{key}", payload))

    # -- public API --------------------------------------------------------------

    def fetch_tle(self, object_name: str, norad_cat_id: int | None = None) -> list[dict[str, Any]]:
        """Latest element sets for one object, cached under its query key.

        Uses the GP class, which superseded the removed class. 

        Returns rows normalised to the fleet's TLE shape: ``object_name``,
        ``norad_cat_id``, ``epoch_utc``, ``tle_line1``, ``tle_line2``.
        """
        identifier = str(norad_cat_id) if norad_cat_id is not None else object_name.strip().upper()
        predicate = "NORAD_CAT_ID" if norad_cat_id is not None else "OBJECT_NAME"
        cached, hit = self._cached("tle", identifier)
        if hit and cached:
            return cached

        path = (
            f"/basicspacedata/query/class/gp/{predicate}/{identifier}"
            "/decay_date/null-val"
            "/orderby/EPOCH%20desc/limit/5/format/json"
        )
        rows = self._throttled_get(path)
        normalised = [
            {
                "object_name": row.get("OBJECT_NAME", object_name),
                "norad_cat_id": row.get("NORAD_CAT_ID"),
                "epoch_utc": row.get("EPOCH"),
                "tle_line1": row.get("TLE_LINE1"),
                "tle_line2": row.get("TLE_LINE2"),
                "ref_epoch_source": "space-track/gp",
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

    def fetch_tles(self, norad_ids: list[int]) -> dict[str, dict[str, Any]]:
        """Latest element sets for many objects in ONE request.

        Space-Track's guidelines are explicit that per-object queries in a
        loop are an abuse of the service; the GP class accepts a
        comma-delimited list of catalogue numbers instead. Keyed by
        catalogue number as a string.
        """
        wanted = sorted({int(value) for value in norad_ids})
        if not wanted:
            return {}
        cache_key = ",".join(str(value) for value in wanted)
        cached, hit = self._cached("tle_bulk", cache_key)
        if hit and cached:
            return {str(row.get("norad_cat_id")): row for row in cached}

        path = (
            f"/basicspacedata/query/class/gp/NORAD_CAT_ID/{cache_key}"
            "/decay_date/null-val"
            "/orderby/NORAD_CAT_ID/format/json"
        )
        rows = self._throttled_get(path)
        normalised = [
            {
                "object_name": row.get("OBJECT_NAME"),
                "norad_cat_id": row.get("NORAD_CAT_ID"),
                "object_type": row.get("OBJECT_TYPE"),
                "epoch_utc": row.get("EPOCH"),
                "tle_line1": row.get("TLE_LINE1"),
                "tle_line2": row.get("TLE_LINE2"),
                "ref_epoch_source": "space-track/gp",
            }
            for row in rows
            if row.get("TLE_LINE1") and row.get("TLE_LINE2")
        ]
        self._store_cache("tle_bulk", cache_key, normalised)
        audit_logger.log_event(
            trace_id="spacetrack",
            agent_name="tools.space_track_api",
            event_type="SPACETRACK_TLE_BULK_FETCHED",
            payload={"requested": len(wanted), "returned": len(normalised)},
            status="OK",
        )
        return {str(row["norad_cat_id"]): row for row in normalised}

    def fetch_cdms(self, norad_cat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Recent public Conjunction Data Messages for one catalogue number.

        A CDM names two objects, and ours may be either of them, so this
        queries ``SAT_1_ID`` and ``SAT_2_ID`` separately and merges — the
        REST grammar cannot express an OR across two different predicates.
        Both queries share one cache entry.

        Rows are normalised to: ``cdm_id``, ``tca_iso``, ``miss_distance_km``,
        ``relative_velocity_km_s``, ``pc`` (raw probability fraction),
        ``risk_band``, ``created_utc``, ``primary_norad_id`` and
        ``secondary_norad_id`` — the last two matter because coordination is
        only possible with the *other* object's operator.
        """
        limit = max(1, min(int(limit), 50))
        cache_key = f"{norad_cat_id}:{limit}"
        cached, hit = self._cached("cdm", cache_key)
        if hit and cached:
            return cached

        rows: list[dict[str, Any]] = []
        for predicate in ("SAT_1_ID", "SAT_2_ID"):
            path = (
                f"/basicspacedata/query/class/cdm_public/{predicate}/{norad_cat_id}"
                f"/orderby/TCA%20desc/limit/{limit}/format/json"
            )
            rows.extend(self._throttled_get(path))

        normalised: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            cdm_id = str(_first(row, "CDM_ID", "CCSDS_CDM_ID", "MESSAGE_ID") or "")
            if cdm_id and cdm_id in seen:
                continue
            if cdm_id:
                seen.add(cdm_id)

            pc = _parse_float(_first(row, "PC", "COLLISION_PROBABILITY"))
            if pc is not None and pc > 0.5:
                pc = pc / 100.0

            miss_m = _parse_float(_first(row, "MIN_RNG", "MISS_DISTANCE"))
            miss_km = (miss_m / 1000.0) if miss_m is not None else None

            rel_speed = _parse_float(_first(row, "RELATIVE_SPEED", "REL_SPEED"))
            rel_km_s = (rel_speed / 1000.0) if rel_speed is not None else None

            sat_1 = _first(row, "SAT_1_ID", "SAT1_ID", "OBJECT1_OBJECT_DESIGNATOR")
            sat_2 = _first(row, "SAT_2_ID", "SAT2_ID", "OBJECT2_OBJECT_DESIGNATOR")

            ours_is_first = str(sat_1) == str(norad_cat_id)
            secondary = sat_2 if ours_is_first else sat_1
            secondary_type = _first(row, "SAT2_OBJECT_TYPE" if ours_is_first else "SAT1_OBJECT_TYPE")

            normalised.append(
                {
                    "cdm_id": cdm_id or None,
                    "tca_iso": _first(row, "TCA"),
                    "miss_distance_km": miss_km,
                    "relative_velocity_km_s": rel_km_s,
                    "pc": pc,
                    "risk_band": band_for_pc(pc) if pc is not None else None,
                    "created_utc": _first(row, "CREATED", "CREATION_DATE"),
                    "primary_norad_id": norad_cat_id,
                    "secondary_norad_id": secondary,
                    "secondary_name": _first(row, "SAT_2_NAME" if ours_is_first else "SAT_1_NAME"),
                    "secondary_object_type": secondary_type,
                    "secondary_possibly_manoeuvrable": str(secondary_type).upper() == "PAYLOAD",
                    "emergency_reportable": str(_first(row, "EMERGENCY_REPORTABLE") or "").upper() == "Y",
                    "source": "space-track/cdm_public",
                }
            )

        normalised.sort(key=lambda item: str(item.get("tca_iso") or ""), reverse=True)
        normalised = normalised[:limit]

        self._store_cache("cdm", cache_key, normalised)
        audit_logger.log_event(
            trace_id="spacetrack",
            agent_name="tools.space_track_api",
            event_type="SPACETRACK_CDM_FETCHED",
            payload={"norad_cat_id": norad_cat_id, "cdms": len(normalised)},
            status="OK",
        )
        return normalised


    def fetch_recent_public_cdms(self, limit: int = 25) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        cache_key = f"recent:{limit}"
        cached, hit = self._cached("cdm_feed", cache_key)
        if hit and cached:
            return cached
        rows = self._throttled_get(
            "/basicspacedata/query/class/cdm_public/TCA/%3Enow"
            f"/orderby/TCA%20asc/limit/{limit}/format/json"
        )
        best_by_pair: dict[tuple[str, str], dict[str, Any]] = {}

        normalised: list[dict[str, Any]] = []
        for row in rows:
            pc = _parse_float(_first(row, "PC", "COLLISION_PROBABILITY"))
            if pc is not None and pc > 0.5:
                pc = pc / 100.0
            miss_m = _parse_float(_first(row, "MIN_RNG", "MISS_DISTANCE"))
            type_1 = str(_first(row, "SAT1_OBJECT_TYPE") or "UNKNOWN").upper()
            type_2 = str(_first(row, "SAT2_OBJECT_TYPE") or "UNKNOWN").upper()

            id_1 = str(_first(row, "SAT_1_ID") or "")
            id_2 = str(_first(row, "SAT_2_ID") or "")
            pair_key = (min(id_1, id_2), max(id_1, id_2))

            normalised.append(
                {
                    "cdm_id": _first(row, "CDM_ID"),
                    "tca_iso": _first(row, "TCA"),
                    "created_utc": _first(row, "CREATED"),
                    "miss_distance_km": (miss_m / 1000.0) if miss_m is not None else None,
                    "miss_distance_m": miss_m,
                    "pc": pc,
                    "risk_band": band_for_pc(pc) if pc is not None else None,
                    "emergency_reportable": str(_first(row, "EMERGENCY_REPORTABLE") or "").upper() == "Y",
                    "object_1": _describe_object(
                        norad_id=_first(row, "SAT_1_ID"),
                        name=_first(row, "SAT_1_NAME"),
                        object_type=type_1,
                        rcs=_first(row, "SAT1_RCS"),
                    ),
                    "object_2": _describe_object(
                        norad_id=_first(row, "SAT_2_ID"),
                        name=_first(row, "SAT_2_NAME"),
                        object_type=type_2,
                        rcs=_first(row, "SAT2_RCS"),
                    ),

                    "coordination_candidate": type_1 == "PAYLOAD" and type_2 == "PAYLOAD",
                    "source": "space-track/cdm_public",
                    "simulated": False,
                }
            )

            candidate = normalised.pop()
            incumbent = best_by_pair.get(pair_key)
            if incumbent is None or str(candidate.get("created_utc") or "") > str(
                incumbent.get("created_utc") or ""
            ):
                best_by_pair[pair_key] = candidate

        normalised = list(best_by_pair.values())
        normalised.sort(key=lambda item: (item.get("pc") or -1.0), reverse=True)
        self._store_cache("cdm_feed", cache_key, normalised)
        audit_logger.log_event(
            trace_id="spacetrack",
            agent_name="tools.space_track_api",
            event_type="SPACETRACK_PUBLIC_CDM_FEED",
            payload={
                "rows": len(normalised),
                "high_band": sum(1 for item in normalised if item.get("risk_band") == "HIGH"),
                "coordination_candidates": sum(1 for item in normalised if item.get("coordination_candidate")),
            },
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
    "MAX_REQUESTS_PER_HOUR",
    "MAX_REQUESTS_PER_MINUTE",
    "MIN_INTERVAL_SECONDS",
    "SpaceTrackClient",
    "SpaceTrackUnavailable",
    "band_for_pc",
    "credentials_configured",
    "get_shared_space_track_client",
]
