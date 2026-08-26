"""Project O.R.B.I.T. — the WatchCommander (long-running conjunction watches).

Architectural role
------------------
The FleetCommanderPipeline reacts to one alert in one invocation. Real
conjunction assessment is different: many encounters need **multi-day
monitoring** while tracking data improves. The WatchCommander is a
long-running supervisory agent that owns that timeline:

* ``WATCH``   — start monitoring a satellite/debris pair at a fixed cadence.
* re-screen   — every N hours (``interval_hours``, 1–72), in-process SGP4.
* escalate    — risk rises to the escalation band (default HIGH) → the watch
  parks in ``AWAITING_HUMAN_APPROVAL`` and requires explicit human
  confirmation *before* a full fleet mission is triggered.
* decline     — risk falls to LOW → the watch auto-closes.
* remember    — all state persists through the MemoryBank (Firestore in
  production, in-process for laptop demos), so watches survive restarts.

Long-running workflow properties (webinar checklist)
----------------------------------------------------
**Idempotency**     one canonical ``watch_id`` per satellite/debris pair;
                    duplicate WATCH commands return the existing watch and
                    are audited, never double-scheduled.
**Crash recovery**  ``resume_active_watches()`` runs at startup, reloads
                    every open watch from persistent storage and audits
                    ``WATCH_RESUMED_ON_STARTUP``; overdue checks execute on
                    the first supervisor tick.
**Human approval**  escalation to the fleet is gated behind an explicit
                    ``approve_escalation`` call — autonomy never silently
                    converts a watch into a manoeuvre.

The supervisor itself is a single asyncio task started by the API lifespan;
because state lives outside the process, restarting the service resumes
exactly where it left off.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Final

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict

from geap_sim.memory_bank import MemoryBank, get_shared_memory_bank, safe_document_id
from geap_sim.observability import audit_logger
from tools.space_tools import screen_conjunction

AGENT_NAME: Final[str] = "watch_commander"

DEFAULT_INTERVAL_HOURS: Final[float] = float(os.environ.get("ORBIT_WATCH_DEFAULT_INTERVAL_HOURS", "6"))
ESCALATE_BAND: Final[str] = os.environ.get("ORBIT_WATCH_ESCALATE_BAND", "HIGH").upper()
SUPERVISOR_POLL_SECONDS: Final[float] = float(os.environ.get("ORBIT_WATCH_SUPERVISOR_POLL_SECONDS", "60"))

#: Risk-band ordering used for threshold comparisons.
_BAND_RANK: Final[dict[str, int]] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Watch lifecycle states.
STATUS_ACTIVE: Final[str] = "ACTIVE"
STATUS_AWAITING_HUMAN_APPROVAL: Final[str] = "AWAITING_HUMAN_APPROVAL"
STATUS_CLOSED_AUTO: Final[str] = "CLOSED_AUTO_RISK_DECLINED"
STATUS_CLOSED_DENIED: Final[str] = "CLOSED_ESCALATION_DENIED"
STATUS_CLOSED_MANUAL: Final[str] = "CLOSED_MANUAL"

_OPEN_STATES: Final[tuple[str, ...]] = (STATUS_ACTIVE, STATUS_AWAITING_HUMAN_APPROVAL)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class WatcherAgent(BaseAgent):
    """Persistent conjunction-watch supervisor (see module docstring).

    ``escalation_handler`` is injected by the API layer: an async callable
    receiving the watch document and returning a mission result dict. The
    watcher never constructs a pipeline itself — it only decides WHEN a
    mission is warranted and holds it behind human approval.
    """

    escalation_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    default_interval_hours: float = DEFAULT_INTERVAL_HOURS
    escalate_band: str = ESCALATE_BAND

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- watch lifecycle -------------------------------------------------------

    def _bank(self) -> MemoryBank:
        return get_shared_memory_bank()

    async def start_watch(
        self,
        sat_id: str,
        debris_id: str,
        interval_hours: float | None = None,
        escalate_band: str | None = None,
    ) -> dict[str, Any]:
        """Begin (or idempotently re-confirm) monitoring of one pair.

        Returns the watch document plus ``created`` (False when an open
        watch already existed for this pair — the duplicate command is
        audited and ignored rather than double-scheduled).
        """
        sat_key, debris_key = sat_id.strip().upper(), debris_id.strip().upper()
        interval = min(72.0, max(1.0, float(interval_hours or self.default_interval_hours)))
        band = (escalate_band or self.escalate_band).strip().upper()

        # Canonical ID == built-in idempotency key.
        watch_id = safe_document_id(f"WATCH-{sat_key}-X-{debris_key}")
        existing = await self._bank().get_watch(watch_id)
        if existing and existing.get("status") in _OPEN_STATES:
            audit_logger.log_event(
                trace_id=str(existing.get("watch_trace_id", "watch")),
                agent_name=self.name,
                event_type="DUPLICATE_WATCH_IGNORED",
                payload={"watch_id": watch_id, "existing_status": existing.get("status")},
                status="IGNORED",
            )
            return {**existing, "created": False}

        # Reject pairs the catalogue cannot screen before persisting anything.
        probe = screen_conjunction(sat_key, debris_key)
        if probe.get("status") != "ok":
            raise ValueError(f"Cannot watch pair: {probe.get('error_code')} — {probe.get('message', '')[:120]}")

        now = _utc_now()
        watch: dict[str, Any] = {
            "sat_id": sat_key,
            "debris_id": debris_key,
            "status": STATUS_ACTIVE,
            "interval_hours": interval,
            "escalate_band": band,
            "created_utc": _iso(now),
            "created_by": "orbit.api",
            "watch_trace_id": uuid.uuid4().hex,
            "check_count": 0,
            "last_checked_utc": None,
            "next_check_due_utc": _iso(now + timedelta(hours=interval)),
            "last_risk_band": probe.get("risk_level"),
            "last_pc": probe.get("probability_of_collision"),
            "last_miss_distance_km": probe.get("miss_distance_km"),
            "mission_trace_ids": [],
        }
        stored = await self._bank().upsert_watch(watch_id, watch)
        audit_logger.log_event(
            trace_id=str(stored["watch_trace_id"]),
            agent_name=self.name,
            event_type="WATCH_CREATED",
            payload={
                "watch_id": watch_id,
                "sat_id": sat_key,
                "debris_id": debris_key,
                "interval_hours": interval,
                "escalate_band": band,
                "initial_risk_band": probe.get("risk_level"),
                "initial_pc": probe.get("probability_of_collision"),
            },
            status="OK",
        )
        return {**stored, "created": True}

    async def get_watch(self, watch_id: str) -> dict[str, Any] | None:
        """One watch document by ID."""
        return await self._bank().get_watch(watch_id)

    async def list_watches(self, status: str | None = None) -> list[dict[str, Any]]:
        """All watches, optionally filtered by status."""
        return await self._bank().list_watches(status)

    async def close_watch(self, watch_id: str, reason: str = "operator request") -> dict[str, Any] | None:
        """Manually close one watch."""
        watch = await self._bank().get_watch(watch_id)
        if watch is None:
            return None
        watch["status"] = STATUS_CLOSED_MANUAL
        watch["closed_reason"] = reason[:200]
        watch["closed_utc"] = _iso(_utc_now())
        stored = await self._bank().upsert_watch(watch_id, watch)
        audit_logger.log_event(
            trace_id=str(watch.get("watch_trace_id", "watch")),
            agent_name=self.name,
            event_type="WATCH_CLOSED_MANUAL",
            payload={"watch_id": watch_id, "reason": reason[:200]},
            status="OK",
        )
        return stored

    async def approve_escalation(self, watch_id: str, approved_by: str, approved: bool = True) -> dict[str, Any] | None:
        """Human gate between a HIGH-risk watch and a full fleet mission.

        Only meaningful while the watch is ``AWAITING_HUMAN_APPROVAL``.
        On approval the injected ``escalation_handler`` runs the complete
        FleetCommanderPipeline for the pair; the resulting trace ID is
        recorded on the watch and monitoring continues so operators can see
        whether the manoeuvre resolved the encounter.
        """
        watch = await self._bank().get_watch(watch_id)
        if watch is None:
            return None
        if watch.get("status") != STATUS_AWAITING_HUMAN_APPROVAL:
            return {**watch, "error": "not_awaiting_approval"}

        if not approved:
            watch["status"] = STATUS_CLOSED_DENIED
            watch["closed_reason"] = f"escalation denied by {approved_by}"
            watch["closed_utc"] = _iso(_utc_now())
            stored = await self._bank().upsert_watch(watch_id, watch)
            audit_logger.log_event(
                trace_id=str(watch.get("watch_trace_id", "watch")),
                agent_name=self.name,
                event_type="WATCH_ESCALATION_DENIED",
                payload={"watch_id": watch_id, "approved_by": approved_by},
                status="REJECTED",
            )
            return stored

        audit_logger.log_event(
            trace_id=str(watch.get("watch_trace_id", "watch")),
            agent_name=self.name,
            event_type="WATCH_ESCALATION_APPROVED",
            payload={"watch_id": watch_id, "approved_by": approved_by},
            status="APPROVED",
        )

        mission_result: dict[str, Any]
        if self.escalation_handler is not None:
            mission_result = await self.escalation_handler(dict(watch))
        else:
            mission_result = {"status": "NO_HANDLER_CONFIGURED", "note": "escalation handler not wired in this process"}

        missions = list(watch.get("mission_trace_ids") or [])
        if mission_result.get("trace_id"):
            missions.append(str(mission_result["trace_id"]))
        watch.update(
            {
                "status": STATUS_ACTIVE,
                "mission_trace_ids": missions[-10:],
                "last_mission_status": mission_result.get("status"),
                "approved_by": approved_by,
            }
        )
        stored = await self._bank().upsert_watch(watch_id, watch)
        audit_logger.log_event(
            trace_id=str(mission_result.get("trace_id") or watch.get("watch_trace_id", "watch")),
            agent_name=self.name,
            event_type="MISSION_TRIGGERED_FROM_WATCH",
            payload={"watch_id": watch_id, "mission_status": mission_result.get("status")},
            status=str(mission_result.get("status", "TRIGGERED")),
        )
        return stored

    # -- periodic checking -----------------------------------------------------

    async def check_watch(self, watch: dict[str, Any]) -> dict[str, Any]:
        """Re-screen one pair once and apply the escalation/decline policy."""
        watch_id = str(watch["watch_id"])
        screened = screen_conjunction(str(watch["sat_id"]), str(watch["debris_id"]))
        now = _utc_now()
        watch["last_checked_utc"] = _iso(now)
        watch["check_count"] = int(watch.get("check_count", 0)) + 1
        watch["next_check_due_utc"] = _iso(now + timedelta(hours=float(watch.get("interval_hours", self.default_interval_hours))))

        if screened.get("status") != "ok":
            audit_logger.log_event(
                trace_id=str(watch.get("watch_trace_id", "watch")),
                agent_name=self.name,
                event_type="WATCH_CHECK_FAILED",
                payload={"watch_id": watch_id, "error_code": screened.get("error_code")},
                status="FAILED",
            )
            return await self._bank().upsert_watch(watch_id, watch)

        band = str(screened.get("risk_level"))
        pc = screened.get("probability_of_collision")
        watch.update(
            {
                "last_risk_band": band,
                "last_pc": pc,
                "last_miss_distance_km": screened.get("miss_distance_km"),
                "last_tca_iso": screened.get("tca_utc"),
            }
        )

        escalated_rank = _BAND_RANK.get(str(watch.get("escalate_band", self.escalate_band)), 2)
        if _BAND_RANK.get(band, 0) >= escalated_rank and watch.get("status") != STATUS_AWAITING_HUMAN_APPROVAL:
            watch["status"] = STATUS_AWAITING_HUMAN_APPROVAL
            audit_logger.log_event(
                trace_id=str(watch.get("watch_trace_id", "watch")),
                agent_name=self.name,
                event_type="WATCH_ESCALATION_REQUESTED",
                payload={
                    "watch_id": watch_id,
                    "risk_band": band,
                    "pc": pc,
                    "tca_iso": screened.get("tca_utc"),
                    "human_approval_required": True,
                },
                status="AWAITING_APPROVAL",
            )
        elif band == "LOW" and watch.get("status") == STATUS_ACTIVE:
            watch["status"] = STATUS_CLOSED_AUTO
            watch["closed_reason"] = "risk declined below watch threshold"
            watch["closed_utc"] = _iso(now)

        stored = await self._bank().upsert_watch(watch_id, watch)
        audit_logger.log_event(
            trace_id=str(watch.get("watch_trace_id", "watch")),
            agent_name=self.name,
            event_type="WATCH_CHECK_COMPLETED",
            payload={
                "watch_id": watch_id,
                "check_number": watch["check_count"],
                "risk_band": band,
                "pc": pc,
                "miss_distance_km": screened.get("miss_distance_km"),
                "status": stored.get("status"),
            },
            status="OK",
        )
        return stored

    async def check_due_watches(self, now: datetime | None = None) -> int:
        """Run every open watch whose next-check time has passed."""
        moment = (now or _utc_now()).isoformat()
        checked = 0
        for watch in await self.list_watches():
            if watch.get("status") not in _OPEN_STATES:
                continue
            if str(watch.get("next_check_due_utc") or "") <= moment:
                await self.check_watch(watch)
                checked += 1
        return checked

    async def resume_active_watches(self) -> int:
        """Startup crash recovery: audit and reload every open watch.

        State was persisted on every transition, so resuming is purely a
        matter of reloading documents — overdue checks fire on the first
        supervisor poll because their ``next_check_due_utc`` has passed.
        """
        resumed = 0
        for watch in await self.list_watches():
            if watch.get("status") in _OPEN_STATES:
                resumed += 1
                audit_logger.log_event(
                    trace_id=str(watch.get("watch_trace_id", "watch")),
                    agent_name=self.name,
                    event_type="WATCH_RESUMED_ON_STARTUP",
                    payload={
                        "watch_id": watch.get("watch_id"),
                        "pair": f"{watch.get('sat_id')}×{watch.get('debris_id')}",
                        "checks_so_far": watch.get("check_count", 0),
                        "overdue": str(watch.get("next_check_due_utc") or "") <= _utc_now().isoformat(),
                    },
                    status="RESUMED",
                )
        if resumed:
            audit_logger.log_event(
                trace_id="startup",
                agent_name=self.name,
                event_type="WATCH_RESUME_SUMMARY",
                payload={"resumed": resumed},
                status="OK",
            )
        return resumed

    async def supervisor_loop(self, poll_seconds: float = SUPERVISOR_POLL_SECONDS) -> None:
        """The long-running heartbeat: check due watches until cancelled."""
        audit_logger.log_event(
            trace_id="startup",
            agent_name=self.name,
            event_type="WATCH_SUPERVISOR_STARTED",
            payload={"poll_seconds": poll_seconds},
            status="OK",
        )
        try:
            while True:
                try:
                    await self.check_due_watches()
                except Exception as exc:  # noqa: BLE001 — the loop must outlive bugs
                    audit_logger.log_event(
                        trace_id="watch",
                        agent_name=self.name,
                        event_type="WATCH_SUPERVISOR_ERROR",
                        payload={"error_type": type(exc).__name__, "error": str(exc)[:200]},
                        status="FAILED",
                    )
                await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            audit_logger.log_event(
                trace_id="shutdown",
                agent_name=self.name,
                event_type="WATCH_SUPERVISOR_STOPPED",
                payload={},
                status="OK",
            )
            raise

    # -- ADK plumbing ------------------------------------------------------------

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Direct ADK invocation reports the watch board (diagnostic use)."""
        open_watches = [w for w in await self.list_watches() if w.get("status") in _OPEN_STATES]
        summary = (
            f"WatchCommander online. {len(open_watches)} open watch(es): "
            + "; ".join(
                f"{w.get('sat_id')}×{w.get('debris_id')} [{w.get('status')}, {w.get('last_risk_band')}]" for w in open_watches
            )
            or "no watches active"
        )
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=summary)]))


watcher_agent = WatcherAgent(
    name=AGENT_NAME,
    description=(
        "Long-running conjunction watch supervisor. Persists multi-day "
        "monitoring state across restarts, re-screens pairs on a fixed "
        "cadence, escalates rising risk behind human approval and closes "
        "declined encounters automatically."
    ),
)

__all__ = [
    "AGENT_NAME",
    "DEFAULT_INTERVAL_HOURS",
    "ESCALATE_BAND",
    "STATUS_ACTIVE",
    "STATUS_AWAITING_HUMAN_APPROVAL",
    "WatcherAgent",
    "watcher_agent",
]
