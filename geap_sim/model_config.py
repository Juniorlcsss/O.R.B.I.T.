"""Shared generation config for the fleet's structured-JSON agents."""

from __future__ import annotations

import os
from typing import Final

from google.genai import types


THINKING_ALLOWANCE_TOKENS: Final[int] = int(
    os.environ.get("ORBIT_THINKING_ALLOWANCE_TOKENS", "1024")
)

DEFAULT_THINKING_LEVEL: Final[str] = os.environ.get(
    "ORBIT_THINKING_LEVEL", "LOW"
).strip().upper()


def structured_json_config(
    *,
    answer_tokens: int,
    temperature: float = 0.0,
) -> types.GenerateContentConfig:
    """Generation config for an agent that must return schema-valid JSON.

    Args:
        answer_tokens: Room the JSON answer itself needs. State the size of
            the *answer*, not the total budget — the thinking allowance is
            added here so no call site has to remember that thinking is
            billed against the same ceiling.
        temperature: Sampling temperature. Defaults to 0.0, because a
            manoeuvre verdict that changes between identical inputs is not a
            verdict.

    Returns:
        A ``GenerateContentConfig`` requesting JSON output with bounded
        thinking and a budget that accounts for it.
    """
    return types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=answer_tokens + THINKING_ALLOWANCE_TOKENS,
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_level=DEFAULT_THINKING_LEVEL),
    )


__all__ = [
    "DEFAULT_THINKING_LEVEL",
    "THINKING_ALLOWANCE_TOKENS",
    "structured_json_config",
]
