"""Debate — shared Pydantic models for the conjunction debate."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Strategy = Literal["prograde_burn", "retrograde_burn", "normal_burn", "hold_and_rescreen"]

STRATEGIES: tuple[str, ...] = ("prograde_burn", "retrograde_burn", "normal_burn", "hold_and_rescreen")


class StrategistPersona(BaseModel):
    """Identity card for one debate participant."""

    model_config = ConfigDict(frozen=True)

    name: str
    philosophy: str
    temperature: float = Field(default=0.6, ge=0.0, le=2.0)


class ManeuverProposal(BaseModel):
    """One strategist's structured answer — validated before it counts."""

    strategist: str
    strategy: Strategy
    delta_v_mps: float = Field(ge=0.0)
    target_miss_distance_km: float = Field(gt=0.0)
    rationale: str
    #: Echo of the screening numbers the argument relied on; cross-checked
    #: against the real screening result (hallucination detection).
    cited_screening_values: dict[str, Any] = Field(default_factory=dict)

    def canonical(self) -> str:
        """Deterministic serialisation used for verbatim-repeat detection."""
        import json

        return json.dumps(
            {
                "strategist": self.strategist,
                "strategy": self.strategy,
                "delta_v_mps": round(float(self.delta_v_mps), 6),
                "target_miss_distance_km": round(float(self.target_miss_distance_km), 6),
                "rationale": self.rationale.strip(),
            },
            sort_keys=True,
        )


class DebateArgument(BaseModel):
    """One contribution in the transcript: a proposal and/or a critique."""

    round: int
    strategist: str
    argument: str
    critique_of: str | None = None
    proposal: ManeuverProposal


class DebateTranscript(BaseModel):
    """Everything one debate produced, persisted under the mission trace."""

    trace_id: str
    conjunction_id: str | None = None
    sat_id: str = ""
    debris_id: str = ""
    rounds: list[DebateArgument] = Field(default_factory=list)
    flags: list[dict[str, Any]] = Field(default_factory=list)
    converged: bool = False
    winner: str | None = None
    final_proposal: dict[str, Any] | None = None
    fallback_used: bool = False
    judge_used: bool = False
    completed_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


__all__ = [
    "STRATEGIES",
    "DebateArgument",
    "DebateTranscript",
    "ManeuverProposal",
    "StrategistPersona",
]
