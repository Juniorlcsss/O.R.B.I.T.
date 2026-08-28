"""GEAP simulation — Model Armour inline guardrail middleware.

This is the enforcement heart of the *Fortified Enterprise Fleet* story: a
deterministic, code-level sweep that runs AFTER the LLM Safety Officer has
issued its verdict but BEFORE any manoeuvre is persisted or authorised for
uplink. LLMs advise; this class decides.

The four checks (any failure → REJECTED, fail-closed):

1. **Hallucination Guard** — the delta-v inside the negotiation payload must
   match the expected (astrodynamics-recommended) delta-v embedded in the
   safety verdict to within 0.1 m/s. Catches payload drift and fabricated
   numbers between pipeline stages.
2. **Policy Ceiling** — the operative delta-v may never exceed
   ``MAX_ALLOWED_DELTA_V_MPS`` (single source of truth in ``agents.safety``,
   the same constant the LLM prompts are written from).
3. **Fuel Guard** — if WE execute the dodge, the projected fuel after the
   burn (queried live from the MemoryBank) must stay above the 5%
   strategic reserve. Counterparty dodges are bounded by their own fleet
   APIs instead, so the check is skipped for them.
4. **Secret/PII Leak** — recursive pattern sweep over every string in both
   payloads (API keys, tokens, JWTs, PEM blocks, e-mails, credential-ish
   keys). Findings are reported by PATTERN NAME and JSON PATH only — matched
   content is never echoed back into logs.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from geap_sim.safety_limits import MAX_ALLOWED_DELTA_V_MPS

from geap_sim.memory_bank import MemoryBank, estimate_fuel_after_burn, get_shared_memory_bank
from geap_sim.observability import audit_logger

STATUS_APPROVED: Final[str] = "APPROVED"
STATUS_REJECTED: Final[str] = "REJECTED"

_STRATEGIC_RESERVE_FUEL_PERCENT: Final[float] = 5.0
_DELTA_V_MATCH_TOLERANCE_MPS: Final[float] = 0.1

# (label, compiled pattern) — findings are reported by LABEL only.
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36}\b")),
    ("OPENAI_STYLE_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("PRIVATE_KEY_BLOCK", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("EMAIL_ADDRESS", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "CREDENTIAL_KEY_VALUE",
        re.compile(r"(?i)[\"']?(password|passwd|secret|api[_-]?key|client[_-]?secret)[\"']?\s*[:=]\s*[\"']?[^\"\s,}{]+"),
    ),
)


class ArmorReport(BaseModel):
    """Immutable outcome of one Model Armour inspection."""

    model_config = ConfigDict(frozen=True)

    status: str
    violations: list[str] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict)
    audit_trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    mission_trace_id: str | None = None
    evaluated_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _iter_strings(node: Any, path: str):
    """Yield (json_path, string_value) for every string in a nested structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _iter_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


class ModelArmor:
    """Inline guardrails executed between verdict and execution."""

    def __init__(self, memory_bank: MemoryBank | None = None) -> None:
        self._memory_bank = memory_bank if memory_bank is not None else get_shared_memory_bank()

    async def inspect_maneuver_request(self, negotiation_payload: dict[str, Any], safety_verdict: dict[str, Any]) -> ArmorReport:
        """Adjudicate a proposed manoeuvre against all four armour checks.

        Args:
            negotiation_payload: The DiplomatAgent's structured outcome plus
                dossier context (``sat_id``, ``debris_id``). Keys of record:
                ``action``, ``our_dv_mps``, ``their_dv_mps``.
            safety_verdict: The LLM Safety Officer's JSON verdict enriched by
                the orchestrator with ``expected_delta_v_mps`` — the value
                Check 1 anchors against.

        Returns:
            ArmorReport with ``status`` APPROVED/REJECTED, human-readable
            ``violations``, per-check ``checks`` detail and an
            ``audit_trace_id`` correlating with the AuditLogger trail.
        """
        trace_id = uuid.uuid4().hex
        mission_trace_id = str(negotiation_payload.get("mission_trace_id") or "").strip() or None
        violations: list[str] = []
        checks: dict[str, str] = {}

        action = str(negotiation_payload.get("action", "")).strip().lower()
        our_dv = _as_float(negotiation_payload.get("our_dv_mps")) or 0.0
        their_dv = _as_float(negotiation_payload.get("their_dv_mps")) or 0.0
        operative_dv = our_dv if action == "we_dodge" else their_dv if action == "they_dodge" else 0.0
        sat_id = str(negotiation_payload.get("sat_id") or safety_verdict.get("sat_id") or "")

        # ---- Check 1: hallucination guard ---------------------------------
        expected_dv = _as_float(safety_verdict.get("expected_delta_v_mps"))
        if action == "standoff":
            checks["hallucination_guard"] = "SKIPPED — standoff proposes no manoeuvre"
        elif expected_dv is None:
            violations.append("HALLUCINATION_GUARD: no expected_delta_v_mps supplied for cross-check")
            checks["hallucination_guard"] = "FAIL — missing reference delta-v (fail-closed)"
        elif abs(operative_dv - expected_dv) > _DELTA_V_MATCH_TOLERANCE_MPS:
            violations.append(
                f"HALLUCINATED_DELTA_V: negotiated {operative_dv:.3f} m/s diverges from "
                f"approved {expected_dv:.3f} m/s beyond {_DELTA_V_MATCH_TOLERANCE_MPS:.1f} m/s tolerance"
            )
            checks["hallucination_guard"] = "FAIL"
        else:
            checks["hallucination_guard"] = f"PASS — {operative_dv:.3f} m/s within tolerance of {expected_dv:.3f} m/s"

        # ---- Check 2: policy ceiling ---------------------------------------
        if operative_dv > MAX_ALLOWED_DELTA_V_MPS:
            violations.append(
                f"POLICY_CEILING_EXCEEDED: operative delta-v {operative_dv:.3f} m/s "
                f"exceeds the {MAX_ALLOWED_DELTA_V_MPS:.1f} m/s hard ceiling"
            )
            checks["policy_ceiling"] = "FAIL"
        else:
            checks["policy_ceiling"] = f"PASS — {operative_dv:.3f} m/s <= {MAX_ALLOWED_DELTA_V_MPS:.1f} m/s"

        # ---- Check 3: strategic fuel reserve --------------------------------
        if action == "we_dodge":
            satellite_state = await self._memory_bank.get_satellite_state(sat_id)
            current_fuel = float(satellite_state["fuel_percentage"])
            projected_fuel = estimate_fuel_after_burn(current_fuel, our_dv)
            if projected_fuel < _STRATEGIC_RESERVE_FUEL_PERCENT:
                violations.append(
                    f"STRATEGIC_RESERVE_VIOLATION: burn would leave {projected_fuel:.2f}% fuel, "
                    f"below the {_STRATEGIC_RESERVE_FUEL_PERCENT:.1f}% reserve"
                )
                checks["fuel_guard"] = f"FAIL — current {current_fuel:.2f}%, projected {projected_fuel:.2f}%"
            else:
                checks["fuel_guard"] = f"PASS — current {current_fuel:.2f}%, projected {projected_fuel:.2f}%"
        else:
            checks["fuel_guard"] = "SKIPPED — our thrusters are not the operative actuator"

        # ---- Check 4: secret / PII leak sweep -------------------------------
        leak_findings: list[str] = []
        for label, pattern in _SECRET_PATTERNS:
            for json_path, text in _iter_strings({"negotiation": negotiation_payload, "verdict": safety_verdict}, "$"):
                if pattern.search(text):
                    leak_findings.append(f"{label} @ {json_path}")
        if leak_findings:
            violations.extend(f"SENSITIVE_DATA_LEAK: {finding}" for finding in leak_findings)
            checks["secret_scan"] = f"FAIL — {len(leak_findings)} finding(s); content withheld from logs"
        else:
            checks["secret_scan"] = f"PASS — {_len_strings(negotiation_payload, safety_verdict)} strings scanned"

        status = STATUS_REJECTED if violations else STATUS_APPROVED
        audit_logger.log_event(
            trace_id=mission_trace_id or trace_id,
            agent_name="geap_sim.model_armor",
            event_type="MANEUVER_INSPECTION",
            payload={
                "inspection_trace_id": trace_id,
                "action": action,
                "operative_dv_mps": operative_dv,
                "expected_dv_mps": expected_dv,
                "violations": violations,
                "checks": checks,
            },
            status=status,
        )

        return ArmorReport(
            status=status,
            violations=violations,
            checks=checks,
            audit_trace_id=trace_id,
            mission_trace_id=mission_trace_id,
        )


def _len_strings(*payloads: dict[str, Any]) -> int:
    total = 0
    for payload in payloads:
        total += sum(1 for _ in _iter_strings(payload, "$"))
    return total


_shared_armor: ModelArmor | None = None


def get_shared_model_armor() -> ModelArmor:
    """Process-wide ModelArmor singleton sharing the default MemoryBank."""
    global _shared_armor
    if _shared_armor is None:
        _shared_armor = ModelArmor()
    return _shared_armor


__all__ = [
    "ArmorReport",
    "ModelArmor",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "get_shared_model_armor",
]
