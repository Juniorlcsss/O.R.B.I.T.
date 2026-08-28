from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Final

#identify
ORIGINATOR: Final[str] = os.environ.get("ORBIT_ORIGINATOR", "ORBIT-OPERATOR")

RESPONSE_DEADLINE_HOURS: Final[float] = float(os.environ.get("ORBIT_COORDINATION_DEADLINE_H", "6"))


def _z(moment: datetime) -> str:
    """CCSDS-style timestamp: milliseconds, no offset suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def build_cdm(
    *,
    primary: dict[str, Any],
    secondary: dict[str, Any],
    screening: dict[str, Any],
    message_id: str,
    created: datetime,
) -> str:
    """Render the encounter as a CCSDS-CDM-shaped KVN message.

    A documented subset, not a conformance-tested CDM: the keywords below are
    the ones a conjunction assessment desk reads first. Covariance blocks are
    omitted deliberately -- the screening upstream uses a fixed covariance
    assumption, and emitting fabricated covariance would be worse than
    emitting none.
    """
    lines = [
        "CCSDS_CDM_VERS = 1.0",
        f"CREATION_DATE = {_z(created)}",
        f"ORIGINATOR = {ORIGINATOR}",
        f"MESSAGE_FOR = {secondary.get('operator', 'UNKNOWN OPERATOR')}",
        f"MESSAGE_ID = {message_id}",
        "COMMENT Screening produced by Project O.R.B.I.T. autonomous fleet.",
        "COMMENT Subset of CCSDS CDM keywords; covariance intentionally omitted.",
        *(
            ["COMMENT SIMULATED EXERCISE: OBJECT2 is not a real spacecraft."]
            if secondary.get("simulated_counterparty")
            else []
        ),
        f"TCA = {screening.get('tca_iso', 'UNKNOWN')}",
        f"MISS_DISTANCE = {float(screening.get('miss_distance_km', 0.0)) * 1000.0:.1f} [m]",
        f"COLLISION_PROBABILITY = {float(screening.get('pc', 0.0)):.6e}",
        "COLLISION_PROBABILITY_METHOD = CHAN_1997",
        "",
        "OBJECT = OBJECT1",
        f"OBJECT_DESIGNATOR = {primary.get('norad_id', 'UNKNOWN')}",
        "CATALOG_NAME = SATCAT",
        f"OBJECT_NAME = {primary.get('name', primary.get('id', 'UNKNOWN'))}",
        f"OPERATOR_ORGANIZATION = {primary.get('operator', ORIGINATOR)}",
        "",
        "OBJECT = OBJECT2",
        f"OBJECT_DESIGNATOR = {secondary.get('norad_id', 'UNKNOWN')}",
        "CATALOG_NAME = SATCAT",
        f"OBJECT_NAME = {secondary.get('name', secondary.get('id', 'UNKNOWN'))}",
        f"OPERATOR_ORGANIZATION = {secondary.get('operator', 'UNKNOWN')}",
    ]
    return "\n".join(lines)


def build_coordination_request(
    *,
    primary: dict[str, Any],
    secondary: dict[str, Any],
    screening: dict[str, Any],
    requested_action: str,
    requested_delta_v_mps: float,
    channel: str = "human",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the full coordination artifact for one encounter.

    Args:
        primary: The asset we operate (id, name, norad_id, operator).
        secondary: The counterparty object, same shape.
        screening: Screening result carrying pc, miss_distance_km, tca_iso,
            risk_band.
        requested_action: ``counterparty_manoeuvre`` when we are asking them
            to move, ``we_manoeuvre_notification`` when we are telling them we
            will.
        requested_delta_v_mps: Size of the manoeuvre under discussion.
        channel: ``protocol`` when a machine counterparty answered, ``human``
            when the artifact must be sent out-of-band.
        now: Injectable clock for deterministic tests.

    Returns:
        A dict carrying ``message_id``, ``cdm``, ``message``, ``channel``,
        ``respond_by_utc``, ``request_digest`` and ``awaiting_reply``.
    """
    created = now or datetime.now(timezone.utc)
    deadline = created + timedelta(hours=RESPONSE_DEADLINE_HOURS)

    seed = "|".join(
        [
            str(primary.get("id")),
            str(secondary.get("id")),
            str(screening.get("tca_iso")),
            f"{float(requested_delta_v_mps):.6f}",
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    message_id = f"ORBIT-CDM-{digest[:12].upper()}"

    cdm = build_cdm(
        primary=primary,
        secondary=secondary,
        screening=screening,
        message_id=message_id,
        created=created,
    )

    pc = float(screening.get("pc", 0.0))
    miss_m = float(screening.get("miss_distance_km", 0.0)) * 1000.0
    ask = (
        f"perform an avoidance manoeuvre of approximately {requested_delta_v_mps:.1f} m/s"
        if requested_action == "counterparty_manoeuvre"
        else f"note that we intend to manoeuvre by approximately {requested_delta_v_mps:.1f} m/s"
    )

    simulated = bool(secondary.get("simulated_counterparty"))
    banner = (
        [
            "*** SIMULATED COORDINATION EXERCISE ***",
            "The counterparty below is not a real spacecraft and this message is",
            "not addressed to any real organisation. Generated to exercise the",
            "coordination path, which has no live counterparty in real data.",
            "",
        ]
        if simulated
        else []
    )

    message = "\n".join(
        banner
        + [
            f"To: conjunction assessment desk, {secondary.get('operator', 'operator unknown')}",
            f"From: {ORIGINATOR}",
            f"Subject: [{message_id}] Conjunction coordination - "
            f"{primary.get('id')} / {secondary.get('id')} at {screening.get('tca_iso', 'TCA unknown')}",
            "",
            "Colleagues,",
            "",
            f"Our screening of {primary.get('name', primary.get('id'))} "
            f"(NORAD {primary.get('norad_id')}) against "
            f"{secondary.get('name', secondary.get('id'))} "
            f"(NORAD {secondary.get('norad_id')}) returns a close approach at "
            f"{screening.get('tca_iso', 'TCA unknown')}:",
            "",
            f"  Miss distance          {miss_m:.0f} m",
            f"  Collision probability  {pc:.3e}  (Chan 1997, first-order)",
            f"  Risk band              {screening.get('risk_band', 'UNKNOWN')}",
            "",
            f"We are requesting that you {ask}.",
            "",
            f"Please confirm by {_z(deadline)}Z. If we do not hear from you by then,",
            "we will re-screen and act unilaterally within our own manoeuvre envelope.",
            "",
            "The machine-readable CDM subset for this encounter is included below and",
            "is also retrievable by message ID.",
            "",
            f"Reference: {message_id}",
            "Screened by Project O.R.B.I.T. autonomous conjunction response fleet.",
            "",
            "--- BEGIN CDM ---",
            cdm,
            "--- END CDM ---",
            "",
        ]
    )

    return {
        "message_id": message_id,
        "channel": channel,
        "cdm": cdm,
        "message": message,
        "requested_action": requested_action,
        "requested_delta_v_mps": round(float(requested_delta_v_mps), 4),
        "created_utc": _z(created) + "Z",
        "respond_by_utc": _z(deadline) + "Z",
        "request_digest": digest,
        "simulated_counterparty": simulated,
        "awaiting_reply": channel != "protocol",
    }


__all__ = [
    "ORIGINATOR",
    "RESPONSE_DEADLINE_HOURS",
    "build_cdm",
    "build_coordination_request",
]
