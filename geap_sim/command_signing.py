"""GEAP simulation — manoeuvre command authentication.

Why this exists
---------------
The SafetyOfficer's rule R2 requires that *every manoeuvre command carries a
cryptographic signature of at least 64 hexadecimal characters*. Nothing in
the fleet produced one. The only signature-shaped field anywhere in a
mission was ``ack_signature`` on the negotiation result, and that is a
completely different claim:

* ``ack_signature`` asserts **the counterparty agreed**. It is consent.
* the command signature asserts **this command is ours and is unaltered**.
  It is authentication.

Conflating the two is not a naming quibble. On the common real-world path —
a conjunction with uncontrolled debris — there is no counterparty to
consent, so the fleet deliberately leaves ``ack_signature`` empty rather
than forging agreement that never happened. The SafetyOfficer then saw a
manoeuvre command with no signature at all and rejected it
``UNSIGNED_COMMAND``, every time, correctly. The gate was working; the
artifact it demanded was never built.

The fix is to sign our own commands, which is the thing R2 was always
asking for. Signing our own manoeuvre proves our integrity. It says nothing
whatsoever about the counterparty, and that separation is preserved
here — this module cannot and does not produce an acknowledgement.

What the signature covers
-------------------------
An HMAC-SHA256 over a canonical serialisation of the command's *safety
critical* fields: which spacecraft, which threat, which direction, how much
delta-v, and for which encounter. Anything that would change the physical
effect of the burn changes the signature. Fields that do not — rationale
text, display strings, timestamps of when a message happened to be
rendered — are excluded deliberately, so that re-rendering a mission report
cannot invalidate a command that has not changed.

Key management, stated honestly
-------------------------------
``ORBIT_COMMAND_SIGNING_KEY`` holds the operator key. In a real deployment
this belongs in Secret Manager and the verifying party is the spacecraft's
command receiver.

When the variable is unset, this module mints a random key for the lifetime
of the process and audits that it has done so. That keeps a local run
working, and the resulting HMAC is a genuine integrity check *within* the
process — a command altered between signing and inspection still fails.
What it cannot do is let anyone outside the process verify the signature,
because the key dies with it. That limitation is audited rather than hidden:
a signature that looks production-grade but is not is worse than no
signature, so ``key_source`` travels with every command.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Final

from geap_sim.observability import audit_logger

SIGNED_FIELDS: Final[tuple[str, ...]] = (
    "sat_id",
    "debris_id",
    "action",
    "dv_direction",
    "our_dv_mps",
    "tca_iso",
    "conjunction_id",
)

_ENV_KEY: Final[str] = "ORBIT_COMMAND_SIGNING_KEY"

#: Resolved once per process so the ephemeral-key warning is emitted exactly
_signing_key: bytes | None = None
_key_source: str = "unresolved"


def _resolve_key() -> tuple[bytes, str]:
    """Return the signing key and where it came from (audited once)."""
    global _signing_key, _key_source
    if _signing_key is not None:
        return _signing_key, _key_source

    configured = os.environ.get(_ENV_KEY, "").strip()
    if configured:
        _signing_key, _key_source = configured.encode("utf-8"), "configured"
    else:
        _signing_key, _key_source = secrets.token_bytes(32), "ephemeral_process_key"
        audit_logger.log_event(
            trace_id="startup",
            agent_name="geap_sim.command_signing",
            event_type="COMMAND_SIGNING_KEY_EPHEMERAL",
            payload={
                "reason": f"{_ENV_KEY} is unset",
                "effect": (
                    "commands are signed with a per-process key; signatures are "
                    "valid within this process but cannot be verified externally"
                ),
            },
            status="DEGRADED",
        )
    return _signing_key, _key_source


def canonical_payload(command: dict[str, Any]) -> str:
    """Canonical serialisation of the safety-critical fields of a command.

    Deterministic across hosts and Python versions: fixed field order, no
    insignificant whitespace, floats normalised so ``2`` and ``2.0`` do not
    sign differently.
    """
    canonical: dict[str, Any] = {}
    for field in SIGNED_FIELDS:
        value = command.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            canonical[field] = round(float(value), 6)
        else:
            canonical[field] = "" if value is None else str(value)
    return json.dumps(canonical, separators=(",", ":"), sort_keys=False)


def sign_maneuver_command(command: dict[str, Any]) -> dict[str, Any]:
    """Attach a command signature to a proposed manoeuvre.

    Args:
        command: The manoeuvre being proposed. Only :data:`SIGNED_FIELDS` are
            covered; the rest travels unsigned because it cannot change what
            the spacecraft does.

    Returns:
        A copy of ``command`` carrying ``command_signature`` (64 hex
        characters, satisfying the SafetyOfficer's R2), the ``signed_fields``
        it covers, the ``signing_algorithm`` and the ``key_source`` — so an
        auditor can see not just that a signature exists but what it is worth.
    """
    key, source = _resolve_key()
    digest = hmac.new(key, canonical_payload(command).encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        **command,
        "command_signature": digest,
        "signed_fields": list(SIGNED_FIELDS),
        "signing_algorithm": "HMAC-SHA256",
        "key_source": source,
    }


def verify_maneuver_command(command: dict[str, Any]) -> bool:
    """Check that a signed command has not been altered since signing.

    Returns ``False`` for an absent or malformed signature rather than
    raising: an unsigned command is not an error condition to be handled, it
    is a command that fails verification.
    """
    presented = str(command.get("command_signature", ""))
    if len(presented) != 64:
        return False
    key, _ = _resolve_key()
    expected = hmac.new(key, canonical_payload(command).encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


__all__ = [
    "SIGNED_FIELDS",
    "canonical_payload",
    "sign_maneuver_command",
    "verify_maneuver_command",
]
