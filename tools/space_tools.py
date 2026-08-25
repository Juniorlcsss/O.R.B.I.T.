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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

import numpy as np
from google.adk.tools import FunctionTool
from sgp4.api import Satrec, SatrecArray, jday

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


_CATALOG: Final[dict[str, _CatalogEntry]] = {
    "LANCASTER_ORBIT_1": _CatalogEntry(
        norad_id=99001,
        name="LANCASTER ORBIT-1 (University CubeSat)",
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
        operator="Lancaster University ORBIT Lab",
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
    # LANCASTER_ORBIT_1 so this scripted scenario screens as a genuine HIGH
    # conjunction (~89 m miss at TCA, Pc ~7.5e-4) under real SGP4
    # propagation. It models a near-coincident post-fragmentation debris
    # cloud member slowly converging with our CubeSat — the classic Kessler
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

# Import-time integrity gate: every synthesised TLE must be exactly 69 chars,
# satisfy the strict column template used by sgp4's parsers (a one-column
# drift silently corrupts the parsed epoch year), and round-trip through
# SGP4. A formatter bug fails fast here instead of mid-demo.
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


def _unknown_object_error(object_id: str) -> dict[str, Any]:
    return _error(
        "UNKNOWN_OBJECT_ID",
        f"'{object_id}' is not present in the simulated space-surveillance catalogue.",
        available_ids=sorted(_CATALOG),
    )


# ---------------------------------------------------------------------------
# ADK Tool 1/3 — catalogue lookup (AstrodynamicsAgent)
# ---------------------------------------------------------------------------


def get_tle_data(satellite_id: str) -> dict[str, Any]:
    """Fetch Two-Line Element (TLE) orbital parameters for a tracked object from the simulated space-surveillance catalogue.

    Use this before any conjunction analysis to obtain fresh orbital elements.
    Identifiers are case-insensitive; unknown identifiers return an error
    payload that lists all valid catalogue IDs so you can self-correct.

    Args:
        satellite_id: Catalogue identifier of the object, e.g.
            "LANCASTER_ORBIT_1" (our CubeSat), "ISS_ZARYA", "STARLINK_3042",
            or debris such as "FENGYUN_1C_DEB".

    Returns:
        A dict with keys: ``status`` ("ok"), ``satellite_id``, ``name``,
        ``norad_id``, ``object_kind``, ``operator``, ``tle_line1``, ``tle_line2``,
        ``epoch_utc``, ``inclination_deg``, ``raan_deg``, ``eccentricity``,
        ``argument_of_perigee_deg``, ``mean_anomaly_deg``,
        ``mean_motion_rev_per_day``, ``mean_altitude_km``, plus
        ``simulated`` (always true). On failure: ``status`` ("error"),
        ``error_code``, ``message``, and possibly ``available_ids``.
    """
    key = satellite_id.strip().upper()
    entry = _CATALOG.get(key)
    if entry is None:
        return _unknown_object_error(satellite_id)

    satrec = Satrec.twoline2rv(_build_line1(entry), _build_line2(entry))
    mean_motion_rad_s = satrec.no_kozai / 60.0
    semi_major_axis_km = (MU_EARTH_KM3_S2 / mean_motion_rad_s**2) ** (1.0 / 3.0)

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
        sat_id: Catalogue identifier of the protected asset, e.g.
            "LANCASTER_ORBIT_1".
        debris_id: Catalogue identifier of the secondary object, e.g.
            "FENGYUN_1C_DEB".

    Returns:
        A dict with keys: ``status`` ("ok"), ``sat_id``, ``debris_id``,
        ``tca_utc``, ``miss_distance_km``, ``relative_velocity_km_s``,
        ``combined_hbr_km``, ``sigma_km``, ``probability_of_collision``,
        ``risk_level`` ("HIGH"|"MEDIUM"|"LOW"), ``recommended_action``,
        ``method``, ``screening_window_hours``, ``policy_thresholds`` and
        ``simulated`` (always true). On failure: ``status`` ("error"),
        ``error_code``, ``message``.
    """
    sat_key, debris_key = sat_id.strip().upper(), debris_id.strip().upper()
    if sat_key not in _CATALOG:
        return _unknown_object_error(sat_id)
    if debris_key not in _CATALOG:
        return _unknown_object_error(debris_id)
    if sat_key == debris_key:
        return _error("IDENTICAL_OBJECTS", "Conjunction screening requires two distinct objects.")

    try:
        primary = Satrec.twoline2rv(_build_line1(_CATALOG[sat_key]), _build_line2(_CATALOG[sat_key]))
        secondary = Satrec.twoline2rv(_build_line1(_CATALOG[debris_key]), _build_line2(_CATALOG[debris_key]))
        tca, miss_km, rel_position, rel_velocity = _find_time_of_closest_approach(primary, secondary)
    except ValueError as exc:
        return _error("PROPAGATION_FAILURE", str(exc))

    sigma_km = _pair_covariance_sigma_km(sat_key, debris_key)
    scale_factor = COMBINED_HBR_KM**2 / (2.0 * sigma_km**2)
    probability = scale_factor * math.exp(-(miss_km**2) / (2.0 * sigma_km**2))
    probability = min(1.0, max(probability, 1.0e-15))

    if probability >= HIGH_RISK_THRESHOLD_P:
        risk_level, recommended_action = (
            "HIGH",
            "IMMEDIATE ACTION REQUIRED: escalate to FleetCommander for dodge coordination.",
        )
    elif probability >= MEDIUM_RISK_THRESHOLD_P:
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
        "policy_thresholds": {"high": HIGH_RISK_THRESHOLD_P, "medium": MEDIUM_RISK_THRESHOLD_P},
        "simulated": True,
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


def get_orbital_snapshot() -> dict[str, Any]:
    """One consistent frame of the tracked-space picture for the command UI."""
    now = datetime.now(timezone.utc)
    return {
        "generated_utc": _iso_z(now),
        "objects": propagate_all_objects(),
        "conjunctions": active_conjunctions(),
        "simulated": True,
    }


# ---------------------------------------------------------------------------
# ADK registration — toolkits are consumed strictly per-agent role
# ---------------------------------------------------------------------------

get_tle_data_tool: Final[FunctionTool] = FunctionTool(func=get_tle_data)
screen_conjunction_tool: Final[FunctionTool] = FunctionTool(func=screen_conjunction)
negotiate_dodge_maneuver_tool: Final[FunctionTool] = FunctionTool(func=negotiate_dodge_maneuver)

#: For AstrodynamicsAgent ONLY — pure math, no external side effects.
ASTRO_TOOLKIT: Final[tuple[FunctionTool, FunctionTool]] = (
    get_tle_data_tool,
    screen_conjunction_tool,
)

#: For DiplomatAgent ONLY — the single sanctioned channel to outside fleets.
DIPLOMAT_TOOLKIT: Final[tuple[FunctionTool, ...]] = (negotiate_dodge_maneuver_tool,)

#: Backwards-compatible alias for the screening tool (pre-rename callers).
calculate_conjunction_probability = screen_conjunction

__all__ = [
    "ABSOLUTE_DELTA_V_LIMIT_MPS",
    "ASTRO_TOOLKIT",
    "DIPLOMAT_TOOLKIT",
    "MAX_NEGOTIABLE_DELTA_V_MPS",
    "get_orbital_snapshot",
    "get_tle_data_tool",
    "negotiate_dodge_maneuver_tool",
    "screen_conjunction_tool",
]
