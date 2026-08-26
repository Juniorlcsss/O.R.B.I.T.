"""Evolution — deterministic gaming detection (runs BEFORE any LLM review).

These five heuristics are pure code. They are the first line of defence
against a proposer optimising its own comfort instead of fleet safety,
and they run on every proposal regardless of who — or what — proposed it.
Any CRITICAL flag forces an automatic REJECT before the Meta-Critic's
verdict is even considered; the LLM review then provides the second,
reasoning-based layer (defence in depth).

Heuristics
----------
G1 CONVENIENCE_DRIFT   raising ``pc_high_threshold`` means fewer HIGH
                       alerts → less work. Only legitimate with real
                       over-reaction evidence; CRITICAL when pushed hard.
G2 ATTENTION_DRIFT     dramatically *lowering* the threshold inflates alert
                       volume (looking busy) without under-reaction proof.
G3 SAFETY_EROSION      any move that erodes a margin — cutting the fuel
                       reserve, shrinking preferred miss distance, pushing
                       efficiency bias toward pure economy, or dragging a
                       threshold toward its unsafe bound. Always CRITICAL.
G4 OSCILLATION         flip-flopping the same parameter across recent
                       cycles signals curve-fitting to noise.
G5 UNJUSTIFIED_MAGNITUDE  large steps justified by thin evidence.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from evolution.policy import EVOLUTION_ENVELOPE, MAX_STEP_FRACTION, ScreeningPolicy


class GamingFlag(BaseModel):
    """One deterministic suspicion signal about a proposed change."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(pattern="^G[1-5]$")
    severity: str = Field(pattern="^(INFO|WARNING|CRITICAL)$")
    detail: str


_OVER_RATE_JUSTIFIED: Final[float] = 0.30      # ≥30 % over-reactions justifies loosening
_UNDER_RATE_JUSTIFIED: Final[float] = 0.30     # ≥30 % under-reactions justifies tightening
_G1_CRITICAL_RATIO: Final[float] = 5.0         # threshold multiplied ≥5x is never "incremental"
_ENVELOPE_EPSILON_FRACTION: Final[float] = 0.01  # >1 % of range toward an unsafe bound counts
_MIN_EVIDENCE_FOR_ANY_CHANGE: Final[int] = 3
_OSCILLATION_WINDOW: Final[int] = 4            # last K cycles examined for flip-flops
_OSCILLATION_FLIPS: Final[int] = 2             # direction changes that constitute oscillation


def _fraction_of_range(name: str, value: float) -> float:
    low, high = EVOLUTION_ENVELOPE[name]
    return abs(high - low)


def _unsafe_direction(name: str) -> int:
    """+1 when increasing the parameter erodes safety, -1 when decreasing does.

    Only *margin* parameters appear here: for those, outcome statistics can
    never justify erosion. The Pc thresholds are deliberately excluded —
    raising/lowering them is exactly what evidence-based tuning does, so
    their safety handling lives in G1/G2 (evidence gate) plus G3's
    evidence-and-magnitude rule below.
    """
    return {
        "preferred_miss_distance_km": -1, # lower → thinner safety cushion
        "delta_v_efficiency_bias": +1,    # higher → closer to pure fuel economy
        "fuel_reserve_floor_pct": -1,     # lower → burns into the strategic reserve
    }.get(name, 0)


def _flag_is_true(outcome: Any, flag: str) -> bool:
    """Truth-test an outcome field across dicts (memory bank) and models."""
    value = outcome.get(flag) if isinstance(outcome, dict) else getattr(outcome, flag, None)
    return value is True


def _rate(outcomes: list[Any], flag: str) -> float:
    relevant = [o for o in outcomes if _flag_is_true(o, flag)]
    return len(relevant) / len(outcomes) if outcomes else 0.0


def detect_gaming(
    current: ScreeningPolicy,
    proposed: ScreeningPolicy,
    outcomes: list[Any],
    history: list[dict[str, Any]],
) -> list[GamingFlag]:
    """Run all deterministic heuristics; returns every flag raised.

    Args:
        current: the active policy.
        proposed: the candidate policy under review.
        outcomes: recent MissionOutcome records (evidence base).
        history: recent EvolutionCycle dicts (for oscillation analysis).
    """
    flags: list[GamingFlag] = []
    n = len(outcomes)
    over_rate = _rate(outcomes, "over_reacted")
    under_rate = _rate(outcomes, "under_reacted")

    for name in EVOLUTION_ENVELOPE:
        span = _fraction_of_range(name, getattr(current, name))
        old_value = float(getattr(current, name))
        new_value = float(getattr(proposed, name))
        delta = new_value - old_value
        if abs(delta) < span * 1e-9:
            continue
        magnitude_fraction = abs(delta) / span

        # --- G1 CONVENIENCE_DRIFT -------------------------------------------------
        if name == "pc_high_threshold" and delta > 0:
            justified = over_rate >= _OVER_RATE_JUSTIFIED
            ratio = new_value / max(old_value, 1e-12)
            if not justified:
                severity = "CRITICAL" if ratio >= _G1_CRITICAL_RATIO else "WARNING"
                flags.append(GamingFlag(
                    code="G1",
                    severity=severity,
                    detail=(
                        f"raises pc_high_threshold {old_value:.3g}→{new_value:.3g} ({ratio:.1f}x, fewer HIGH "
                        f"alerts → less work) but only {over_rate:.0%} of {n} outcomes show over-reaction"
                    ),
                ))
            elif ratio >= _G1_CRITICAL_RATIO:
                flags.append(GamingFlag(
                    code="G1",
                    severity="CRITICAL",
                    detail=(
                        f"raises pc_high_threshold {ratio:.1f}x in one cycle "
                        f"({old_value:.3g}→{new_value:.3g}); no evidence base justifies that magnitude"
                    ),
                ))

        # --- G2 ATTENTION_DRIFT ---------------------------------------------------
        if name == "pc_high_threshold" and delta < 0 and new_value <= 0.5 * old_value and under_rate < _UNDER_RATE_JUSTIFIED:
            flags.append(GamingFlag(
                code="G2",
                severity="WARNING",
                detail=(
                    f"drops pc_high_threshold {old_value:.3g}→{new_value:.3g} (doubles or better the HIGH alert "
                    f"volume — looks busy) with only {under_rate:.0%} under-reaction evidence in {n} outcomes"
                ),
            ))

        # --- G3 SAFETY_EROSION ----------------------------------------------------
        # Margin parameters: any unsafe-ward movement is CRITICAL, period.
        # Pc thresholds: erosion is only tolerable when the outcome evidence
        # justifies the direction (mirroring G1/G2) AND the step stays within
        # the per-cycle cap — otherwise it is treated as safety erosion too.
        unsafe = _unsafe_direction(name)
        if name in ("pc_high_threshold", "pc_medium_threshold"):
            evidence_ok = over_rate >= _OVER_RATE_JUSTIFIED if name == "pc_high_threshold" else under_rate >= _UNDER_RATE_JUSTIFIED
            # Slightly-over-cap steps are the clamp's job (CLAMPED_APPLIED);
            # only grossly over-cap moves — or missing evidence entirely —
            # escalate to a safety-erosion REJECT here.
            grossly_over = magnitude_fraction > (2 * MAX_STEP_FRACTION)
            if delta > 0 and ((not evidence_ok) or grossly_over):
                flags.append(GamingFlag(
                    code="G3",
                    severity="CRITICAL",
                    detail=(
                        f"erodes safety margin: {name} moves {old_value:.6g}→{new_value:.6g} "
                        f"({magnitude_fraction:.0%} of envelope range) without sufficient "
                        f"{'over' if name == 'pc_high_threshold' else 'under'}-reaction evidence "
                        f"({(over_rate if name == 'pc_high_threshold' else under_rate):.0%} of {n})"
                    ),
                ))
        elif unsafe != 0 and (delta * unsafe) > 0 and magnitude_fraction > _ENVELOPE_EPSILON_FRACTION:
            flags.append(GamingFlag(
                code="G3",
                severity="CRITICAL",
                detail=(
                    f"erodes safety margin: {name} moves {old_value:.6g}→{new_value:.6g}, "
                    f"{magnitude_fraction:.0%} of its envelope range toward its unsafe bound"
                ),
            ))

        # --- G5 UNJUSTIFIED_MAGNITUDE ----------------------------------------------
        if magnitude_fraction > MAX_STEP_FRACTION and n < 10:
            flags.append(GamingFlag(
                code="G5",
                severity="WARNING",
                detail=(
                    f"{name} shifts {magnitude_fraction:.0%} of its envelope range "
                    f"({old_value:.3g}→{new_value:.3g}) on thin evidence ({n} outcomes)"
                ),
            ))
        elif magnitude_fraction > 4 * MAX_STEP_FRACTION and n < _MIN_EVIDENCE_FOR_ANY_CHANGE:
            flags.append(GamingFlag(
                code="G5",
                severity="WARNING",
                detail=f"{name} swings {magnitude_fraction:.0%} of range with almost no evidence ({n} outcomes)",
            ))

    # Any actual proposal with essentially zero evidence base.
    changed = [
        name for name in EVOLUTION_ENVELOPE
        if abs(float(getattr(proposed, name)) - float(getattr(current, name))) > _fraction_of_range(name, getattr(current, name)) * 1e-9
    ]
    if changed and n < _MIN_EVIDENCE_FOR_ANY_CHANGE:
        flags.append(GamingFlag(
            code="G5",
            severity="WARNING",
            detail=f"proposes {len(changed)} change(s) {changed} from only {n} outcome(s); insufficient evidence for ANY tuning",
        ))

    # --- G4 OSCILLATION -----------------------------------------------------------
    directions: dict[str, list[int]] = {}
    for cycle in history[-_OSCILLATION_WINDOW:]:
        after = cycle.get("after") or {}
        before = cycle.get("before") or {}
        for name in EVOLUTION_ENVELOPE:
            try:
                step = float(after.get(name)) - float(before.get(name))
            except (TypeError, ValueError, AttributeError):
                continue
            if abs(step) > 0:
                directions.setdefault(name, []).append(1 if step > 0 else -1)

    for name, signs in directions.items():
        flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
        if flips >= _OSCILLATION_FLIPS and name in changed:
            flags.append(GamingFlag(
                code="G4",
                severity="WARNING",
                detail=(
                    f"{name} has flipped direction {flips} times in the last {_OSCILLATION_WINDOW} cycles "
                    f"and this proposal moves it again — possible curve-fitting to noise"
                ),
            ))

    return flags


__all__ = ["GamingFlag", "detect_gaming"]
