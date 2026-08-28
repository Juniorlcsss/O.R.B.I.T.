"""Evolution — the tunable ScreeningPolicy and its hard safety envelope.

The ScreeningPolicy is the fleet's learned judgement about risk. Every
field is bounded by ``EVOLUTION_ENVELOPE`` and every transition is limited
by ``MAX_STEP_FRACTION`` — together they form a deterministic box the
fleet literally cannot think its way out of:

* ``pc_high_threshold``   can never leave [1e-5, 1e-3] (default CARA HIGH
                          bound is 1e-4; the fleet may drift within a
                          decade either way, never further).
* ``fuel_reserve_floor_pct`` can never drop below 5 % — the strategic
                          reserve is not negotiable at any confidence level.
* ``delta_v_efficiency_bias`` can never reach 1.0 — pure fuel-optimisation
                          is forbidden; the fleet must always retain a
                          safety-first weighting.
* No parameter may jump more than ``MAX_STEP_FRACTION`` of its envelope
  range in one cycle, so even a hallucinated proposal changes behaviour
  gradually and observably.

``clamp_to_envelope`` is PURE and DETERMINISTIC: same inputs, same output,
no LLM anywhere near it. It runs on EVERY candidate policy — APPROVED,
CLAMPed or otherwise.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from geap_sim.memory_bank import get_shared_memory_bank


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScreeningPolicy(BaseModel):
    """The fleet's live risk-classification tuning. See module docstring."""

    model_config = ConfigDict(frozen=True)

    pc_high_threshold: float = Field(default=1e-4, gt=0.0, le=1.0)
    pc_medium_threshold: float = Field(default=1e-6, gt=0.0, le=1.0)
    preferred_miss_distance_km: float = Field(default=1.0, gt=0.0)
    delta_v_efficiency_bias: float = Field(default=0.5, ge=0.0, le=1.0)
    fuel_reserve_floor_pct: float = Field(default=5.0, ge=0.0, le=100.0)

    policy_version: int = 0
    updated_at: str = Field(default_factory=_utc_now_iso)
    provenance: str = Field(default="default", pattern="^(default|evolved|clamped)$")


#: Hard bounds per tunable parameter: (safe_low, safe_high). Nothing outside
#: these ranges can ever become the active policy, whatever any model says.
EVOLUTION_ENVELOPE: Final[dict[str, tuple[float, float]]] = {
    "pc_high_threshold": (1e-5, 1e-3),
    "pc_medium_threshold": (1e-7, 1e-5),
    "preferred_miss_distance_km": (0.5, 5.0),
    "delta_v_efficiency_bias": (0.0, 0.8),   # never allow pure fuel-optimisation
    "fuel_reserve_floor_pct": (5.0, 20.0),   # NEVER allow below 5 %
}

#: A parameter may move at most this fraction of its envelope RANGE per cycle.
MAX_STEP_FRACTION: Final[float] = 0.20

_TUNABLES: Final[tuple[str, ...]] = tuple(EVOLUTION_ENVELOPE)


# ---------------------------------------------------------------------------
# clamp_to_envelope — the deterministic hard boundary
# ---------------------------------------------------------------------------


def clamp_to_envelope(current: ScreeningPolicy, proposed: ScreeningPolicy) -> tuple[ScreeningPolicy, list[str]]:
    """Force ``proposed`` into the safe neighbourhood of ``current``.

    Applied in order:
      (a) per-parameter envelope bounds,
      (b) max-step limiting (≤ MAX_STEP_FRACTION of the envelope range per
          cycle, measured from ``current``),
      (c)+(d) ordering invariant pc_medium_threshold < pc_high_threshold,
          re-verified after clamping with a deterministic decade-separation
          repair if the two ever collide.

    Returns:
        ``(clamped_policy, clamp_actions)`` where each action is a
        human-readable string describing exactly what was altered.
    """
    actions: list[str] = []
    values: dict[str, float] = {}

    for name in _TUNABLES:
        low, high = EVOLUTION_ENVELOPE[name]
        span = high - low
        original = float(getattr(proposed, name))
        anchor = float(getattr(current, name))
        value = original

        # (a) hard envelope bounds.
        if value < low:
            value = low
        elif value > high:
            value = high

        # (b) max-step limiting around the current value.
        max_step = MAX_STEP_FRACTION * span
        if abs(value - anchor) > max_step:
            value = anchor + max_step if value > anchor else anchor - max_step
            # A step-limit may itself push past an envelope edge when the
            # anchor sits near a bound — re-bound afterwards.
            value = min(high, max(low, value))

        if abs(value - original) > 1e-15:
            actions.append(
                f"{name} adjusted from {original:.6g} to {value:.6g} "
                f"(envelope [{low:.6g}, {high:.6g}], max step {max_step:.6g})"
            )
        values[name] = value

    # (c)+(d) ordering invariant, enforced AFTER all other clamping.
    if values["pc_medium_threshold"] >= values["pc_high_threshold"]:
        repaired = values["pc_high_threshold"] / 10.0
        floor = EVOLUTION_ENVELOPE["pc_medium_threshold"][0]
        repaired = max(floor, repaired)
        actions.append(
            f"ordering invariant violated (medium {values['pc_medium_threshold']:.6g} >= "
            f"high {values['pc_high_threshold']:.6g}); medium threshold repaired to {repaired:.6g}"
        )
        values["pc_medium_threshold"] = repaired

    provenance = "clamped" if actions else proposed.provenance
    clamped = ScreeningPolicy(
        **values,
        policy_version=proposed.policy_version,
        updated_at=proposed.updated_at,
        provenance=provenance,
    )
    return clamped, actions


def validate_invariants(policy: ScreeningPolicy) -> list[str]:
    """Every invariant that must hold before a policy may be saved.

    Returns a list of violation descriptions; empty means the policy is
    safe to activate.
    """
    violations: list[str] = []
    for name in _TUNABLES:
        low, high = EVOLUTION_ENVELOPE[name]
        value = float(getattr(policy, name))
        if value != value or value in (float("inf"), float("-inf")):
            violations.append(f"{name} is not finite ({value!r})")
        elif not (low <= value <= high):
            violations.append(f"{name}={value:.6g} outside envelope [{low:.6g}, {high:.6g}]")

    if not policy.pc_medium_threshold < policy.pc_high_threshold:
        violations.append(
            f"ordering invariant: pc_medium_threshold ({policy.pc_medium_threshold:.6g}) "
            f"must be strictly below pc_high_threshold ({policy.pc_high_threshold:.6g})"
        )
    if policy.fuel_reserve_floor_pct < EVOLUTION_ENVELOPE["fuel_reserve_floor_pct"][0]:
        violations.append("strategic reserve below the absolute 5% floor")
    return violations


# ---------------------------------------------------------------------------
# PolicyStore — async persistence over the MemoryBank
# ---------------------------------------------------------------------------

_POLICY_COLLECTION: Final[str] = "evolution_current_policy"
_POLICY_DOC_ID: Final[str] = "active"

#: Short-lived read cache: screening runs on every alert and poll; the store
#: invalidates it whenever a new policy is saved, so applied cycles take
#: effect immediately while hot loops pay zero storage cost.
_CACHE_TTL_SECONDS: Final[float] = 5.0


class PolicyStore:
    """Async load/save of the single active :class:`ScreeningPolicy`."""

    def __init__(self, bank: Any | None = None) -> None:
        self._bank = bank if bank is not None else get_shared_memory_bank()
        self._cache: ScreeningPolicy | None = None
        self._cache_monotonic: float = 0.0

    def invalidate(self) -> None:
        """Drop the read cache (called automatically by :meth:`save`)."""
        self._cache = None
        self._cache_monotonic = 0.0

    async def load(self) -> ScreeningPolicy:
        """Current policy, or pristine defaults when none was ever saved."""
        import time as _time

        now = _time.monotonic()
        if self._cache is not None and (now - self._cache_monotonic) < _CACHE_TTL_SECONDS:
            return self._cache

        try:
            doc = await self._bank.get_doc(_POLICY_COLLECTION, _POLICY_DOC_ID)
        except Exception:  # noqa: BLE001 — screening must survive storage faults
            doc = None
        if not doc:
            policy = ScreeningPolicy()
        else:
            try:
                policy = ScreeningPolicy(**{k: v for k, v in dict(doc).items() if k != "doc_key"})
            except Exception:
                from geap_sim.observability import audit_logger

                audit_logger.log_event(
                    trace_id="evolution",
                    agent_name="evolution.policy_store",
                    event_type="POLICY_LOAD_CORRUPT_USING_DEFAULTS",
                    payload={"stored_keys": sorted(dict(doc).keys())},
                    status="DEGRADED",
                )
                policy = ScreeningPolicy()

        self._cache, self._cache_monotonic = policy, now
        return policy

    async def save(self, policy: ScreeningPolicy) -> ScreeningPolicy:
        """Persist a new active policy: bump version, stamp time, invalidate."""
        stored = policy.model_copy(
            update={
                "policy_version": int(policy.policy_version) + 1,
                "updated_at": _utc_now_iso(),
            }
        )
        await self._bank.put_doc(_POLICY_COLLECTION, _POLICY_DOC_ID, stored.model_dump())
        self.invalidate()
        try:
            # The screening hot path keeps its own short-TTL cache; an
            # applied cycle must take effect on the very next screen.
            from tools.space_tools import invalidate_policy_cache

            invalidate_policy_cache()
        except Exception:  # noqa: BLE001 — cache hygiene must never break saving
            pass
        return stored


# Module-level convenience store used by the screening hot path.
_default_store: PolicyStore | None = None


def get_shared_policy_store() -> PolicyStore:
    """Process-wide PolicyStore singleton."""
    global _default_store
    if _default_store is None:
        _default_store = PolicyStore()
    return _default_store


async def load_active_policy() -> ScreeningPolicy:
    """One-liner used by the screening hot path (falls back to defaults)."""
    return await get_shared_policy_store().load()


__all__ = [
    "EVOLUTION_ENVELOPE",
    "MAX_STEP_FRACTION",
    "PolicyStore",
    "ScreeningPolicy",
    "clamp_to_envelope",
    "get_shared_policy_store",
    "load_active_policy",
    "validate_invariants",
]
