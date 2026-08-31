"""Project O.R.B.I.T. — space-domain tool belt.

Pure-Python, deterministic implementations of the orbital-mechanics and
external-fleet simulations consumed by the specialist agents.

Every public function is registered with the Google Agent Development Kit
(ADK) as a ``FunctionTool``. ADK derives each tool's JSON schema from the
type hints and Google-style docstrings below, so those docstrings are part
of the LLM contract — keep them accurate.

Design guarantees
-----------------
* **Deterministic** — identical inputs always produce identical outputs, so
  every decision in the audit trail can be replayed byte-for-byte.
* **Fail-soft** — invalid inputs return structured ``{"status": "error", ...}``
  payloads instead of raising, letting the calling agent recover gracefully.
* **Honest simulation** — every fabricated datum is flagged (``simulated``)
  and the computation method is echoed back for observability.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

import numpy as np
from google.adk.tools import FunctionTool
from sgp4.api import Satrec, SatrecArray, jday

from geap_sim.observability import audit_logger

try:  # gstime lives in different homes across sgp4 builds/versions
    from sgp4.api import gstime
except ImportError:  # pragma: no cover — pure-Python sgp4 installs
    from sgp4.propagation import gstime

# ---------------------------------------------------------------------------
# Physical constants & fleet policy limits
# ---------------------------------------------------------------------------

MU_EARTH_KM3_S2: Final[float] = 398_600.4418
EARTH_MEAN_RADIUS_KM: Final[float] = 6_378.1363
SECONDS_PER_DAY: Final[float] = 86_400.0
JULIAN_DATE_UNIX_EPOCH: Final[float] = 2_440_587.5

#: Combined hard-body radius for a CubeSat/debris encounter, km (~10 m).
COMBINED_HBR_KM: Final[float] = 0.010
#: Model Armour hard ceiling: no burn above this may ever be negotiated (m/s).
ABSOLUTE_DELTA_V_LIMIT_MPS: Final[float] = 50.0
#: Largest delta-v an external fleet may be asked to absorb (m/s).
MAX_NEGOTIABLE_DELTA_V_MPS: Final[float] = 25.0
#: Industry-standard conjunction alert thresholds (NASA CARA / ESA convention).
HIGH_RISK_THRESHOLD_P: Final[float] = 1.0e-4
MEDIUM_RISK_THRESHOLD_P: Final[float] = 1.0e-6

# Three-stage TCA search around the primary object's catalogue epoch, then a
# parabolic sub-sample refinement. Deterministic by construction.
_COARSE_HALF_WINDOW_S: Final[float] = 12.0 * 3_600.0
_COARSE_SAMPLES: Final[int] = 97
_MID_HALF_WINDOW_S: Final[float] = 2_700.0
_MID_SAMPLES: Final[int] = 81
_FINE_HALF_WINDOW_S: Final[float] = 120.0
_FINE_SAMPLES: Final[int] = 61

_TLE_WIDTH: Final[int] = 69
_SIGNING_KEY_ENV_VAR: Final[str] = "ORBIT_SIM_SIGNING_KEY"
_FALLBACK_SIM_SIGNING_KEY: Final[str] = "orbit-sim-demo-key-public-repo-safe"
_SIGNATURE_HEX_RE: Final[re.Pattern[str]] = re.compile(r"\b[a-f0-9]{64}\b")


# ---------------------------------------------------------------------------
# Simulated space-object catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    """Structured elements used to synthesise a valid, parseable TLE."""

    norad_id: int
    name: str
    classification: str
    intl_designator: str
    epoch_year: int
    epoch_day: float
    ndot: float
    nddot: float
    bstar: float
    ephemeris_type: int
    element_set: int
    inclination_deg: float
    raan_deg: float
    eccentricity: float
    argp_deg: float
    mean_anomaly_deg: float
    mean_motion_rev_day: float
    kind: Literal["payload", "debris"]
    operator: str


def _tle_checksum(body: str) -> str:
    """Standard TLE checksum: digit sum (minus signs count as 1), mod 10."""
    total = sum(int(ch) if ch.isdigit() else 1 for ch in body if ch == "-" or ch.isdigit())
    return str(total % 10)


def _place(width: int, placements: tuple[tuple[int, str], ...]) -> str:
    """Write text at fixed 1-indexed columns into an otherwise-blank field."""
    buf = [" "] * width
    for start, text in placements:
        for offset, ch in enumerate(text):
            buf[start - 1 + offset] = ch
    return "".join(buf)


def _sci_field(value: float) -> str:
    """Format an SGP4 exponential field (ndot/bstar) into 8 characters."""
    if value == 0.0:
        return " 00000+0"
    sign = "-" if value < 0 else " "
    magnitude = abs(value)
    exponent = int(math.floor(math.log10(magnitude)))
    mantissa = int(round(magnitude / (10.0**exponent) * 1e5))
    if mantissa >= 100_000:
        mantissa //= 10
        exponent += 1
    return f"{sign}{mantissa:05d}{exponent:+d}"


def _ndot_field(value: float) -> str:
    """Format the first time-derivative of mean motion (cols 34-43, 10 chars).

    Uses the catalogue convention of an implicit leading zero (" .00016717")
    so the decimal point lands on the exact column sgp4 validates against.
    """
    mantissa_text = f"{abs(value):.8f}"
    sign = "-" if value < 0 else " "
    return f"{sign}.{mantissa_text[2:]}"


def _epoch_field(year2: int, day: float) -> str:
    """Format the YYDDD.DDDDDDDD epoch into exactly 14 characters."""
    day_i = int(day)
    frac = int(round((day - day_i) * 1e8))
    if frac >= 100_000_000:
        day_i += 1
        frac -= 100_000_000
    return f"{year2:02d}{day_i:03d}.{frac:08d}"


def _build_line1(e: _CatalogEntry) -> str:
    # Column map follows the canonical NORAD template enforced by
    # ``sgp4.io.twoline2rv``: epoch occupies cols 19-32, ndot cols 34-44;
    # cols 9, 18, 33, 44, 53, 62 and 64 are mandatory blanks/separators.
    body = _place(
        _TLE_WIDTH - 1,
        (
            (1, "1"),
            (3, f"{e.norad_id:05d}"),
            (8, e.classification),
            (10, e.intl_designator.ljust(8)),
            (19, _epoch_field(e.epoch_year, e.epoch_day)),
            (34, _ndot_field(e.ndot)),
            (45, _sci_field(e.nddot)),
            (54, _sci_field(e.bstar)),
            (63, str(e.ephemeris_type)),
            (65, f"{e.element_set:4d}"),
        ),
    )
    return body + _tle_checksum(body)


def _build_line2(e: _CatalogEntry) -> str:
    body = _place(
        _TLE_WIDTH - 1,
        (
            (1, "2"),
            (3, f"{e.norad_id:05d}"),
            (9, f"{e.inclination_deg:8.4f}"),
            (18, f"{e.raan_deg:8.4f}"),
            (27, f"{int(round(e.eccentricity * 1e7)):07d}"),
            (35, f"{e.argp_deg:8.4f}"),
            (44, f"{e.mean_anomaly_deg:8.4f}"),
            (53, f"{e.mean_motion_rev_day:11.8f}"),
            (64, f"{e.element_set % 100_000:05d}"),
        ),
    )
    return body + _tle_checksum(body)

SIMULATED_COUNTERPARTIES: Final[frozenset[str]] = frozenset({"SIM_COORDINATION_TARGET"})
PROTECTED_SAT_ID: Final[str] = os.environ.get("ORBIT_PROTECTED_SAT_ID", "").strip().upper()

_CATALOG: Final[dict[str, _CatalogEntry]] = {
    "SIM_PROTECTED_ASSET": _CatalogEntry(
        norad_id=99001,
        name="SIMULATED PROTECTED ASSET (test fixture)",
        classification="U",
        intl_designator="26001AX",
        epoch_year=26,
        epoch_day=232.51234567,
        ndot=0.00002310,
        nddot=0.0,
        bstar=0.00011200,
        ephemeris_type=0,
        element_set=118,
        inclination_deg=97.5521,
        raan_deg=141.2033,
        eccentricity=0.0011450,
        argp_deg=88.4120,
        mean_anomaly_deg=212.7780,
        mean_motion_rev_day=15.06123456,
        kind="payload",
        operator="SIMULATED OPERATOR (test fixture)",
    ),
    "ISS_ZARYA": _CatalogEntry(
        norad_id=25544,
        name="ISS (ZARYA)",
        classification="U",
        intl_designator="98067A",
        epoch_year=26,
        epoch_day=228.16420000,
        ndot=0.00016710,
        nddot=0.0,
        bstar=0.00030777,
        ephemeris_type=0,
        element_set=999,
        inclination_deg=51.6410,
        raan_deg=209.1010,
        eccentricity=0.0006700,
        argp_deg=72.1010,
        mean_anomaly_deg=301.8000,
        mean_motion_rev_day=15.49889550,
        kind="payload",
        operator="Roscosmos/NASA consortium",
    ),
    "CSS_TIANHE": _CatalogEntry(
        norad_id=48274,
        name="CSS (TIANHE)",
        classification="C",
        intl_designator="21035AG",
        epoch_year=26,
        epoch_day=229.04129876,
        ndot=0.00021300,
        nddot=0.0,
        bstar=0.00042000,
        ephemeris_type=0,
        element_set=742,
        inclination_deg=41.4680,
        raan_deg=122.6100,
        eccentricity=0.0004200,
        argp_deg=155.3200,
        mean_anomaly_deg=204.8800,
        mean_motion_rev_day=15.61000000,
        kind="payload",
        operator="CMSA",
    ),
    "HUBBLE": _CatalogEntry(
        norad_id=20580,
        name="HST (HUBBLE)",
        classification="U",
        intl_designator="90037B",
        epoch_year=26,
        epoch_day=227.88471111,
        ndot=0.00001050,
        nddot=0.0,
        bstar=0.00002340,
        ephemeris_type=0,
        element_set=521,
        inclination_deg=28.4696,
        raan_deg=84.3102,
        eccentricity=0.0002450,
        argp_deg=118.7700,
        mean_anomaly_deg=241.3300,
        mean_motion_rev_day=15.09123456,
        kind="payload",
        operator="NASA/ESA",
    ),
    "STARLINK_3042": _CatalogEntry(
        norad_id=53042,
        name="STARLINK-3042",
        classification="U",
        intl_designator="22016BE",
        epoch_year=26,
        epoch_day=231.20765432,
        ndot=0.00012400,
        nddot=0.0,
        bstar=0.00015200,
        ephemeris_type=0,
        element_set=604,
        inclination_deg=53.0540,
        raan_deg=217.4010,
        eccentricity=0.0001500,
        argp_deg=71.2200,
        mean_anomaly_deg=289.4400,
        mean_motion_rev_day=15.06400000,
        kind="payload",
        operator="SpaceX",
    ),
    # NOTE: elements below are empirically calibrated against
    # SIM_PROTECTED_ASSET so this scripted scenario screens as a genuine HIGH
    # conjunction (~89 m miss at TCA, Pc ~7.5e-4) under real SGP4
    # propagation. It models a near-coincident post-fragmentation debris
    # cloud member slowly converging with the protected asset — the classic Kessler
    # cascade geometry. All values are simulated; see README.
    "FENGYUN_1C_DEB": _CatalogEntry(
        norad_id=34331,
        name="FENGYUN 1C DEB",
        classification="D",
        intl_designator="99025VCB",
        epoch_year=26,
        epoch_day=232.51234567,
        ndot=0.00004500,
        nddot=0.0,
        bstar=0.00018900,
        ephemeris_type=0,
        element_set=287,
        inclination_deg=97.5521,
        raan_deg=141.2233,
        eccentricity=0.0011450,
        argp_deg=88.4120,
        mean_anomaly_deg=212.8270,
        mean_motion_rev_day=15.06153456,
        kind="debris",
        operator="CNSA (debris)",
    ),
    # ------------------------------------------------------------------
    # Simulated coordination counterparty.
    "SIM_COORDINATION_TARGET": _CatalogEntry(
        norad_id=90001,
        name="SIMULATED PARTNER SAT (coordination exercise)",
        classification="U",
        intl_designator="26900AA",
        epoch_year=26,
        epoch_day=232.51234567,
        ndot=0.00004500,
        nddot=0.0,
        bstar=0.00018900,
        ephemeris_type=0,
        element_set=287,
        inclination_deg=97.5521,
        raan_deg=141.2233,
        eccentricity=0.0011450,
        argp_deg=88.4120,
        mean_anomaly_deg=212.8270,
        mean_motion_rev_day=15.06153456,
        kind="payload",
        operator="SIMULATED PARTNER OPERATOR (not a real organisation)",
    ),
    "COSMOS_2251_DEB": _CatalogEntry(
        norad_id=33759,
        name="COSMOS 2251 DEB",
        classification="D",
        intl_designator="93036SVV",
        epoch_year=26,
        epoch_day=230.91234567,
        ndot=0.00003200,
        nddot=0.0,
        bstar=0.00013400,
        ephemeris_type=0,
        element_set=398,
        inclination_deg=74.0190,
        raan_deg=260.5110,
        eccentricity=0.0091200,
        argp_deg=104.6700,
        mean_anomaly_deg=12.3400,
        mean_motion_rev_day=14.30876543,
        kind="debris",
        operator="ROSCOSMOS (debris)",
    ),
}

_LINE1_TEMPLATE: Final[tuple[tuple[int, str], ...]] = ((8, " "), (17, " "), (23, "."), (32, " "), (34, "."), (43, " "), (52, " "), (61, " "), (63, " "))
for _entry in _CATALOG.values():
    _l1, _l2 = _build_line1(_entry), _build_line2(_entry)
    if len(_l1) != _TLE_WIDTH or len(_l2) != _TLE_WIDTH:
        raise RuntimeError(f"TLE formatter produced invalid width for {_entry.name}")
    for _idx, _ch in _LINE1_TEMPLATE:
        if _l1[_idx] != _ch:
            raise RuntimeError(f"TLE line1 column {_idx + 1} malformed for {_entry.name}")
    try:
        Satrec.twoline2rv(_l1, _l2)
    except Exception as exc:  # pragma: no cover — fail-fast integrity gate
        raise RuntimeError(f"TLE for {_entry.name} failed SGP4 parsing: {exc}") from exc
del _entry, _l1, _l2, _idx, _ch


# ---------------------------------------------------------------------------
# Simulated external fleet directory (counterparties for the DiplomatAgent)
# ---------------------------------------------------------------------------
_FLEET_DIRECTORY: Final[dict[str, dict[str, Any]]] = {
    "STARLINK": {
        "fuel_budget_delta_v_mps": 18.5,
        "autonomy_mode": "AUTONOMOUS_DODGE_ENABLED",
        "contact_endpoint": "https://sim.spacex.example/api/fleet/starlink/maneuvers",
    },
    "ONEWEB": {
        "fuel_budget_delta_v_mps": 9.75,
        "autonomy_mode": "GROUND_SEGMENT_CONFIRMATION",
        "contact_endpoint": "https://sim.oneweb.example/api/fleet/oneweb/maneuvers",
    },
    "GUOWANG": {
        "fuel_budget_delta_v_mps": 12.40,
        "autonomy_mode": "GROUND_SEGMENT_CONFIRMATION",
        "contact_endpoint": "https://sim.guowang.example/api/fleet/guowang/maneuvers",
    },
    "ESA": {
        "fuel_budget_delta_v_mps": 11.25,
        "autonomy_mode": "GROUND_SEGMENT_CONFIRMATION",
        "contact_endpoint": "https://sim.esa.example/api/fleet/esa/maneuvers",
    },
}


# ---------------------------------------------------------------------------
# Internal helpers — time, propagation, geometry
# ---------------------------------------------------------------------------

def _datetime_from_julian(jd: float) -> datetime:
    return datetime.fromtimestamp((jd - JULIAN_DATE_UNIX_EPOCH) * SECONDS_PER_DAY, tz=timezone.utc)


def _julian_parts(moment: datetime) -> tuple[float, float]:
    second = moment.second + moment.microsecond / 1e6
    return jday(moment.year, moment.month, moment.day, moment.hour, moment.minute, second)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _grid(center: datetime, half_window_s: float, samples: int) -> list[datetime]:
    step = (2.0 * half_window_s) / (samples - 1)
    return [center + timedelta(seconds=i * step - half_window_s) for i in range(samples)]


def _scan_pair(pair: SatrecArray, moments: list[datetime]) -> np.ndarray:
    """Propagate both objects over ``moments``; return per-sample separation."""
    jd = np.empty(len(moments), dtype=float)
    fr = np.empty(len(moments), dtype=float)
    for i, moment in enumerate(moments):
        jd[i], fr[i] = _julian_parts(moment)
    errors, positions, _velocities = pair.sgp4(jd, fr)
    usable = (errors[0] == 0) & (errors[1] == 0)
    gaps = np.full(len(moments), np.inf)
    if usable.any():
        gaps[usable] = np.linalg.norm(positions[0][usable] - positions[1][usable], axis=1)
    return gaps


def _quadratic_minimum(d_minus: float, d_center: float, d_plus: float, step_s: float) -> tuple[float, float] | None:
    """Parabolic vertex fit through three samples → sub-grid TCA refinement."""
    denom = d_minus - 2.0 * d_center + d_plus
    if abs(denom) < 1e-12:
        return None
    first_deriv = (d_plus - d_minus) / (2.0 * step_s)
    curvature = denom / (2.0 * step_s**2)
    offset = -first_deriv / (2.0 * curvature)
    if not (-step_s <= offset <= step_s):
        return None
    minimum = d_center - first_deriv**2 / (4.0 * curvature)
    return offset, minimum


def _find_time_of_closest_approach(primary: Satrec, secondary: Satrec) -> tuple[datetime, float, np.ndarray, np.ndarray]:
    """Coarse→mid→fine grid search plus parabolic refinement of the TCA.

    Returns ``(tca_utc, miss_distance_km, relative_position_km, relative_velocity_km_s)``
    evaluated at the refined time of closest approach.
    """
    pair = SatrecArray([primary, secondary])
    epoch = _datetime_from_julian(primary.jdsatepoch + primary.jdsatepochF)

    coarse_gaps = _scan_pair(pair, _grid(epoch, _COARSE_HALF_WINDOW_S, _COARSE_SAMPLES))
    if not np.isfinite(coarse_gaps).any():
        raise ValueError("SGP4 propagation failed across the entire screening window.")
    coarse_best = _grid(epoch, _COARSE_HALF_WINDOW_S, _COARSE_SAMPLES)[int(np.argmin(coarse_gaps))]

    mid_times = _grid(coarse_best, _MID_HALF_WINDOW_S, _MID_SAMPLES)
    mid_gaps = _scan_pair(pair, mid_times)
    mid_best = mid_times[int(np.argmin(mid_gaps))]

    fine_times = _grid(mid_best, _FINE_HALF_WINDOW_S, _FINE_SAMPLES)
    fine_gaps = _scan_pair(pair, fine_times)
    fine_idx = int(np.argmin(fine_gaps))

    tca = fine_times[fine_idx]
    miss = float(fine_gaps[fine_idx])
    step = (2.0 * _FINE_HALF_WINDOW_S) / (_FINE_SAMPLES - 1)
    if 0 < fine_idx < len(fine_gaps) - 1:
        refined = _quadratic_minimum(float(fine_gaps[fine_idx - 1]), miss, float(fine_gaps[fine_idx + 1]), step)
        if refined is not None:
            offset, minimum = refined
            tca += timedelta(seconds=offset)
            miss = minimum
    miss = max(miss, 0.0)

    jd, fr = _julian_parts(tca)
    err1, r1, v1 = primary.sgp4(jd, fr)
    err2, r2, v2 = secondary.sgp4(jd, fr)
    if err1 or err2:  # pragma: no cover — guarded by window screening above
        raise ValueError(f"SGP4 failed at the refined TCA (error codes {err1}/{err2}).")
    return tca, miss, np.asarray(r1) - np.asarray(r2), np.asarray(v1) - np.asarray(v2)


def _pair_covariance_sigma_km(object_a: str, object_b: str) -> float:
    """Deterministic simulated covariance σ ∈ [0.05, 0.50] km for a pair.

    Modern commercial space-surveillance tracking routinely resolves LEO
    covariances to well under a kilometre, so this range keeps Chan's
    criterion physically sensible while spanning every alert band. The value
    derives stably from the sorted pair IDs so audit-log replays are
    reproducible byte-for-byte.
    """
    digest = hashlib.sha256("|".join(sorted((object_a.upper(), object_b.upper()))).encode("utf-8")).digest()
    draw = int.from_bytes(digest[:8], "big")
    return 0.05 + (draw % 46) / 100.0


def _error(error_code: str, message: str, **context: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error_code": error_code, "message": message}
    payload.update(context)
    return payload


# ---------------------------------------------------------------------------
# Live ScreeningPolicy thresholds (Phase 10 self-evolution linchpin)
# ---------------------------------------------------------------------------

_POLICY_CACHE_TTL_SECONDS: Final[float] = 5.0
_policy_cache: Final[dict[str, Any]] = {"loaded_monotonic": 0.0, "high": HIGH_RISK_THRESHOLD_P, "medium": MEDIUM_RISK_THRESHOLD_P}


def _risk_thresholds() -> tuple[float, float]:
    """(high, medium) Pc thresholds from the live ScreeningPolicy.

    Synchronous by contract (screening is a sync tool); bridges into the
    async PolicyStore when no loop is running and serves a short-TTL cache
    otherwise. Any failure keeps the CARA defaults — screening can never be
    broken by the memory layer, but an applied evolution cycle always
    changes the very next classification.
    """
    import asyncio
    import time as _time

    now = _time.monotonic()
    cached = _policy_cache["loaded_monotonic"]
    if now - cached < _POLICY_CACHE_TTL_SECONDS:
        return _policy_cache["high"], _policy_cache["medium"]

    try:
        from evolution.policy import get_shared_policy_store

        store = get_shared_policy_store()

        async def _load() -> None:
            policy = await store.load()
            _policy_cache["high"] = float(policy.pc_high_threshold)
            _policy_cache["medium"] = float(policy.pc_medium_threshold)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_load())
        else:
            # Inside a running loop (ADK async tool invocation): refresh in
            # a worker thread so we never block or nest loops.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, _load()).result(timeout=10)
        _policy_cache["loaded_monotonic"] = now
    except Exception:
        pass

    return _policy_cache["high"], _policy_cache["medium"]


def invalidate_policy_cache() -> None:
    """Drop the threshold cache so the next screening re-reads the policy."""
    _policy_cache["loaded_monotonic"] = 0.0


def _unknown_object_error(object_id: str) -> dict[str, Any]:
    """Unknown-id error listing what the agent could have asked for instead.

    Both id spaces are offered: the live objects currently on the board and
    the deterministic fixtures. Listing only the fixtures
    """
    return _error(
        "UNKNOWN_OBJECT_ID",
        f"'{object_id}' is not a live tracked object or a known fixture.",
        available_ids=sorted(set(_LAST_LIVE_OBJECTS) | set(_CATALOG)),
    )


# ---------------------------------------------------------------------------
# ADK Tool 1/3 — catalogue lookup (AstrodynamicsAgent)
# ---------------------------------------------------------------------------


def get_tle_data(satellite_id: str) -> dict[str, Any]:
    """Fetch Two-Line Element (TLE) orbital parameters for a tracked object.

    Resolves live objects from Space-Track's GP class first, falling back to
    the deterministic test fixtures. Use this before any conjunction analysis
    to obtain fresh orbital elements. Identifiers are case-insensitive;
    unknown ones return an error listing every valid id so you can
    self-correct. The response states its provenance via ``source`` and
    ``simulated``.

    Args:
        satellite_id: Catalogue identifier of the object, e.g.
            a live catalogue id as shown in the command picture (for example
            "COSMOS_864_9509"), or a deterministic test fixture id. Call
            get_live_conjunctions to discover the ids currently in view.

    Returns:
        A dict with keys: ``status`` ("ok"), ``satellite_id``, ``name``,
        ``norad_id``, ``object_kind``, ``operator``, ``tle_line1``, ``tle_line2``,
        ``epoch_utc``, ``inclination_deg``, ``raan_deg``, ``eccentricity``,
        ``argument_of_perigee_deg``, ``mean_anomaly_deg``,
        ``mean_motion_rev_per_day``, ``mean_altitude_km``, plus
        ``simulated`` (always true). On failure: ``status`` ("error"),
        ``error_code``, ``message``, and possibly ``available_ids``.
    """
    key = resolve_object_key(satellite_id)
    if not _known_object(key):
        return _unknown_object_error(satellite_id)
    entry = _CATALOG.get(key)
    live = _LAST_LIVE_OBJECTS.get(key)

    satrec = _satrec_for(key)
    if satrec is None:
        return _error("PROPAGATION_FAILURE", f"No usable element set for '{key}'.")

    mean_motion_rad_s = satrec.no_kozai / 60.0
    semi_major_axis_km = (MU_EARTH_KM3_S2 / mean_motion_rad_s**2) ** (1.0 / 3.0)

    if entry is None:
        return {
            "status": "ok",
            "satellite_id": key,
            "name": live.get("name", key),
            "norad_id": live.get("norad_id"),
            "object_kind": "payload" if live.get("type") == "satellite" else "debris",
            "operator": live.get("operator") or "UNKNOWN OPERATOR",
            "tle_line1": live.get("tle_line1"),
            "tle_line2": live.get("tle_line2"),
            "epoch_utc": live.get("epoch_utc"),
            "inclination_deg": round(float(satrec.inclo) * 180.0 / math.pi, 4),
            "raan_deg": round(float(satrec.nodeo) * 180.0 / math.pi, 4),
            "eccentricity": round(float(satrec.ecco), 7),
            "argument_of_perigee_deg": round(float(satrec.argpo) * 180.0 / math.pi, 4),
            "mean_anomaly_deg": round(float(satrec.mo) * 180.0 / math.pi, 4),
            "mean_motion_rev_per_day": round(float(satrec.no_kozai) * 1440.0 / (2.0 * math.pi), 8),
            "mean_altitude_km": round(semi_major_axis_km - EARTH_MEAN_RADIUS_KM, 2),
            "source": "space-track/gp",
            "simulated": False,
        }

    return {
        "status": "ok",
        "satellite_id": key,
        "name": entry.name,
        "norad_id": entry.norad_id,
        "object_kind": entry.kind,
        "operator": entry.operator,
        "tle_line1": _build_line1(entry),
        "tle_line2": _build_line2(entry),
        "epoch_utc": _iso_z(_datetime_from_julian(satrec.jdsatepoch + satrec.jdsatepochF)),
        "inclination_deg": round(float(satrec.inclo) * 180.0 / math.pi, 4),
        "raan_deg": round(float(satrec.nodeo) * 180.0 / math.pi, 4),
        "eccentricity": round(float(satrec.ecco), 7),
        "argument_of_perigee_deg": round(float(satrec.argpo) * 180.0 / math.pi, 4),
        "mean_anomaly_deg": round(float(satrec.mo) * 180.0 / math.pi, 4),
        "mean_motion_rev_per_day": round(entry.mean_motion_rev_day, 8),
        "mean_altitude_km": round(semi_major_axis_km - EARTH_MEAN_RADIUS_KM, 2),
        "source": "simulated_catalogue/v1",
        "simulated": True,
    }


# ---------------------------------------------------------------------------
# ADK Tool 2/3 — conjunction screening (AstrodynamicsAgent)
# ---------------------------------------------------------------------------


def screen_conjunction(sat_id: str, debris_id: str) -> dict[str, Any]:
    """Screen two catalogued objects for a close approach and estimate the probability of collision.

    Propagates both objects with the SGP4 model across a ±12-hour window
    around the primary's catalogue epoch using a three-stage refinement plus
    parabolic interpolation to locate the time of closest approach (TCA),
    then applies Chan's first-order Gaussian collision criterion:

        Pc ≈ (R² / 2σ²) · exp(−d² / 2σ²)

    where R is the combined hard-body radius, d the miss distance at TCA and
    σ a deterministic per-pair simulated covariance (real CDMs carry one).
    Risk bands follow the NASA CARA / ESA convention: HIGH ≥ 1e-4,
    MEDIUM ≥ 1e-6, otherwise LOW.

    Args:
        sat_id: Identifier of the protected asset, as shown in the command
            picture (for example "COSMOS_864_9509").
        debris_id: Identifier of the secondary object, likewise.

    Returns:
        A dict with keys: ``status`` ("ok"), ``sat_id``, ``debris_id``,
        ``tca_utc``, ``miss_distance_km``, ``relative_velocity_km_s``,
        ``combined_hbr_km``, ``sigma_km``, ``probability_of_collision``,
        ``risk_level`` ("HIGH"|"MEDIUM"|"LOW"), ``recommended_action``,
        ``method``, ``screening_window_hours``, ``policy_thresholds`` and
        ``simulated`` (always true). On failure: ``status`` ("error"),
        ``error_code``, ``message``.
    """
    sat_key, debris_key = resolve_object_key(sat_id), resolve_object_key(debris_id)
    if not _known_object(sat_key):
        return _unknown_object_error(sat_id)
    if not _known_object(debris_key):
        return _unknown_object_error(debris_id)
    if sat_key == debris_key:
        return _error("IDENTICAL_OBJECTS", "Conjunction screening requires two distinct objects.")

    published = _LAST_LIVE_ENCOUNTERS.get((min(sat_key, debris_key), max(sat_key, debris_key)))
    if published is not None:
        return _screening_from_cdm(sat_key, debris_key, published)

    try:
        primary = _satrec_for(sat_key)
        secondary = _satrec_for(debris_key)
        if primary is None or secondary is None:
            return _error(
                "PROPAGATION_FAILURE",
                "No usable element set for one of the objects; it may have decayed.",
            )
        tca, miss_km, rel_position, rel_velocity = _find_time_of_closest_approach(primary, secondary)
    except ValueError as exc:
        return _error("PROPAGATION_FAILURE", str(exc))

    sigma_km = _pair_covariance_sigma_km(sat_key, debris_key)
    scale_factor = COMBINED_HBR_KM**2 / (2.0 * sigma_km**2)
    probability = scale_factor * math.exp(-(miss_km**2) / (2.0 * sigma_km**2))
    probability = min(1.0, max(probability, 1.0e-15))

    # ---- Risk classification reads the LIVE ScreeningPolicy -------------------
    # After an applied evolution cycle the next screening uses the evolved
    # thresholds; any load failure falls back to the CARA defaults.
    high_threshold, medium_threshold = _risk_thresholds()

    if probability >= high_threshold:
        risk_level, recommended_action = (
            "HIGH",
            "IMMEDIATE ACTION REQUIRED: escalate to FleetCommander for dodge coordination.",
        )
    elif probability >= medium_threshold:
        risk_level, recommended_action = (
            "MEDIUM",
            "MONITOR: reassess on next ground pass and prepare a contingency burn plan.",
        )
    else:
        risk_level, recommended_action = "LOW", "NO ACTION: record in the mission log and continue nominal operations."

    return {
        "status": "ok",
        "sat_id": sat_key,
        "debris_id": debris_key,
        "tca_utc": _iso_z(tca),
        "miss_distance_km": round(miss_km, 4),
        "relative_velocity_km_s": round(float(np.linalg.norm(rel_velocity)), 4),
        "relative_position_km": [round(float(x), 4) for x in rel_position],
        "combined_hbr_km": COMBINED_HBR_KM,
        "sigma_km": round(sigma_km, 3),
        "probability_of_collision": float(f"{probability:.6e}"),
        "risk_level": risk_level,
        "recommended_action": recommended_action,
        "method": "sgp4_propagation+chan_first_order_gaussian",
        "screening_window_hours": 24,
        "policy_thresholds": {"high": high_threshold, "medium": medium_threshold},

        "element_source": (
            "space-track/gp"
            if sat_key not in _CATALOG and debris_key not in _CATALOG
            else "simulated_catalogue/v1"
            if sat_key in _CATALOG and debris_key in _CATALOG
            else "mixed"
        ),
        "simulated": sat_key in _CATALOG or debris_key in _CATALOG,
    }


# ---------------------------------------------------------------------------
# ADK Tool 3/3 — external fleet negotiation (DiplomatAgent only)
# ---------------------------------------------------------------------------


def negotiate_dodge_maneuver(target_fleet: str, required_delta_v: float) -> dict[str, Any]:
    """Ask a neighbouring satellite network to perform a collision-avoidance burn on our behalf.

    Simulates a request against an external constellation operator's API.
    Each counterparty has a fixed remaining fuel budget and its own autonomy
    policy; requests are honoured deterministically so negotiations can be
    replayed during incident review. The acknowledgement carries an
    HMAC-SHA256 signature over the agreed terms (simulated trust anchor).

    Policy limits enforced here (defence-in-depth beneath the SafetyOfficer):
    requests above 50 m/s are blocked outright as a policy violation;
    counterparty fleets refuse any ask above 25 m/s regardless of budget.

    Args:
        target_fleet: Constellation to negotiate with. One of "STARLINK",
            "ONEWEB", "GUOWANG" (case-insensitive).
        required_delta_v: Requested manoeuvre size in metres per second.
            Must be positive and within policy limits.

    Returns:
        A dict with keys: ``status`` ("ok"), ``target_fleet``,
        ``accepted`` (bool), and when accepted: ``manoeuvre_reference``,
        ``signature_algorithm``, ``acknowledgement_signature``,
        ``burn_window_start_utc``, ``burn_window_end_utc``,
        ``fleet_autonomy_mode``, ``contact_endpoint``,
        ``estimated_round_trip_ms``; when rejected additionally ``reason``
        and relevant limit echoes. All responses include ``simulated``
        (always true) and ``policy_version``. Hard failures use
        ``status`` ("error") with ``error_code`` and ``message``.
    """
    fleet_key = target_fleet.strip().upper()
    if fleet_key not in _FLEET_DIRECTORY:
        return _error(
            "UNKNOWN_FLEET",
            f"'{target_fleet}' is not a registered counterparty fleet.",
            known_fleets=sorted(_FLEET_DIRECTORY),
        )
    if required_delta_v <= 0.0:
        return _error("INVALID_DELTA_V", "required_delta_v must be strictly positive.")
    if required_delta_v > ABSOLUTE_DELTA_V_LIMIT_MPS:
        return _error(
            "POLICY_BLOCK_ABSOLUTE_CEILING",
            f"Requested delta-v exceeds the absolute {ABSOLUTE_DELTA_V_LIMIT_MPS} m/s ceiling.",
            requested_delta_v_mps=round(required_delta_v, 4),
            absolute_ceiling_mps=ABSOLUTE_DELTA_V_LIMIT_MPS,
        )

    fleet = _FLEET_DIRECTORY[fleet_key]
    base_response: dict[str, Any] = {
        "status": "ok",
        "target_fleet": fleet_key,
        "policy_version": "ORBIT-FLEET-POLICY-1.0",
        "simulated": True,
    }

    if required_delta_v > MAX_NEGOTIABLE_DELTA_V_MPS:
        base_response.update(
            accepted=False,
            reason="DELTA_V_EXCEEDS_COUNTERPARTY_POLICY",
            requested_delta_v_mps=round(required_delta_v, 4),
            counterparty_limit_mps=MAX_NEGOTIABLE_DELTA_V_MPS,
        )
        return base_response
    if required_delta_v > float(fleet["fuel_budget_delta_v_mps"]):
        base_response.update(
            accepted=False,
            reason="INSUFFICIENT_COUNTERPARTY_FUEL",
            requested_delta_v_mps=round(required_delta_v, 4),
            counterparty_fuel_budget_mps=fleet["fuel_budget_delta_v_mps"],
        )
        return base_response

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=2)
    window_end = now + timedelta(hours=6)
    terms = f"{fleet_key}|{required_delta_v:.6f}|{window_start.isoformat()}|{window_end.isoformat()}"
    signing_key = os.getenv(_SIGNING_KEY_ENV_VAR, _FALLBACK_SIM_SIGNING_KEY)
    signature = hmac.new(signing_key.encode("utf-8"), terms.encode("utf-8"), hashlib.sha256).hexdigest()
    reference_digest = hashlib.sha256(terms.encode("utf-8")).hexdigest()

    base_response.update(
        accepted=True,
        manoeuvre_reference=f"MVR-{reference_digest[:10].upper()}",
        signature_algorithm="HMAC-SHA256(simulated)",
        acknowledgement_signature=signature,
        burn_window_start_utc=_iso_z(window_start),
        burn_window_end_utc=_iso_z(window_end),
        fleet_autonomy_mode=fleet["autonomy_mode"],
        contact_endpoint=fleet["contact_endpoint"],
        estimated_round_trip_ms=140 + int(reference_digest[:4], 16) % 360,
    )
    return base_response


# ---------------------------------------------------------------------------
# Live orbital snapshot (consumed by the /api/orbital_state visualisation API)
# ---------------------------------------------------------------------------

_WGS84_FLATTENING: Final[float] = 1.0 / 298.257223563
_SATELLITE_COLOR_HEX: Final[str] = "#38bdf8"
_DEBRIS_COLOR_HEX: Final[str] = "#ef4444"
#: Conjunction lines are only drawn for MEDIUM/HIGH encounters; LOW pairs
#: would add dozens of near-invisible lines across the globe.
_SNAPSHOT_MIN_RISK_BAND: Final[str] = "MEDIUM"


def _teme_to_geodetic(position_teme_km: np.ndarray, gmst_rad: float) -> tuple[float, float, float]:
    """TEME position (km) → (geodetic_lat_deg, lon_deg, altitude_km).

    TEME is Earth-centred inertial; rotating the right-ascension by GMST
    yields an Earth-fixed vector, which is then converted to WGS84 geodetic
    coordinates with a closed-form first guess plus three Bowring iterations
    (converges to sub-millimetre at LEO altitudes).
    """
    x, y, z = float(position_teme_km[0]), float(position_teme_km[1]), float(position_teme_km[2])
    lon = math.atan2(y, x) - gmst_rad
    lon = (lon + math.pi) % (2.0 * math.pi) - math.pi

    r_xy = math.hypot(x, y)
    e2 = _WGS84_FLATTENING * (2.0 - _WGS84_FLATTENING)
    lat = math.atan2(z, r_xy * (1.0 - e2))
    n_radius = EARTH_MEAN_RADIUS_KM
    for _ in range(3):
        sin_lat = math.sin(lat)
        n_radius = EARTH_MEAN_RADIUS_KM / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        lat = math.atan2(z + n_radius * e2 * sin_lat, r_xy)
    sin_lat = math.sin(lat)
    n_radius = EARTH_MEAN_RADIUS_KM / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    altitude = r_xy / math.cos(lat) - n_radius if abs(math.cos(lat)) > 1e-9 else abs(z) - n_radius * (1.0 - e2)
    return math.degrees(lat), math.degrees(lon), max(altitude, 0.0)


def propagate_all_objects() -> list[dict[str, Any]]:
    """Propagate every catalogued object to the current UTC instant.

    Returns one record per object: identity, satellite/debris kind, WGS84
    geodetic position and inertial speed — exactly what a 3D front end needs
    to place points on the globe.
    """
    jd, fr = _julian_parts(datetime.now(timezone.utc))
    gmst = gstime(jd + fr)
    objects: list[dict[str, Any]] = []
    for key, entry in _CATALOG.items():
        satrec = Satrec.twoline2rv(_build_line1(entry), _build_line2(entry))
        error, r_teme, v_teme = satrec.sgp4(jd, fr)
        if error != 0:
            continue
        lat, lon, alt = _teme_to_geodetic(r_teme, gmst)
        objects.append(
            {
                "id": key,
                "name": entry.name,
                "type": "satellite" if entry.kind == "payload" else "debris",
                "norad_id": entry.norad_id,
                "operator": entry.operator,
                "owned": key == PROTECTED_SAT_ID,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "alt_km": round(alt, 2),
                "velocity_km_s": round(float(np.linalg.norm(v_teme)), 4),
                "inclination_deg": round(float(satrec.inclo) * 180.0 / math.pi, 3),
                "color": _SATELLITE_COLOR_HEX if entry.kind == "payload" else _DEBRIS_COLOR_HEX,
            }
        )
    return objects


def _screen_pair_cached(sat_key: str, debris_key: str) -> dict[str, Any] | None:
    """Screen one payload×debris pair; memoised forever because the synthetic
    catalogue's elements never change, so every replay is byte-identical."""
    result = screen_conjunction(sat_key, debris_key)
    if result.get("status") != "ok":
        return None
    return {
        "sat_id": result["sat_id"],
        "debris_id": result["debris_id"],
        "tca_utc": result["tca_utc"],
        "miss_distance_km": result["miss_distance_km"],
        "probability_of_collision": result["probability_of_collision"],
        "risk_band": result["risk_level"],
    }


_PAIR_SCREEN_CACHE: Final[dict[tuple[str, str], dict[str, Any] | None]] = {}


def active_conjunctions() -> list[dict[str, Any]]:
    """All currently-screened non-LOW conjunctions in the catalogue.

    Payloads are protected assets; debris objects are the secondaries. The
    cache makes repeated polls from a live dashboard free after the first.
    """
    payloads = [key for key, e in _CATALOG.items() if e.kind == "payload"]
    debris = [key for key, e in _CATALOG.items() if e.kind == "debris"]
    active: list[dict[str, Any]] = []
    for sat_key in sorted(payloads):
        for debris_key in sorted(debris):
            pair = (sat_key, debris_key)
            if pair not in _PAIR_SCREEN_CACHE:
                _PAIR_SCREEN_CACHE[pair] = _screen_pair_cached(*pair)
            screened = _PAIR_SCREEN_CACHE[pair]
            if screened and screened["risk_band"] in ("HIGH", _SNAPSHOT_MIN_RISK_BAND):
                active.append(screened)
    return active


def catalog_identity(object_id: str) -> dict[str, Any]:
    """Identity fields for one catalogued object, for coordination artifacts.

    Unknown objects resolve to a clearly-marked placeholder rather than
    raising: a coordination request naming an unidentified secondary is still
    more useful to a human operator than no request at all.
    """
    key = resolve_object_key(object_id)
    entry = _CATALOG.get(key)
    if entry is None:
        live = _LAST_LIVE_OBJECTS.get(key)
        if live is None and _known_object(key):
            live = _LAST_LIVE_OBJECTS.get(key)
        if live is not None:
            payload_like = str(live.get("object_type", "")).upper() == "PAYLOAD"
            return {
                "id": key,
                "name": live.get("name", key),
                "norad_id": live.get("norad_id", "UNKNOWN"),
                "operator": live.get("operator") or "UNKNOWN OPERATOR",
                "owned": bool(live.get("owned")),
                "kind": "payload" if payload_like else "debris",
                "manoeuvrable": bool(live.get("possibly_manoeuvrable")),
                "manoeuvrability": live.get("manoeuvrability", "unknown"),
                "source": "space-track/gp",
            }
        return {
            "id": key,
            "name": key,
            "norad_id": "UNKNOWN",
            "operator": "UNKNOWN OPERATOR",
            "kind": "unknown",
            "manoeuvrable": False,
            "manoeuvrability": "unknown",
        }
    return {
        "id": key,
        "name": entry.name,
        "norad_id": entry.norad_id,
        "operator": entry.operator,
        "owned": key == PROTECTED_SAT_ID,
        "kind": entry.kind,
        "manoeuvrable": entry.kind == "payload",
        "simulated_counterparty": key in SIMULATED_COUNTERPARTIES,
    }

LIVE_MODE: Final[str] = os.environ.get("ORBIT_LIVE_MODE", "auto").strip().lower()


def _live_mode_enabled() -> bool:
    if LIVE_MODE in {"0", "false", "off", "no"}:
        return False
    if LIVE_MODE in {"1", "true", "on", "yes"}:
        return True
    try:
        from tools.space_track_api import credentials_configured

        return credentials_configured()
    except Exception:  # noqa: BLE001
        return False

_LAST_LIVE_OBJECTS: dict[str, dict[str, Any]] = {}

_LAST_LIVE_ENCOUNTERS: dict[tuple[str, str], dict[str, Any]] = {}


def resolve_object_key(raw: Any) -> str:
    """Best-effort resolution of an object identifier to a catalogue key.

    Ids do not survive a round trip through an LLM unchanged. The alert
    triage agent normalises inbound alerts, and in doing so it rewrites
    ``COSMOS_1328_12987`` as ``COSMOS 1328 12987`` — the object's *name*
    followed by its catalogue number, which is a perfectly reasonable
    reading of the id and completely useless as a dict key.

    Every lookup in this module was an exact match on the upper-cased string,
    so that rewrite silently turned a real object into an unidentified one.
    The consequences were not cosmetic:

    * ``catalog_identity`` fell through to its "genuinely unidentified"
      branch, which deliberately reports ``manoeuvrable: True`` — so the
      fleet negotiated with a piece of debris, deadlocked, and escalated a
      HIGH-risk conjunction to a human for an arbitration nobody can perform.
    * The emitted CCSDS CDM carried ``OBJECT_DESIGNATOR = UNKNOWN`` and
      ``OPERATOR_ORGANIZATION = UNKNOWN OPERATOR`` for *our own asset*, in a
      message addressed to another operator's conjunction assessment desk.

    Resolution order: exact key, then the same slug transform
    :func:`_live_object_id` applies when the id is minted, so any separator
    an agent chooses collapses back to the canonical form. Unresolvable input
    returns the normalised form, which keeps the "unknown object" error
    message readable.
    """
    candidate = str(raw or "").strip().upper()
    if not candidate:
        return ""
    if candidate in _CATALOG or candidate in _LAST_LIVE_OBJECTS:
        return candidate
    normalised = re.sub(r"[^A-Z0-9]+", "_", candidate).strip("_")
    if normalised in _CATALOG or normalised in _LAST_LIVE_OBJECTS:
        return normalised
    return normalised or candidate


def _screening_from_cdm(sat_key: str, debris_key: str, cdm: dict[str, Any]) -> dict[str, Any]:
    """Express a published Conjunction Data Message in screening shape."""
    pc = float(cdm.get("pc") or 0.0)
    miss_km = float(cdm.get("miss_distance_km") or 0.0)
    high_threshold, medium_threshold = _risk_thresholds()

    if pc >= high_threshold:
        risk_level = "HIGH"
        recommended_action = "EVALUATE AVOIDANCE: probability exceeds the action threshold."
    elif pc >= medium_threshold:
        risk_level = "MEDIUM"
        recommended_action = "MONITOR: reassess on next ground pass and prepare a contingency burn plan."
    else:
        risk_level = "LOW"
        recommended_action = "NO ACTION: record in the mission log and continue nominal operations."

    rel_speed_km_s = cdm.get("relative_velocity_km_s")
    if rel_speed_km_s is None:
        rel_speed_km_s = _relative_speed_at(sat_key, debris_key, str(cdm.get("tca_iso") or ""))

    return {
        "status": "ok",
        "sat_id": sat_key,
        "debris_id": debris_key,
        "tca_utc": cdm.get("tca_iso"),
        "miss_distance_km": round(miss_km, 4),
        "relative_velocity_km_s": rel_speed_km_s,
        "combined_hbr_km": COMBINED_HBR_KM,
        "probability_of_collision": float(f"{max(pc, 1.0e-15):.6e}"),
        "risk_level": risk_level,
        "recommended_action": recommended_action,
        "method": "published_cdm/18sds",
        "cdm_id": cdm.get("cdm_id"),
        "emergency_reportable": cdm.get("emergency_reportable"),
        "policy_thresholds": {"high": high_threshold, "medium": medium_threshold},
        "element_source": "space-track/cdm_public",
        "simulated": False,
    }


def _relative_speed_at(sat_key: str, debris_key: str, tca_iso: str) -> float | None:
    """Relative speed of two objects at a given instant, via SGP4."""
    try:
        moment = datetime.fromisoformat(tca_iso.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        primary, secondary = _satrec_for(sat_key), _satrec_for(debris_key)
        if primary is None or secondary is None:
            return None
        jd, fr = _julian_parts(moment)
        error_a, _, v_a = primary.sgp4(jd, fr)
        error_b, _, v_b = secondary.sgp4(jd, fr)
        if error_a != 0 or error_b != 0:
            return None
        return round(float(np.linalg.norm(np.array(v_a) - np.array(v_b))), 4)
    except Exception:
        return None


def _satrec_for(object_key: str) -> Any | None:
    """Build an SGP4 propagator for either a live or a fixture object.

    Identity in this fleet arrives from two places
    Returns None when the id is unknown to both.
    """
    entry = _CATALOG.get(object_key)
    if entry is not None:
        return Satrec.twoline2rv(_build_line1(entry), _build_line2(entry))

    live = _LAST_LIVE_OBJECTS.get(object_key)
    if live and live.get("tle_line1") and live.get("tle_line2"):
        try:
            return Satrec.twoline2rv(str(live["tle_line1"]), str(live["tle_line2"]))
        except Exception:
            return None
    return None


def _known_object(object_key: str) -> bool:
    """True when the id resolves to either a live object or a fixture. """
    if object_key in _CATALOG or object_key in _LAST_LIVE_OBJECTS:
        return True
    if not _LAST_LIVE_OBJECTS:
        try:
            get_live_orbital_snapshot()
        except Exception:
            return False
    return object_key in _LAST_LIVE_OBJECTS


def _live_object_id(name: Any, norad_id: str) -> str:
    """Stable, unique id for a live object.

    Debris fields share a name across hundreds of fragments, so the name
    alone collides; the catalogue number disambiguates.
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", str(name or "OBJECT").upper()).strip("_")
    return f"{slug or 'OBJECT'}_{norad_id}"


def _propagate_tle_row(row: dict[str, Any], jd: float, fr: float, gmst: float) -> dict[str, Any] | None:
    """Propagate one real element set to the current instant for the globe."""
    try:
        satrec = Satrec.twoline2rv(str(row["tle_line1"]), str(row["tle_line2"]))
        error, r_teme, v_teme = satrec.sgp4(jd, fr)
        if error != 0:
            return None
        lat, lon, alt = _teme_to_geodetic(r_teme, gmst)
    except Exception:
        return None
    return {
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "alt_km": round(alt, 2),
        "velocity_km_s": round(float(np.linalg.norm(v_teme)), 4),
        "inclination_deg": round(float(satrec.inclo) * 180.0 / math.pi, 3),
    }

_LIVE_INPUTS: dict[str, Any] = {}
LIVE_INPUTS_TTL_SECONDS: Final[float] = float(
    os.environ.get("ORBIT_LIVE_SNAPSHOT_TTL_SECONDS", "30")
)


def _live_inputs(encounters: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any], str]:
    """Fetch (or reuse) the raw ingredients of the live command picture."""
    now = time.monotonic()
    cached = _LIVE_INPUTS.get("value")
    if cached is not None and _LIVE_INPUTS.get("encounters") == encounters:
        if now - float(_LIVE_INPUTS.get("fetched_at", 0.0)) < LIVE_INPUTS_TTL_SECONDS:
            return cached

    chosen = select_live_protagonist(limit=100)
    if chosen.get("status") != "ok":
        raise RuntimeError(chosen.get("reason", "no live protagonist available"))

    feed = get_live_conjunctions(limit=100)
    top = [item for item in feed.get("encounters", []) if item.get("pc") is not None][:encounters]

    protagonist_id = str(chosen["protagonist"]["norad_id"])
    wanted: dict[str, dict[str, Any]] = {}
    for item in top:
        for side in ("object_1", "object_2"):
            obj = item[side]
            wanted[str(obj["norad_id"])] = obj
    wanted[protagonist_id] = chosen["protagonist"]

    from tools.space_track_api import get_shared_space_track_client

    elsets = get_shared_space_track_client().fetch_tles([int(key) for key in wanted])
    if protagonist_id not in elsets:
        raise RuntimeError(f"no current element set for protagonist {protagonist_id}")

    value = (chosen, top, wanted, elsets, protagonist_id)
    _LIVE_INPUTS.update({"value": value, "fetched_at": now, "encounters": encounters})
    return value


def get_live_orbital_snapshot(encounters: int = 5) -> dict[str, Any]:
    """The real command picture: actual objects, actual close approaches.
    Raises:
        RuntimeError: when live data is unavailable, so the caller can fall back to the synthetic catalogue.
    """
    chosen, top, wanted, elsets, protagonist_id = _live_inputs(encounters)

    jd, fr = _julian_parts(datetime.now(timezone.utc))
    gmst = gstime(jd + fr)

    objects: list[dict[str, Any]] = []
    for norad_id, meta in wanted.items():
        row = elsets.get(norad_id)
        if row is None:
            continue
        state = _propagate_tle_row(row, jd, fr, gmst)
        if state is None:
            continue
        is_payload = str(meta.get("object_type", "")).upper() == "PAYLOAD"
        objects.append(
            {
                "id": _live_object_id(meta.get("name") or row.get("object_name"), norad_id),
                "name": str(meta.get("name") or row.get("object_name") or norad_id),
                "type": "satellite" if is_payload else "debris",
                "norad_id": int(norad_id),
                "object_type": meta.get("object_type"),
                "operator": None,
                "owned": norad_id == protagonist_id,
                "possibly_manoeuvrable": bool(meta.get("possibly_manoeuvrable")),
                "manoeuvrability": meta.get("manoeuvrability", "unknown"),
                "tle_line1": row.get("tle_line1"),
                "tle_line2": row.get("tle_line2"),
                "epoch_utc": row.get("epoch_utc"),
                "color": _SATELLITE_COLOR_HEX if is_payload else _DEBRIS_COLOR_HEX,
                **state,
            }
        )

    _LAST_LIVE_OBJECTS.clear()
    _LAST_LIVE_OBJECTS.update({obj["id"]: obj for obj in objects})
    _LAST_LIVE_ENCOUNTERS.clear()

    by_norad = {str(obj["norad_id"]): obj for obj in objects}
    conjunctions: list[dict[str, Any]] = []
    for item in top:
        one = by_norad.get(str(item["object_1"]["norad_id"]))
        two = by_norad.get(str(item["object_2"]["norad_id"]))
        if not one or not two:
            continue

        if two["id"] == by_norad[protagonist_id]["id"] or (
            not one["owned"] and one["type"] != "satellite" and two["type"] == "satellite"
        ):
            one, two = two, one
        conjunctions.append(
            {
                "sat_id": one["id"],
                "debris_id": two["id"],
                "tca_utc": item["tca_iso"],
                "miss_distance_km": item["miss_distance_km"],
                "probability_of_collision": item["pc"],
                "risk_band": item["risk_band"],
                "coordination_candidate": item["coordination_candidate"],
                "source": item["source"],
            }
        )
        _LAST_LIVE_ENCOUNTERS[(min(one["id"], two["id"]), max(one["id"], two["id"]))] = item

    return {
        "generated_utc": _iso_z(datetime.now(timezone.utc)),
        "objects": objects,
        "conjunctions": conjunctions,
        "protected_sat_id": by_norad[protagonist_id]["id"],
        "protected_norad_id": int(protagonist_id),
        "counterparty_norad_id": int(chosen["counterparty"]["norad_id"]),
        "response_mode": chosen["response_mode"],
        "coordination_candidate": chosen["coordination_candidate"],
        "source": "space-track/cdm_public+gp",
        "simulated": False,
    }

EXERCISE_ASSET_ID: Final[str] = "SIM_PROTECTED_ASSET"
EXERCISE_COUNTERPARTY_ID: Final[str] = "SIM_COORDINATION_TARGET"

_EXERCISE_COLOR_HEX: Final[str] = "#f59e0b"

_EXERCISE_SCREEN_CACHE: dict[str, Any] = {}
EXERCISE_SCREEN_TTL_SECONDS: Final[float] = float(
    os.environ.get("ORBIT_EXERCISE_SCREEN_TTL_SECONDS", "30")
)


def _exercise_screening() -> dict[str, Any]:
    """The exercise pair's screened encounter, memoised for the poll interval."""
    now = time.monotonic()
    cached = _EXERCISE_SCREEN_CACHE.get("value")
    if cached is not None and now - float(_EXERCISE_SCREEN_CACHE.get("fetched_at", 0.0)) < EXERCISE_SCREEN_TTL_SECONDS:
        return cached
    screened = screen_conjunction(EXERCISE_ASSET_ID, EXERCISE_COUNTERPARTY_ID)
    _EXERCISE_SCREEN_CACHE.update({"value": screened, "fetched_at": now})
    return screened


def exercise_overlay() -> dict[str, Any]:
    """The simulated coordination pair, for display alongside live objects.

    Returned separately from the live picture and flagged ``exercise`` on
    every record, so the front end can style and label it as a simulation
    rather than folding it into the real catalogue counts.
    """
    jd, fr = _julian_parts(datetime.now(timezone.utc))
    gmst = gstime(jd + fr)

    objects: list[dict[str, Any]] = []
    for key, role in (
        (EXERCISE_ASSET_ID, "exercise_asset"),
        (EXERCISE_COUNTERPARTY_ID, "exercise_counterparty"),
    ):
        entry = _CATALOG.get(key)
        if entry is None:
            continue
        satrec = Satrec.twoline2rv(_build_line1(entry), _build_line2(entry))
        error, r_teme, v_teme = satrec.sgp4(jd, fr)
        if error != 0:
            continue
        lat, lon, alt = _teme_to_geodetic(r_teme, gmst)
        objects.append(
            {
                "id": key,
                "name": entry.name,
                "type": "satellite" if entry.kind == "payload" else "debris",
                "norad_id": entry.norad_id,
                "operator": entry.operator,
                "owned": False,
                "exercise": True,
                "role": role,
                "simulated": True,
                "possibly_manoeuvrable": entry.kind == "payload",
                "manoeuvrability": "unknown" if entry.kind == "payload" else "none",
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "alt_km": round(alt, 2),
                "velocity_km_s": round(float(np.linalg.norm(v_teme)), 4),
                "inclination_deg": round(float(satrec.inclo) * 180.0 / math.pi, 3),
                "color": _EXERCISE_COLOR_HEX,
            }
        )

    conjunctions: list[dict[str, Any]] = []
    screened = _exercise_screening()
    if screened.get("status") == "ok" and len(objects) == 2:
        conjunctions.append(
            {
                "sat_id": EXERCISE_ASSET_ID,
                "debris_id": EXERCISE_COUNTERPARTY_ID,
                "tca_utc": screened["tca_utc"],
                "miss_distance_km": screened["miss_distance_km"],
                "probability_of_collision": screened["probability_of_collision"],
                "risk_band": screened["risk_level"],
                "coordination_candidate": True,
                "exercise": True,
                "source": "simulated_catalogue/v1",
            }
        )

    return {"objects": objects, "conjunctions": conjunctions}


def get_orbital_snapshot(include_exercise: bool = False) -> dict[str, Any]:
    """One consistent frame of the tracked-space picture for the command UI.

    Live objects only. Every point on this map is a real catalogued object
    at a position propagated from its current element set.
    """
    overlay = exercise_overlay() if include_exercise else {"objects": [], "conjunctions": []}

    try:
        snapshot = get_live_orbital_snapshot()
        snapshot["objects"] = list(snapshot["objects"]) + overlay["objects"]
        snapshot["conjunctions"] = list(snapshot["conjunctions"]) + overlay["conjunctions"]
        snapshot["exercise_active"] = bool(overlay["objects"])
        return snapshot
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _spacetrack_fallback_audit("get_orbital_snapshot", reason)

    now = datetime.now(timezone.utc)
    return {
        "generated_utc": _iso_z(now),
        "status": "unavailable",
        "objects": overlay["objects"],
        "conjunctions": overlay["conjunctions"],
        "exercise_active": bool(overlay["objects"]),
        "protected_sat_id": None,
        "source": "space-track/unavailable",
        "reason": reason[:300],
        "simulated": False,
    }


# ---------------------------------------------------------------------------
# Phase 8 — real-data tools with synthetic fallback (AstrodynamicsAgent)
# ---------------------------------------------------------------------------


def _spacetrack_fallback_audit(tool: str, reason: str) -> None:
    audit_logger.log_event(
        trace_id="spacetrack",
        agent_name="tools.space_tools",
        event_type="SPACETRACK_FALLBACK_SYNTHETIC",
        payload={"tool": tool, "reason": reason[:200]},
        status="DEGRADED",
    )


def _run_coroutine_blocking(awaitable: Any) -> Any:
    """Bridge a coroutine into sync tool context.

    ADK executes sync tools on a worker thread with no running loop, so
    ``asyncio.run`` is safe there; should a loop somehow be active in this
    thread, the coroutine is executed on a dedicated one-off thread instead
    of nesting loops.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, awaitable).result()


def fetch_real_tle(satellite_id: str) -> dict[str, Any]:
    """Fetch live Two-Line Element data for an object from Space-Track.org.

    Tries Space-Track first (real Space Surveillance Network elements,
    cached to avoid rate limits) and falls back to the calibrated synthetic
    catalogue whenever credentials are missing, the network is down or the
    object has no real counterpart. The response states its provenance via
    ``source`` so downstream reasoning never confuses the two.

    Args:
        satellite_id: Catalogue identifier of the object, e.g.
            as shown in the command picture, e.g. "COSMOS_864_9509".

    Returns:
        Same schema as get_tle_data plus ``source`` ("space-track/v1" or
        "simulated_catalogue/v1") and ``fallback_reason`` when degraded.
    """
    key = resolve_object_key(satellite_id)
    try:
        from tools.space_track_api import get_shared_space_track_client

        rows = get_shared_space_track_client().fetch_tle(key)
        if not rows:
            raise RuntimeError(f"Space-Track returned no element sets for '{key}'.")
        row = rows[0]
        return {
            "status": "ok",
            "satellite_id": key,
            "name": row.get("object_name", key),
            "norad_id": row.get("norad_cat_id"),
            "tle_line1": row["tle_line1"],
            "tle_line2": row["tle_line2"],
            "epoch_utc": row.get("epoch_utc"),
            "source": "space-track/v1",
            "simulated": False,
        }
    except Exception as exc:  # noqa: BLE001 — any failure degrades gracefully
        _spacetrack_fallback_audit("fetch_real_tle", f"{type(exc).__name__}: {exc}")
        result = get_tle_data(satellite_id)
        if result.get("status") == "ok":
            result["fallback_reason"] = str(exc)[:200]
        return result


def fetch_conjunction_screening(satellite_id: str) -> dict[str, Any]:
    """Retrieve real Conjunction Data Messages (CDMs) for one asset.

    Queries Space-Track's conjunction API for recent CDMs involving
    ``satellite_id`` and normalises them into screening-shaped records
    (TCA, miss distance, Pc, CARA band). When real data is unavailable the
    deterministic SGP4 screening stands in, clearly labelled as simulated.

    Args:
        satellite_id: Catalogue identifier of the protected asset.

    Returns:
        ``{"status": "ok", "source": "space-track/v1"|"simulated_sgp4/v1",
           "asset_id", "cdms": [...], "max_risk_band"}`` on success.
    """
    key = resolve_object_key(satellite_id)
    entry = _CATALOG.get(key)
    try:
        if entry is None:
            return _unknown_object_error(satellite_id)
        from tools.space_track_api import get_shared_space_track_client

        norad = int(entry.norad_id)
        cdms = get_shared_space_track_client().fetch_cdms(norad)
        bands = [str(cdm.get("risk_band")) for cdm in cdms if cdm.get("risk_band")]
        max_band = "HIGH" if "HIGH" in bands else "MEDIUM" if "MEDIUM" in bands else "LOW" if "LOW" in bands else None
        return {
            "status": "ok",
            "source": "space-track/v1",
            "asset_id": key,
            "norad_cat_id": norad,
            "cdm_count": len(cdms),
            "cdms": cdms,
            "max_risk_band": max_band,
            "simulated": False,
        }
    except Exception as exc:  # noqa: BLE001 — degrade to SGP4 screening
        _spacetrack_fallback_audit("fetch_conjunction_screening", f"{type(exc).__name__}: {exc}")

    # Synthetic stand-in: screen our asset against every catalogued debris
    # object and report the worst encounter, same shape as the CDM path.
    if entry is None:
        return _unknown_object_error(satellite_id)
    screens: list[dict[str, Any]] = []
    for other_key, other in sorted(_CATALOG.items()):
        if other_key == key or other.kind != "debris":
            continue
        screened = _screen_pair_cached(key, other_key)
        if screened:
            screens.append(screened)
    screens.sort(key=lambda item: item["probability_of_collision"], reverse=True)
    worst = screens[0] if screens else None
    return {
        "status": "ok",
        "source": "simulated_sgp4/v1",
        "asset_id": key,
        "cdm_count": len(screens),
        "cdms": [
            {
                "cdm_id": f"SIM-{item['sat_id']}-{item['debris_id']}",
                "tca_iso": item["tca_utc"],
                "miss_distance_km": item["miss_distance_km"],
                "pc": item["probability_of_collision"],
                "risk_band": item["risk_band"],
                "created_utc": None,
                "source": "simulated_sgp4/v1",
            }
            for item in screens[:10]
        ],
        "max_risk_band": worst["risk_band"] if worst else None,
        "fallback_note": "Synthetic SGP4 screening used; no Space-Track credentials/data available.",
        "simulated": True,
    }


def recall_similar_conjunctions(
    risk_band: str,
    miss_distance_km: float,
    pc: float,
    debris_type_hint: str = "",
    fuel_percent_remaining: float | None = None,
) -> dict[str, Any]:
    """Recall similar past conjunctions from the fleet's vector memory.

    Embeds the current situation and returns the most similar historical
    encounters *with the actions that resolved them*, letting recommendations
    be grounded in fleet experience instead of starting from scratch.

    Args:
        risk_band: Current CARA band ("LOW"|"MEDIUM"|"HIGH").
        miss_distance_km: Current miss-distance estimate in km.
        pc: Current collision probability estimate.
        debris_type_hint: Optional catalogue identifier of the secondary.
        fuel_percent_remaining: Optional current fuel reserve percentage.

    Returns:
        ``{"status": "ok", "count": N, "matches": [{..., "similarity"}],
          "summary": "<human-readable precedent line>"}``
    """
    try:
        from geap_sim.memory_bank import build_conjunction_context, get_shared_memory_bank

        context = build_conjunction_context(
            {
                "risk_band": risk_band,
                "miss_distance_km": miss_distance_km,
                "pc": pc,
                "debris_id": debris_type_hint,
                "fuel_percentage": fuel_percent_remaining,
            }
        )
        matches = _run_coroutine_blocking(get_shared_memory_bank().find_similar_conjunctions(context, top_k=5))
        dv_values = [float(m["our_dv_mps"]) for m in matches if isinstance(m.get("our_dv_mps"), (int, float)) and m["our_dv_mps"] > 0]
        if matches and dv_values:
            summary = (
                f"Based on {len(matches)} similar past conjunctions, the executed delta-v range "
                f"was {min(dv_values):.1f}-{max(dv_values):.1f} m/s "
                f"(best similarity {matches[0]['similarity']:.2f})."
            )
        elif matches:
            summary = f"Based on {len(matches)} similar past conjunctions; none required an avoidance burn."
        else:
            summary = "No similar past conjunctions on record yet; this encounter will seed fleet memory."
        return {
            "status": "ok",
            "count": len(matches),
            "query_context": context,
            "matches": matches,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001 — recall must never break screening
        audit_logger.log_event(
            trace_id="memory-bank",
            agent_name="tools.space_tools",
            event_type="VECTOR_RECALL_FAILED",
            payload={"error_type": type(exc).__name__, "error": str(exc)[:200]},
            status="DEGRADED",
        )
        return {"status": "error", "error_code": "VECTOR_MEMORY_UNAVAILABLE", "message": str(exc)[:200], "count": 0}


def get_live_conjunctions(limit: int = 25) -> dict[str, Any]:
    """Current real conjunctions from Space-Track's public CDM feed.

    Unlike ``fetch_conjunction_screening``, which asks "what threatens *our*
    asset", this asks "what is genuinely dangerous in orbit right now". The
    ``cdm_public`` class is the released slice of the conjunction picture
    across the whole catalogue, filtered to close approaches that have not
    yet happened.

    Each encounter reports whether either object can manoeuvre. This is the
    decisive fact for response planning: debris and spent rocket bodies have
    no operator and no thrusters, so an encounter involving one admits only
    unilateral avoidance by the other party. Coordination is possible only
    when *both* objects are active payloads.

    Args:
        limit: Maximum encounters to retrieve (1-100).

    Returns:
        ``{"status": "ok", "source", "encounters": [...], "counts": {...}}``
        with encounters sorted by collision probability, descending. Falls
        back to the synthetic catalogue with ``source`` stating so.
    """
    try:
        from tools.space_track_api import get_shared_space_track_client

        feed = get_shared_space_track_client().fetch_recent_public_cdms(limit=limit)
        candidates = sum(1 for item in feed if item.get("coordination_candidate"))
        with_payload = sum(
            1
            for item in feed
            if item["object_1"]["possibly_manoeuvrable"] or item["object_2"]["possibly_manoeuvrable"]
        )
        return {
            "status": "ok",
            "source": "space-track/cdm_public",
            "simulated": False,
            "encounters": feed,
            "counts": {
                "total": len(feed),
                "high_band": sum(1 for item in feed if item.get("risk_band") == "HIGH"),
                "involving_payload": with_payload,
                "coordination_candidates": candidates,
            },
        }
    except Exception as exc:
        _spacetrack_fallback_audit("get_live_conjunctions", f"{type(exc).__name__}: {exc}")
        return {
            "status": "degraded",
            "source": "simulated_sgp4/v1",
            "simulated": True,
            "encounters": [],
            "counts": {"total": 0, "high_band": 0, "involving_payload": 0,"coordination_candidates": 0},
            "fallback_reason": str(exc)[:200],
        }


def select_live_protagonist(limit: int = 25) -> dict[str, Any]:
    """Choose the real payload most in need of a response right now.

    The fleet defends whichever manoeuvrable spacecraft currently
    faces the highest-probability close approach.
    """
    feed = get_live_conjunctions(limit=limit)
    if feed.get("status") != "ok" or not feed.get("encounters"):
        return {
            "status": "unavailable",
            "reason": feed.get("fallback_reason", "live conjunction feed returned nothing"),
            "source": feed.get("source"),
        }

    for encounter in feed["encounters"]:
        one, two = encounter["object_1"], encounter["object_2"]
        if one["possibly_manoeuvrable"]:
            protagonist, counterparty = one, two
        elif two["possibly_manoeuvrable"]:
            protagonist, counterparty = two, one
        else:
            continue

        return {
            "status": "ok",
            "source": encounter["source"],
            "simulated": False,
            "protagonist": protagonist,
            "counterparty": counterparty,
            "screening": {
                "cdm_id": encounter["cdm_id"],
                "tca_iso": encounter["tca_iso"],
                "miss_distance_km": encounter["miss_distance_km"],
                "miss_distance_m": encounter["miss_distance_m"],
                "pc": encounter["pc"],
                "risk_band": encounter["risk_band"],
                "emergency_reportable": encounter["emergency_reportable"],
            },
            "response_mode": (
                "attempt_coordination" if counterparty["possibly_manoeuvrable"] else "unilateral_avoidance"
            ),
            "coordination_candidate": bool(counterparty["possibly_manoeuvrable"]),
            "counterparty_manoeuvrability": counterparty["manoeuvrability"],
        }

    return {
        "status": "no_candidate",
        "reason": (
            f"None of the {len(feed['encounters'])} live encounters involves a payload; "
            "every object is debris or a spent rocket body."
        ),
        "source": feed.get("source"),
    }


# ---------------------------------------------------------------------------
# ADK registration — toolkits are consumed strictly per-agent role
# ---------------------------------------------------------------------------

get_tle_data_tool: Final[FunctionTool] = FunctionTool(func=get_tle_data)
screen_conjunction_tool: Final[FunctionTool] = FunctionTool(func=screen_conjunction)
negotiate_dodge_maneuver_tool: Final[FunctionTool] = FunctionTool(func=negotiate_dodge_maneuver)
fetch_real_tle_tool: Final[FunctionTool] = FunctionTool(func=fetch_real_tle)
fetch_conjunction_screening_tool: Final[FunctionTool] = FunctionTool(func=fetch_conjunction_screening)
recall_similar_conjunctions_tool: Final[FunctionTool] = FunctionTool(func=recall_similar_conjunctions)
get_live_conjunctions_tool: Final[FunctionTool] = FunctionTool(func=get_live_conjunctions)
select_live_protagonist_tool: Final[FunctionTool] = FunctionTool(func=select_live_protagonist)

#: For AstrodynamicsAgent ONLY — pure math, no external side effects.
#: (fetch_real_tle/fetch_conjunction_screening touch the network but are
#: read-only and rate-limit-shielded; recall reads fleet vector memory.)
ASTRO_TOOLKIT: Final[tuple[FunctionTool, ...]] = (
    get_tle_data_tool,
    screen_conjunction_tool,
    fetch_real_tle_tool,
    fetch_conjunction_screening_tool,
    recall_similar_conjunctions_tool,
    get_live_conjunctions_tool,
    select_live_protagonist_tool,
)

#: For DiplomatAgent ONLY — the single sanctioned channel to outside fleets.
DIPLOMAT_TOOLKIT: Final[tuple[FunctionTool, ...]] = (negotiate_dodge_maneuver_tool,)

#: Backwards-compatible alias for the screening tool (pre-rename callers).
calculate_conjunction_probability = screen_conjunction

__all__ = [
    "resolve_object_key",
    "ABSOLUTE_DELTA_V_LIMIT_MPS",
    "ASTRO_TOOLKIT",
    "DIPLOMAT_TOOLKIT",
    "MAX_NEGOTIABLE_DELTA_V_MPS",
    "fetch_conjunction_screening_tool",
    "fetch_real_tle_tool",
    "get_live_conjunctions",
    "get_live_conjunctions_tool",
    "get_orbital_snapshot",
    "get_tle_data_tool",
    "negotiate_dodge_maneuver_tool",
    "recall_similar_conjunctions_tool",
    "screen_conjunction_tool",
    "select_live_protagonist",
    "select_live_protagonist_tool",
]
