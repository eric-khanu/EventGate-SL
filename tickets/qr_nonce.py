from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import TYPE_CHECKING

from django.core.cache import cache

if TYPE_CHECKING:
    from .models import Ticket


logger = logging.getLogger(__name__)


#: Nonce lifetime, in seconds. Anything longer than the QR is likely to
#: remain on-screen is wasted; 10 minutes covers "buyer opens the ticket at
#: the gate, fumbles with their phone, then approaches the scanner".
NONCE_TTL_SECONDS = 10 * 60

#: Nonce length in bytes, pre-base64url. 16 bytes = 128 bits — collision
#: probability is negligible and the payload stays short.
NONCE_BYTES = 16

#: Cache key for the currently active nonce *hash* for a ticket.
_ACTIVE_KEY = "eventgate:qr:nonce:{ticket_id}"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------
def hash_nonce(nonce: str) -> str:
    """SHA-256 of a raw nonce. This is what gets stored and audited."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()

def _active_key(ticket_id: int) -> str:
    return _ACTIVE_KEY.format(ticket_id=ticket_id)


def issue_nonce(ticket: "Ticket") -> tuple[str, int]:
    """
    Mint a fresh nonce for the ticket and record its hash.

    Returns ``(raw_nonce, expires_at_unix)``. The raw nonce is embedded in
    the QR payload; only its hash is stored.

    Note: *always* mints. Reuse is not supported because reuse would require
    storing the raw nonce in the cache, which is worse than storing nothing.
    A page refresh is a user action, so a fresh nonce is fine.

    Cache failure is *not fatal*. If the cache is unavailable, this returns
    a raw nonce that was never registered — the scanner will still attempt
    the scan, and the DB will still reject a duplicate. We degrade to
    "no nonce audit trail" rather than "buyer can't get in".
    """
    if getattr(ticket, "pk", None) is None:
        raise ValueError("Cannot mint a nonce for an unsaved ticket.")

    raw = secrets.token_urlsafe(NONCE_BYTES)
    nonce_hash = hash_nonce(raw)
    expires_at = int(time.time() + NONCE_TTL_SECONDS)

    try:
        cache.set(
            _active_key(ticket.pk),
            (nonce_hash, expires_at),
            timeout=NONCE_TTL_SECONDS,
        )
    except Exception:                                     # noqa: BLE001
        # Never let a cache outage block ticket rendering.
        logger.exception(
            "Could not persist nonce for ticket %s; continuing.", ticket.pk,
        )

    return raw, expires_at


def classify_nonce(
    ticket_id: int, nonce: str,
) -> tuple[str, str]:
    """
    Classify a submitted nonce as one of:

      * ``("active", hash)`` — matches the currently active nonce
      * ``("stale", hash)``  — doesn't match, but the ticket has one active
      * ``("unknown", hash)`` — ticket has no active nonce (cache miss/expired)
      * ``("malformed", "")`` — empty or missing nonce

    Returns ``(state, nonce_hash)``. The scanner uses this for the audit
    row only — it never affects whether the ticket is consumed. The DB is
    the authority on consumption.
    """
    if not nonce:
        return "malformed", ""

    nonce_hash = hash_nonce(nonce)

    try:
        active = cache.get(_active_key(ticket_id))
    except Exception:                                     # noqa: BLE001
        logger.exception("Cache read failed for ticket %s nonce.", ticket_id)
        return "unknown", nonce_hash

    if not active or not isinstance(active, (tuple, list)) or len(active) != 2:
        return "unknown", nonce_hash

    active_hash, expires_at = active
    if not isinstance(active_hash, str) or not isinstance(expires_at, int):
        return "unknown", nonce_hash

    if expires_at <= time.time():
        return "unknown", nonce_hash

    return ("active" if active_hash == nonce_hash else "stale"), nonce_hash


def invalidate_nonce(ticket_id: int) -> None:
    """
    Drop the ticket's active nonce. Call on void, cancel, reissue.

    Best-effort: a cache failure here doesn't matter because the DB already
    rejects scans of a non-valid ticket.
    """
    try:
        cache.delete(_active_key(ticket_id))
    except Exception:                                     # noqa: BLE001
        logger.exception(
            "Could not invalidate nonce for ticket %s (cache error).",
            ticket_id,
        )


