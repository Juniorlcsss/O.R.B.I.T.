"""GEAP simulation — OpenTelemetry-style audit logging.

Simulates the managed audit-trail service of a Gemini Enterprise Agent
Platform deployment. Every governance decision (Model Armour verdicts,
circuit-breaker trips, mission status transitions) MUST flow through
``AuditLogger.log_event``, producing one structured JSON line per event on
stdout. Cloud Run captures stdout into Cloud Logging, where the ``severity``
field is honoured natively and the JSON body is preserved as the structured
payload — so local dev and production share an identical, greppable trail.

Correlation follows OTel conventions: all events belonging to one mission
share a single ``trace_id`` (a 32-hex UUID), emitted by the FleetCommander
at pipeline start.
"""

from __future__ import annotations

import collections
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Final, Mapping, TextIO

#: Ordered severity rules: first matching keyword in ``status`` wins.
#: Keywords are matched case-insensitively against the event status string.
_SEVERITY_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("TRIPPED", "ERROR"),
    ("CRITICAL", "ALERT"),
    ("DEGRADED", "ALERT"),
    ("REJECTED", "WARNING"),
    ("BLOCKED", "WARNING"),
    ("STANDOFF", "NOTICE"),
    ("DISPATCH", "NOTICE"),
    ("AUTHORIZED", "INFO"),
    ("APPROVED", "INFO"),
    ("EXECUTED", "NOTICE"),
)

_DEFAULT_SEVERITY: Final[str] = "INFO"
_SERVICE_NAME: Final[str] = "orbit-geap-sim"


def _map_severity(status: str) -> str:
    """Map an event status string onto a Cloud Logging severity level."""
    upper = str(status).upper()
    for keyword, severity in _SEVERITY_RULES:
        if keyword in upper:
            return severity
    return _DEFAULT_SEVERITY


class AuditLogger:
    """Structured JSON-lines audit logger (Cloud Logging compatible).

    One line per event; safe to call from multiple threads. The record shape
    mirrors OpenTelemetry log conventions (``trace_id`` correlation plus a
    ``severity`` field Cloud Logging understands natively).

    A bounded in-memory ring buffer keeps recent records so the observability
    API (``/api/armor_report/{trace_id}``) can replay a mission's reasoning
    chain without a log-management dependency.
    """

    _BUFFER_MAXLEN: Final[int] = 4096

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=self._BUFFER_MAXLEN)
        self._seq: int = 0

    def log_event(
        self,
        trace_id: str,
        agent_name: str,
        event_type: str,
        payload: Mapping[str, Any] | Any,
        status: str,
    ) -> dict[str, Any]:
        """Emit one audit record and return it (handy for tests/chaining).

        Args:
            trace_id: Mission-level correlation ID shared by every event of
                one conjunction response.
            agent_name: Actor responsible for the event (agent or service).
            event_type: Machine-readable event class, e.g.
                "MANEUVER_INSPECTION" or "CIRCUIT_BREAKER_TRIPPED".
            payload: Event detail (any JSON-serialisable structure).
            status: Outcome string; also drives severity mapping.
        """
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": _map_severity(status),
            "service_name": _SERVICE_NAME,
            "trace_id": str(trace_id),
            "agent_name": agent_name,
            "event_type": event_type,
            "status": str(status),
            "payload": json.loads(json.dumps(payload, default=str)),
        }
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self._lock:
            self._seq += 1
            record["seq"] = self._seq
            print(line, file=self._stream, flush=True)
            self._events.append(record)
        return record

    def latest_seq(self) -> int:
        """Highest sequence number handed out so far (0 before any event)."""
        with self._lock:
            return self._seq

    def get_events_since(self, seq: int) -> list[dict[str, Any]]:
        """Buffered records with sequence numbers strictly above ``seq``.

        The cursor contract powering the Server-Sent-Events live feed: a
        client remembers the last ``seq`` it saw and polls this for new
        records. Oldest first; each returned copy is independent.
        """
        wanted = int(seq)
        with self._lock:
            return [dict(record) for record in self._events if int(record.get("seq", 0)) > wanted]

    def get_events_by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """Replay buffered audit records for one mission trace (oldest first)."""
        wanted = str(trace_id)
        with self._lock:
            return [dict(record) for record in self._events if record["trace_id"] == wanted]


class JsonFormatter(logging.Formatter):
    """Logging formatter rendering standard ``logging`` records as JSON lines.

    Used by uvicorn's ``--log-config logging.json`` so framework logs land in
    Cloud Logging with the same structured shape as AuditLogger events.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


#: Process-wide singleton used by ModelArmor, MemoryBank and the orchestrator.
audit_logger = AuditLogger()

__all__ = ["AuditLogger", "JsonFormatter", "audit_logger"]