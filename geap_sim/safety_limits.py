"""Fleet safety limits — the numbers the prompt and the code must share."""

from __future__ import annotations

from typing import Final

MAX_ALLOWED_DELTA_V_MPS: Final[float] = 50.0

#minimum accepted length
SIGNATURE_MIN_HEX_CHARS: Final[int] = 64

__all__ = ["MAX_ALLOWED_DELTA_V_MPS", "SIGNATURE_MIN_HEX_CHARS"]
