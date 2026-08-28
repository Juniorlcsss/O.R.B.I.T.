"""Project O.R.B.I.T. — constellation optimisation tools (Phase 12).

The Fleet Admiral's arithmetic, extracted so it is testable on its own and
callable either as plain functions or as ADK ``FunctionTool``s. The Admiral
itself stays deterministic and tool-free by design; these exist so the
allocation logic has exactly one implementation rather than one inside the
agent and a second one wherever else it is needed.

Three things live here:

``group_alerts_into_debris_fields``
    A batch is not necessarily one event. Two unrelated fragmentations can
    alert in the same second, and pooling their satellites would let a
    95%-fuel asset threatened by debris A appear to "cover" a conjunction
    with debris B that it is nowhere near. Fuel only couples satellites that
    share a threat, so allocation happens per field, never across the batch.

``get_fleet_fuel_status``
    Reads the persisted fleet state and ranks it. Nothing more — the ranking
    IS the scarce-resource view an operator needs before deciding anything.

``calculate_fuel_equity_allocation``
    The assignment matrix, and the derivation of each dodge's delta-v budget.

On the delta-v budget
---------------------
The budget handed down with each dodge is not a new policy. It is the exact
inverse of the fuel guard in ``geap_sim/model_armor.py``: Armour rejects a
burn whose projected fuel would fall below the strategic reserve, and

    projected = current - dv * FUEL_PERCENT_PER_DV_MPS

so the largest burn Armour can ever approve is

    dv_max = (current - reserve) / FUEL_PERCENT_PER_DV_MPS

capped by the absolute envelope ceiling. Deriving it from the same constants
means the Admiral tells the pipeline the ceiling Armour would otherwise
enforce by rejection, rather than inventing a second, competing limit. If the
two ever disagree, Armour still wins — the budget is advisory context, and
the Admiral has no authority to raise a gate.
"""

from __future__ import annotations

import os
from typing import Any, Final

from geap_sim.memory_bank import (
    FUEL_PERCENT_PER_DV_MPS,
    MemoryBank,
    get_shared_memory_bank,
)
from geap_sim.safety_limits import MAX_ALLOWED_DELTA_V_MPS
STRATEGIC_RESERVE_FUEL_PERCENT: Final[float] = float(
    os.environ.get("ORBIT_ADMIRAL_FUEL_RESERVE_PERCENT", "5.0")
)

DODGE_FUEL_MARGIN_PERCENT: Final[float] = float(
    os.environ.get("ORBIT_ADMIRAL_FUEL_MARGIN_PERCENT", "15.0")
)

DEBRIS_FIELD_WINDOW_SECONDS: Final[float] = float(
    os.environ.get("ORBIT_ADMIRAL_FIELD_WINDOW_SECONDS", "3600")
)

ASSIGN_DODGE: Final[str] = "dodge"
ASSIGN_HOLD: Final[str] = "hold_and_reassess"


def normalise_alert(alert: Any) -> dict[str, Any]:
    """Coerce one inbound alert into a plain dict (Pydantic model or mapping)."""
    if hasattr(alert, "model_dump"):
        return dict(alert.model_dump())
    return dict(alert or {})


def _tca_seconds(alert: dict[str, Any]) -> float | None:
    """Parse whatever TCA the alert carries into epoch seconds, or ``None``."""
    raw = alert.get("tca_utc") or alert.get("tca_iso") or alert.get("tca")
    if not raw:
        return None
    from datetime import datetime

    text = str(raw).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def fuel_budget_mps(fuel_percentage: float) -> float:
    """Largest delta-v this asset can spend without breaching the reserve.

    The inverse of Model Armour's fuel guard, capped by the absolute envelope
    ceiling. See the module docstring for why it is derived rather than chosen.

    Args:
        fuel_percentage: Current propellant remaining, 0–100.

    Returns:
        Delta-v ceiling in m/s, never negative and never above
        ``MAX_ALLOWED_DELTA_V_MPS``.
    """
    spendable = max(0.0, float(fuel_percentage) - STRATEGIC_RESERVE_FUEL_PERCENT)
    if FUEL_PERCENT_PER_DV_MPS <= 0:
        return 0.0
    return round(min(MAX_ALLOWED_DELTA_V_MPS, spendable / FUEL_PERCENT_PER_DV_MPS), 3)


def group_alerts_into_debris_fields(
    alerts: list[Any],
    window_seconds: float = DEBRIS_FIELD_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """Partition a batch into debris fields — one field per threat event.

    Alerts naming the same secondary object within of each
    other are one event.

    Args:
        alerts: Inbound alerts (dicts or ``ConjunctionAlertRequest``-shaped).
        window_seconds: TCA spread within which alerts on one debris object
            are treated as a single field.

    Returns:
        Fields ordered by ``field_id``, each carrying its member alerts.
        Ordering is total and stable so a plan built from it is replayable.
    """
    normalised = [normalise_alert(alert) for alert in alerts]

    buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for alert in normalised:
        debris_id = str(alert.get("debris_id", "")).strip().upper() or "UNKNOWN_DEBRIS"
        tca = _tca_seconds(alert)
        bucket = int(tca // window_seconds) if tca is not None and window_seconds > 0 else 0
        buckets.setdefault((debris_id, bucket), []).append(alert)

    fields: list[dict[str, Any]] = []
    for (debris_id, bucket), members in sorted(buckets.items()):
        fields.append(
            {
                "field_id": f"{debris_id}@{bucket}",
                "debris_id": debris_id,
                "alerts": members,
                "threatened_count": len(members),
            }
        )
    return fields


async def get_fleet_fuel_status(
    sat_ids: list[str],
    memory: MemoryBank | None = None,
) -> dict[str, Any]:
    """Rank the named assets by remaining propellant, healthiest first.

    Args:
        sat_ids: Satellites to read. Duplicates are collapsed; an unknown id
            reads as the Memory Bank's default state rather than raising,
            because a fleet-wide query must not fail on one missing record.
        memory: MemoryBank to read from; defaults to the process singleton.

    Returns:
        ``{"status": "ok", "fleet": [...], "reserve_critical": [...]}`` where
        each fleet entry carries ``sat_id``, ``fuel_percentage``,
        ``thruster_health``, the derived ``fuel_budget_mps`` and whether the
        asset clears the dodge threshold.
    """
    bank = memory if memory is not None else get_shared_memory_bank()

    seen: set[str] = set()
    fleet: list[dict[str, Any]] = []
    for raw in sat_ids:
        sat_id = str(raw or "").strip()
        if not sat_id or sat_id in seen:
            continue
        seen.add(sat_id)
        state = await bank.get_satellite_state(sat_id)
        fuel = round(float(state.get("fuel_percentage", 0.0)), 4)
        fleet.append(
            {
                "sat_id": sat_id,
                "fuel_percentage": fuel,
                "thruster_health": round(float(state.get("thruster_health", 100.0)), 4),
                "fuel_budget_mps": fuel_budget_mps(fuel),
                "clears_reserve": fuel >= STRATEGIC_RESERVE_FUEL_PERCENT,
                "clears_dodge_threshold": fuel >= DODGE_FUEL_MARGIN_PERCENT,
            }
        )

    fleet.sort(key=lambda entry: (-entry["fuel_percentage"], entry["sat_id"]))

    return {
        "status": "ok",
        "fleet": fleet,
        "queried": len(fleet),
        "reserve_critical": [e["sat_id"] for e in fleet if not e["clears_reserve"]],
        "strategic_reserve_percent": STRATEGIC_RESERVE_FUEL_PERCENT,
        "dodge_threshold_percent": DODGE_FUEL_MARGIN_PERCENT,
    }


def _assign(fuel: float, is_field_leader: bool) -> tuple[str, str]:
    """Decide one asset's action and the reason it is being recorded."""
    if fuel < STRATEGIC_RESERVE_FUEL_PERCENT:
        return ASSIGN_HOLD, (
            f"fuel {fuel:.2f}% is below the {STRATEGIC_RESERVE_FUEL_PERCENT:.1f}% "
            "strategic reserve; Model Armour would reject any burn"
        )
    if fuel < DODGE_FUEL_MARGIN_PERCENT:
        return ASSIGN_HOLD, (
            f"fuel {fuel:.2f}% clears the reserve but leaves no margin for a "
            f"subsequent conjunction (dodge threshold {DODGE_FUEL_MARGIN_PERCENT:.1f}%)"
        )
    if is_field_leader:
        return ASSIGN_DODGE, (
            f"fuel {fuel:.2f}% is the healthiest margin threatened by this debris field; "
            "primary dodge responsibility"
        )
    return ASSIGN_DODGE, f"fuel {fuel:.2f}% carries sufficient margin to absorb the burn"


async def calculate_fuel_equity_allocation(
    alerts: list[Any],
    memory: MemoryBank | None = None,
) -> dict[str, Any]:
    """Assign every alert in a batch an action, per debris field, by fuel.

    Pure and deterministic given the fleet's persisted state: the same alerts
    against the same fuel levels always produce the same matrix.

    Args:
        alerts: Inbound alerts, each naming a ``sat_id`` we operate.
        memory: MemoryBank to read fuel from; defaults to the singleton.

    Returns:
        A plan carrying a flat ``assignments`` list (fuel-ranked, each with
        its action, reason and delta-v budget), the ``fields`` it was derived
        from, and the thresholds used — so an operator can audit the split
        without re-deriving it.
    """
    fields = group_alerts_into_debris_fields(alerts)
    bank = memory if memory is not None else get_shared_memory_bank()

    assignments: list[dict[str, Any]] = []
    field_summaries: list[dict[str, Any]] = []

    for field in fields:
        status = await get_fleet_fuel_status(
            [str(a.get("sat_id", "")).strip() for a in field["alerts"]], bank
        )
        by_sat = {entry["sat_id"]: entry for entry in status["fleet"]}

        ordered = [
            alert
            for alert in sorted(
                field["alerts"],
                key=lambda a: (
                    -by_sat.get(str(a.get("sat_id", "")).strip(), {}).get("fuel_percentage", 0.0),
                    str(a.get("sat_id", "")).strip(),
                ),
            )
        ]

        leader_claimed = False
        field_assignments: list[dict[str, Any]] = []
        for alert in ordered:
            sat_id = str(alert.get("sat_id", "")).strip()
            entry = by_sat.get(sat_id, {"fuel_percentage": 0.0, "fuel_budget_mps": 0.0})
            fuel = entry["fuel_percentage"]
            action, reason = _assign(fuel, not leader_claimed)
            is_primary = action == ASSIGN_DODGE and not leader_claimed
            if is_primary:
                leader_claimed = True

            budget = entry["fuel_budget_mps"] if action == ASSIGN_DODGE else 0.0
            record = {
                "sat_id": sat_id,
                "debris_id": str(alert.get("debris_id", "")),
                "field_id": field["field_id"],
                "fuel_percentage": fuel,
                "thruster_health": entry.get("thruster_health", 100.0),
                "assigned_action": action,
                "primary_responder": is_primary,
                "reason": reason,
                "fuel_budget_constraint": {
                    "max_delta_v_mps": budget,
                    "fuel_percentage": fuel,
                    "strategic_reserve_percent": STRATEGIC_RESERVE_FUEL_PERCENT,
                    "derived_from": "model_armor.fuel_guard_inverse",
                },
                "alert": alert,
            }
            field_assignments.append(record)
            assignments.append(record)

        field_summaries.append(
            {
                "field_id": field["field_id"],
                "debris_id": field["debris_id"],
                "threatened_count": field["threatened_count"],
                "primary_responder": next(
                    (a["sat_id"] for a in field_assignments if a["primary_responder"]), None
                ),
                "dodge_count": sum(1 for a in field_assignments if a["assigned_action"] == ASSIGN_DODGE),
                "hold_count": sum(1 for a in field_assignments if a["assigned_action"] == ASSIGN_HOLD),
            }
        )

    assignments.sort(key=lambda a: (-a["fuel_percentage"], a["sat_id"]))

    return {
        "status": "ok",
        "batch_size": len(assignments),
        "assignments": assignments,
        "fields": field_summaries,
        "field_count": len(field_summaries),
        "dodge_count": sum(1 for a in assignments if a["assigned_action"] == ASSIGN_DODGE),
        "hold_count": sum(1 for a in assignments if a["assigned_action"] == ASSIGN_HOLD),
        "strategic_reserve_percent": STRATEGIC_RESERVE_FUEL_PERCENT,
        "dodge_threshold_percent": DODGE_FUEL_MARGIN_PERCENT,
    }


__all__ = [
    "ASSIGN_DODGE",
    "ASSIGN_HOLD",
    "DEBRIS_FIELD_WINDOW_SECONDS",
    "DODGE_FUEL_MARGIN_PERCENT",
    "STRATEGIC_RESERVE_FUEL_PERCENT",
    "calculate_fuel_equity_allocation",
    "fuel_budget_mps",
    "get_fleet_fuel_status",
    "group_alerts_into_debris_fields",
    "normalise_alert",
]
