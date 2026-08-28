"""Project O.R.B.I.T. — FleetAdmiralAgent (constellation-level optimiser).

Why a layer above the Fleet Commander
-------------------------------------
The FleetCommander answers one question extremely well: *given this
conjunction, what should this satellite do?* It is deliberately blind to the
rest of the constellation, because a mission pipeline that re-reasons about
the whole fleet on every alert cannot be made deterministic or testable.

But a real operator does not receive one alert. A debris-generating event —
a fragmentation, an ASAT test, a Starlink shell crossing — produces a *burst*
of conjunctions across many owned assets at once, and the right response is
not the union of N independently-optimal responses. Fuel is the constraint
that couples them: burning 12 m/s on a satellite sitting at 8% fuel to clear
a conjunction that a 90%-fuel sibling could also clear spends the wrong
vehicle's remaining life.

The Fleet Admiral is the constellation-level control plane that owns exactly
that coupling, and nothing else:

* it ranks the owned assets in the batch by *fuel remaining*, the only
  resource that is genuinely scarce and genuinely shared;
* assets with enough margin are assigned ``dodge`` — they run the full
  FleetCommander pipeline, gates and all;
* assets below the strategic reserve are assigned ``hold_and_reassess`` —
  they are re-screened rather than burned, and the decision is recorded with
  its reason.

Deliberate design constraints
-----------------------------
**The Admiral is deterministic.** No LLM. Fuel triage is arithmetic, and an
arithmetic decision that allocates propellant across a fleet should not be
re-litigated by a sampler on every invocation. This mirrors the
FleetCommander's own thesis one level up.

**The Admiral cannot weaken a safety gate.** It decides *which* satellites
enter the mission pipeline; it has no authority over what happens once they
are in it. A satellite the Admiral assigns to dodge still faces the
SafetyOfficer, the Model Armour sweep and the fuel guard exactly as if the
alert had arrived alone. ``hold_and_reassess`` is strictly *less* action
than the pipeline would have taken, never more — so the Admiral can only
ever subtract manoeuvres, never authorise one.

**A single alert is a no-op.** One-element batches pass straight through to
the FleetCommander with no plan, no reordering and no added state. There is
no constellation to optimise with one satellite in it, and pretending
otherwise would put an untested branch in front of the most-exercised path
in the system.

Execution model
---------------
The Admiral produces the *plan*; each assigned mission then runs in its own
ADK session (see ``execute_constellation_batch`` in ``app.py``). That
isolation is deliberate rather than incidental: the FleetCommander pipeline
writes screening, negotiation and verdict payloads to fixed session-state
keys, so running several missions through one session would let mission N+1
read mission N's screening. Separate sessions make each mission's audit
trail independently replayable by trace ID, which is the property the whole
observability story rests on.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Final

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict

from agents.orchestrator import FleetCommanderPipeline, fleet_commander_agent
from geap_sim.memory_bank import MemoryBank, get_shared_memory_bank
from geap_sim.observability import audit_logger
from tools.optimization_tools import (
    ASSIGN_DODGE,
    ASSIGN_HOLD,
    DODGE_FUEL_MARGIN_PERCENT,
    STRATEGIC_RESERVE_FUEL_PERCENT,
    calculate_fuel_equity_allocation,
    normalise_alert as _normalise_alert,
)

AGENT_NAME: Final[str] = "fleet_admiral"

#: Session-state key carrying the inbound batch of alerts (set by the API).
STATE_CONSTELLATION_BATCH: Final[str] = "orbit_constellation_batch"
#: Session-state key carrying the Admiral's assignment plan.
STATE_CONSTELLATION_PLAN: Final[str] = "orbit_constellation_plan"

async def build_constellation_plan(
    alerts: list[Any],
    memory: MemoryBank | None = None,
) -> dict[str, Any]:
    """Group a batch into debris fields, rank each by fuel, assign an action.

    Thin wrapper over ``tools.optimization_tools.calculate_fuel_equity_allocation``
    that additionally injects each assignment's delta-v budget into the alert
    payload handed down to the FleetCommander. The arithmetic lives in the
    tools module so there is exactly one implementation of it — an allocation
    policy that existed twice would drift, and the copy the tests exercised
    would not be the copy that flew.

    Pure and deterministic given the fleet's persisted state: same alerts and
    same fuel levels always produce the same plan, which is what makes the
    constellation evaluation reproducible.

    Args:
        alerts: Inbound alerts (dicts or ``ConjunctionAlertRequest``-shaped
            models), each naming a ``sat_id`` we operate.
        memory: MemoryBank to read fuel from; defaults to the process
            singleton.

    Returns:
        A plan carrying ``assignments`` (ordered highest-fuel first, each with
        the alert, its fuel reading, its assigned action, the reason and the
        ``fuel_budget_constraint``), the ``fields`` the batch resolved into,
        and the thresholds used — so an operator can audit the split without
        re-deriving it.
    """
    bank = memory if memory is not None else get_shared_memory_bank()
    plan = await calculate_fuel_equity_allocation(alerts, bank)

    # Hand the budget down with the alert itself. The FleetCommander reads a
    # plain alert dict, so a constraint that stays only in the plan would
    # never reach the agents that plan the burn — they would size a manoeuvre
    # freely and meet the ceiling as a rejection from Model Armour instead.
    for assignment in plan["assignments"]:
        if assignment["assigned_action"] != ASSIGN_DODGE:
            continue
        alert = dict(assignment["alert"])
        alert["fuel_budget_constraint"] = assignment["fuel_budget_constraint"]
        assignment["alert"] = alert

    return plan


class FleetAdmiralAgent(BaseAgent):
    """Constellation-level optimiser sitting above the FleetCommander.

    Holds the FleetCommander as its sole sub-agent. On a single alert it is a
    transparent pass-through; on a batch it computes the fuel-ranked
    assignment plan, publishes it to session state and leaves execution of
    the assigned missions to the caller, one isolated session each.
    """

    fleet_commander: FleetCommanderPipeline

    #: GEAP MemoryBank supplying fleet fuel levels (injectable for tests).
    constellation_memory: MemoryBank | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _status_event(self, text: str) -> Event:
        return Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        batch: list[Any] = list(ctx.session.state.get(STATE_CONSTELLATION_BATCH) or [])

        # Single-alert missions are the overwhelmingly common case and the
        # most heavily exercised path in the system. They must behave exactly
        # as they did before the Admiral existed, so there is nothing here to
        # step around: delegate and get out of the way.
        if len(batch) < 2:
            async for event in self.fleet_commander.run_async(ctx):
                yield event
            return

        plan = await build_constellation_plan(batch, self.constellation_memory)
        ctx.session.state[STATE_CONSTELLATION_PLAN] = plan

        audit_logger.log_event(
            trace_id=str(ctx.session.state.get("orbit_trace_id") or "constellation"),
            agent_name=self.name,
            event_type="CONSTELLATION_PLAN_ASSIGNED",
            payload={
                "batch_size": plan["batch_size"],
                "field_count": plan["field_count"],
                "fields": plan["fields"],
                "dodge_count": plan["dodge_count"],
                "hold_count": plan["hold_count"],
                "assignments": [
                    {k: v for k, v in a.items() if k != "alert"} for a in plan["assignments"]
                ],
            },
            status="PLANNED",
        )

        yield self._status_event(
            f"Fleet Admiral: {plan['batch_size']} concurrent conjunctions across "
            f"{plan['field_count']} debris field(s) triaged by fuel — "
            f"{plan['dodge_count']} assigned to dodge, {plan['hold_count']} held for reassessment."
        )


fleet_admiral_agent = FleetAdmiralAgent(
    name=AGENT_NAME,
    description=(
        "Fleet Admiral for Project O.R.B.I.T. Constellation-level control "
        "plane: on a burst of simultaneous conjunctions it ranks owned assets "
        "by remaining fuel, dispatches the healthiest to dodge and holds "
        "reserve-critical assets for reassessment. Deterministic, tool-free, "
        "and structurally incapable of authorising a manoeuvre — it decides "
        "which satellites enter the mission pipeline, never what the pipeline "
        "is allowed to do once they are in it."
    ),
    fleet_commander=fleet_commander_agent,
    constellation_memory=get_shared_memory_bank(),
    sub_agents=[fleet_commander_agent],
)


__all__ = [
    "AGENT_NAME",
    "ASSIGN_DODGE",
    "ASSIGN_HOLD",
    "DODGE_FUEL_MARGIN_PERCENT",
    "FleetAdmiralAgent",
    "STATE_CONSTELLATION_BATCH",
    "STATE_CONSTELLATION_PLAN",
    "STRATEGIC_RESERVE_FUEL_PERCENT",
    "build_constellation_plan",
    "calculate_fuel_equity_allocation",
    "fleet_admiral_agent",
]
