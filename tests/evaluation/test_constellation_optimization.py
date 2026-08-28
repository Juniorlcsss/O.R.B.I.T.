"""Evaluation — the Fleet Admiral spends the right satellite's fuel.

A fragmentation event does not produce one conjunction; it produces a burst
of them across the constellation at once. Answered independently, each
mission reaches a locally correct decision — *this* satellite should dodge —
and the fleet collectively burns propellant on whichever vehicles happened
to be alerted, including ones that have almost none left.

The Fleet Admiral owns the one resource that couples those missions: fuel.
This test puts three owned assets at deliberately different fuel levels in
front of one batch and asserts the constellation-level behaviour:

* the healthiest asset is ranked first and assigned to dodge;
* an asset below the strategic reserve is held, not flown, because Model
  Armour would refuse its burn anyway — dispatching it would waste a mission
  to reach a foregone rejection;
* an asset that clears the bare reserve but has no margin left for the *next*
  conjunction is also held;
* the plan is deterministic — same fleet state, same plan, byte for byte;
* and, most importantly, the Admiral cannot manufacture action: it never
  assigns anything other than dodge/hold, so it can only ever subtract a
  manoeuvre from what the pipeline would have done, never authorise one.

The single-alert no-op is asserted too. It is the overwhelmingly common
path, and a constellation optimiser that quietly changes single-mission
behaviour would be a regression dressed as a feature.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.admiral import (  # noqa: E402
    ASSIGN_DODGE,
    ASSIGN_HOLD,
    DODGE_FUEL_MARGIN_PERCENT,
    STRATEGIC_RESERVE_FUEL_PERCENT,
    build_constellation_plan,
)
from geap_sim.memory_bank import FUEL_PERCENT_PER_DV_MPS, estimate_fuel_after_burn  # noqa: E402
from geap_sim.safety_limits import MAX_ALLOWED_DELTA_V_MPS  # noqa: E402
from tests.evaluation.harness import EvaluationHarness  # noqa: E402

NAME = "constellation_optimization"
DESCRIPTION = "Fleet Admiral ranks a conjunction burst by fuel: healthiest dodges, reserve-critical holds"

HEALTHY = "SIM_FLEET_ALPHA"
MARGINAL = "SIM_FLEET_BRAVO"
CRITICAL = "SIM_FLEET_CHARLIE"


async def execute(harness: EvaluationHarness):
    checks = []
    bank = harness.memory_bank

    await bank.update_satellite_state(HEALTHY, 0.0, 92.0)
    await bank.update_satellite_state(MARGINAL, 0.0, (STRATEGIC_RESERVE_FUEL_PERCENT + DODGE_FUEL_MARGIN_PERCENT) / 2.0)
    await bank.update_satellite_state(CRITICAL, 0.0, STRATEGIC_RESERVE_FUEL_PERCENT - 2.0)

    alerts = [
        {"sat_id": CRITICAL, "debris_id": "FENGYUN_1C_DEB"},
        {"sat_id": HEALTHY, "debris_id": "FENGYUN_1C_DEB"},
        {"sat_id": MARGINAL, "debris_id": "FENGYUN_1C_DEB"},
    ]
    plan = await build_constellation_plan(alerts, bank)
    by_sat = {a["sat_id"]: a for a in plan["assignments"]}
    order = [a["sat_id"] for a in plan["assignments"]]

    checks.append(harness.require(
        "all_assets_assigned", set(by_sat) == {HEALTHY, MARGINAL, CRITICAL}, str(order),
    ))

    # Ranked by fuel, highest first — the ordering IS the optimisation.
    checks.append(harness.check(
        "ranked_by_fuel_descending",
        order == [HEALTHY, MARGINAL, CRITICAL],
        f"order={order}",
    ))

    checks.append(harness.check(
        "healthiest_asset_assigned_dodge",
        by_sat[HEALTHY]["assigned_action"] == ASSIGN_DODGE,
        f"{HEALTHY} -> {by_sat[HEALTHY]['assigned_action']} at {by_sat[HEALTHY]['fuel_percentage']}%",
    ))

    # Below the strategic reserve: Model Armour would reject the burn, so
    # flying the mission at all would only spend time reaching a known no.
    checks.append(harness.check(
        "reserve_critical_asset_held",
        by_sat[CRITICAL]["assigned_action"] == ASSIGN_HOLD,
        f"{CRITICAL} -> {by_sat[CRITICAL]['assigned_action']} at {by_sat[CRITICAL]['fuel_percentage']}%",
    ))
    checks.append(harness.check(
        "hold_reason_cites_reserve",
        "reserve" in by_sat[CRITICAL]["reason"].lower(),
        by_sat[CRITICAL]["reason"],
    ))

    # Clears the reserve but keeps nothing back for the next conjunction.
    checks.append(harness.check(
        "no_margin_asset_held",
        by_sat[MARGINAL]["assigned_action"] == ASSIGN_HOLD,
        f"{MARGINAL} -> {by_sat[MARGINAL]['assigned_action']} at {by_sat[MARGINAL]['fuel_percentage']}%",
    ))

    checks.append(harness.check(
        "counts_match_assignments",
        plan["dodge_count"] == 1 and plan["hold_count"] == 2 and plan["batch_size"] == 3,
        f"batch={plan['batch_size']} dodge={plan['dodge_count']} hold={plan['hold_count']}",
    ))

    # {dodge, hold} would be authority it was never granted.
    actions = {a["assigned_action"] for a in plan["assignments"]}
    checks.append(harness.check(
        "no_action_outside_dodge_or_hold",
        actions <= {ASSIGN_DODGE, ASSIGN_HOLD},
        str(sorted(actions)),
    ))

    # Determinism: an allocation decision over propellant must be replayable.
    replay = await build_constellation_plan(alerts, bank)
    checks.append(harness.check(
        "plan_is_deterministic",
        [(a["sat_id"], a["assigned_action"]) for a in replay["assignments"]]
        == [(a["sat_id"], a["assigned_action"]) for a in plan["assignments"]],
        "replayed plan matches",
    ))

    solo = await build_constellation_plan([{"sat_id": HEALTHY, "debris_id": "FENGYUN_1C_DEB"}], bank)
    checks.append(harness.check(
        "single_alert_is_a_noop_dodge",
        solo["batch_size"] == 1
        and solo["dodge_count"] == 1
        and solo["assignments"][0]["sat_id"] == HEALTHY,
        f"batch={solo['batch_size']} dodge={solo['dodge_count']}",
    ))

    # ---- debris-field grouping ---------------------------------------------
    checks.append(harness.check(
        "one_debris_object_is_one_field",
        plan["field_count"] == 1 and plan["fields"][0]["threatened_count"] == 3,
        f"fields={plan['field_count']} threatened={plan['fields'][0]['threatened_count']}",
    ))
    checks.append(harness.check(
        "field_names_its_primary_responder",
        plan["fields"][0]["primary_responder"] == HEALTHY,
        str(plan["fields"][0]["primary_responder"]),
    ))
    checks.append(harness.check(
        "only_one_primary_per_field",
        sum(1 for a in plan["assignments"] if a["primary_responder"]) == 1,
        str([a["sat_id"] for a in plan["assignments"] if a["primary_responder"]]),
    ))


    SECOND = "SIM_FLEET_DELTA"
    await bank.update_satellite_state(SECOND, 0.0, 88.0)
    split = await build_constellation_plan(
        [
            {"sat_id": HEALTHY, "debris_id": "FENGYUN_1C_DEB"},
            {"sat_id": SECOND, "debris_id": "COSMOS_2251_DEB"},
        ],
        bank,
    )
    primaries = {f["debris_id"]: f["primary_responder"] for f in split["fields"]}
    checks.append(harness.check(
        "separate_debris_events_are_separate_fields",
        split["field_count"] == 2,
        f"fields={split['field_count']} -> {sorted(primaries)}",
    ))
    checks.append(harness.check(
        "each_field_elects_its_own_responder",
        primaries.get("FENGYUN_1C_DEB") == HEALTHY and primaries.get("COSMOS_2251_DEB") == SECOND,
        str(primaries),
    ))

    # ---- fuel budget constraint --------------------------------------------
    healthy_alert = by_sat[HEALTHY]["alert"]
    checks.append(harness.check(
        "budget_injected_into_dodge_alert",
        "fuel_budget_constraint" in healthy_alert,
        str(sorted(healthy_alert)),
    ))

    # A held asset is not flown, so it must not carry a spend authorisation.
    checks.append(harness.check(
        "held_asset_carries_no_budget",
        "fuel_budget_constraint" not in by_sat[CRITICAL]["alert"],
        str(sorted(by_sat[CRITICAL]["alert"])),
    ))

    # The budget is the inverse of Model Armour's fuel guard, capped by the
    # envelope ceiling — derived from the same constants, not chosen.
    lean = "SIM_FLEET_ECHO"
    await bank.update_satellite_state(lean, 0.0, 20.0)
    lean_plan = await build_constellation_plan(
        [{"sat_id": lean, "debris_id": "FENGYUN_1C_DEB"}, {"sat_id": HEALTHY, "debris_id": "FENGYUN_1C_DEB"}],
        bank,
    )
    lean_entry = next(a for a in lean_plan["assignments"] if a["sat_id"] == lean)
    expected_dv = min(
        MAX_ALLOWED_DELTA_V_MPS,
        (20.0 - STRATEGIC_RESERVE_FUEL_PERCENT) / FUEL_PERCENT_PER_DV_MPS,
    )
    checks.append(harness.check(
        "budget_matches_armour_fuel_guard_inverse",
        abs(lean_entry["fuel_budget_constraint"]["max_delta_v_mps"] - expected_dv) < 0.01,
        f"{lean_entry['fuel_budget_constraint']['max_delta_v_mps']} m/s vs expected {expected_dv:.3f}",
    ))

    projected = estimate_fuel_after_burn(20.0, lean_entry["fuel_budget_constraint"]["max_delta_v_mps"])
    checks.append(harness.check(
        "spending_the_full_budget_respects_the_reserve",
        projected >= STRATEGIC_RESERVE_FUEL_PERCENT - 1e-6,
        f"projected {projected:.3f}% vs reserve {STRATEGIC_RESERVE_FUEL_PERCENT:.1f}%",
    ))

    return checks


if __name__ == "__main__":
    results = asyncio.run(execute(EvaluationHarness()))
    for r in results:
        print(("PASS" if r.passed else "FAIL"), "-", r.name, ("| " + r.detail if r.detail else ""))
    sys.exit(0 if all(r.passed for r in results) else 1)
