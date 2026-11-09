from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING, TypedDict

from django.conf import settings

if TYPE_CHECKING:
    from tickets.models import Ticket

logger = logging.getLogger(__name__)


# Placeholder shown when a value cannot be computed (missing key, missing
# field). Distinct glyphs so a support agent can tell "not computed" from
# a real code at a glance.
_PLACEHOLDER_FINGERPRINT = "········"
_PLACEHOLDER_SEAL = "····"
_PLACEHOLDER_BIND_CODE = "······"


class SecurityFeature(TypedDict):
    """One row on the ticket page's "security features" panel."""

    label: str          # human-readable name
    value: str          # the displayed code (or placeholder)
    help: str           # short explanatory text


class SecurityContext(TypedDict):
    """Everything the ticket template needs for the security panel."""

    fingerprint: SecurityFeature
    seal: SecurityFeature
    bind_code: SecurityFeature
    key_version: int


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------
def _signing_key_bytes() -> bytes | None:
    """
    The key used for the displayed HMACs.

    Uses ``TICKET_QR_SIGNING_KEY`` (same key as QR payload signing) rather
    than ``SECRET_KEY`` so that rotating session/CSRF secrets doesn't
    change every ticket's displayed values.

    Returns ``None`` if the key is not configured. Callers must treat that
    as "feature unavailable" and use the placeholder — never crash the
    ticket page over a display badge.
    """
    key = getattr(settings, "TICKET_QR_SIGNING_KEY", None)
    if not key:
        logger.warning(
            "TICKET_QR_SIGNING_KEY is not configured; ticket security "
            "features will show placeholders."
        )
        return None
    return key.encode("utf-8")


# ---------------------------------------------------------------------------
# Individual features
# ---------------------------------------------------------------------------
def ticket_fingerprint(ticket: "Ticket") -> str:
    """First 8 hex chars of ``token_hash`` — stable, non-reversible."""
    value = (getattr(ticket, "token_hash", "") or "")[:8].upper()
    return value or _PLACEHOLDER_FINGERPRINT


def ciphertext_seal(ticket: "Ticket") -> str:
    """
    4-char base32 tag over the ciphertext.

    Changes if the ciphertext changes — i.e. after a lazy re-encryption
    following a key rotation. A screenshot from before the rotation won't
    match the seal on the live page.
    """
    ciphertext = getattr(ticket, "token_ciphertext", "") or ""
    if not ciphertext:
        return _PLACEHOLDER_SEAL
    key = _signing_key_bytes()
    if key is None:
        return _PLACEHOLDER_SEAL
    mac = hmac.new(key, ciphertext.encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(mac)[:4].decode("ascii")


def page_bind_code(ticket: "Ticket") -> str:
    """
    6-char hex code that changes whenever the ticket's state changes.

    Derived from ``public_id``, ``status``, and ``used_at``. All three are
    either public or already shown on the page, so the HMAC key is what
    stops a viewer from predicting the *next* value.
    """
    key = _signing_key_bytes()
    if key is None:
        return _PLACEHOLDER_BIND_CODE

    # ``used_at`` only exists after the ticket is scanned; for VALID tickets
    # it's ``None`` and we fall back to ``issued_at``. Both are already
    # visible on the page, so including them doesn't leak anything.
    state_anchor = (
        getattr(ticket, "used_at", None)
        or getattr(ticket, "issued_at", None)
        or ""
    )
    material = (
        f"{getattr(ticket, 'public_id', '')}"
        f"|{getattr(ticket, 'status', '')}"
        f"|{state_anchor}"
    )
    return hmac.new(
        key, material.encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:6].upper()


# ---------------------------------------------------------------------------
# Bundle for the template
# ---------------------------------------------------------------------------
def build_security_context(ticket: "Ticket") -> SecurityContext:
    """
    Assemble the security panel for the ticket template.

    Returns a nested structure so the template can render each feature with
    its label and help text — a bare 8-char hex string on the page tells
    the holder nothing and helps support only if they know what to do with
    it.
    """
    return {
        "fingerprint": {
            "label": "Ticket fingerprint",
            "value": ticket_fingerprint(ticket),
            "help": "First 8 characters of this ticket's unique code. "
                    "Matches every print of the same ticket.",
        },
        "seal": {
            "label": "Ciphertext seal",
            "value": ciphertext_seal(ticket),
            "help": "Short tag over the encrypted token. Changes if the "
                    "ticket is re-issued under a new encryption key, so "
                    "support can tell a fresh screenshot from a stale one.",
        },
        "bind_code": {
            "label": "State code",
            "value": page_bind_code(ticket),
            "help": "Changes when the ticket is scanned, voided, or "
                    "re-issued. A screenshot of a valid ticket shows a "
                    "different code after the ticket is used.",
        },
        "key_version": getattr(ticket, "token_key_version", 0),
    }