from __future__ import annotations

import base64
import hashlib
import hmac

from django.conf import settings

TOKEN_PREFIX = "EGT1"
SIG_LEN = 22               # base64url, no padding; ~132 bits of HMAC
MAX_PUBLIC_ID_LEN = 64
MAX_EVENT_ID = 2_147_483_647   # fits in 32-bit signed int
MAX_EPOCH = 4_102_444_800      # 2100-01-01, sanity bound


# ---------------------------------------------------------------------------
# Signing key
# ---------------------------------------------------------------------------
def _signing_key(version: int) -> bytes:
    """
    Return the HMAC key for ``version``.

    Must be configured explicitly — no silent fallback to ``SECRET_KEY``.
    A missing key is a misconfiguration, not a "use the session secret"
    situation: rotating ``SECRET_KEY`` should not invalidate every offline
    ticket, and vice versa.
    """
    keys = getattr(settings, "TICKET_SIGNING_KEYS", None)
    if not keys:
        raise RuntimeError(
            "TICKET_SIGNING_KEYS is not configured. This module is not "
            "used by the online scan path — either configure the keys or "
            "delete tickets/tokens.py."
        )
    try:
        key = keys[version]
    except KeyError:
        raise RuntimeError(
            f"TICKET_SIGNING_KEYS has no entry for version {version!r}."
        )
    return key.encode("utf-8") if isinstance(key, str) else key


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    """URL-safe base64, no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sig(payload: str, version: int) -> str:
    mac = hmac.new(
        _signing_key(version), payload.encode("utf-8"), hashlib.sha256,
    ).digest()
    return _b64(mac)[:SIG_LEN]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def make_token(
    *,
    public_id: str,
    event_id: int,
    issued_epoch: int,
    version: int = 1,
) -> str:
    if not public_id or len(public_id) > MAX_PUBLIC_ID_LEN or "." in public_id:
        raise ValueError("public_id must be non-empty, ≤64 chars, no dots.")
    if not isinstance(event_id, int) or not (0 < event_id <= MAX_EVENT_ID):
        raise ValueError("event_id must be a positive 32-bit integer.")
    if not isinstance(issued_epoch, int) or not (0 < issued_epoch <= MAX_EPOCH):
        raise ValueError("issued_epoch must be a plausible Unix timestamp.")

    payload = f"{TOKEN_PREFIX}|{public_id}|{event_id}|{issued_epoch}"
    return (
        f"{TOKEN_PREFIX}.{public_id}.{event_id}.{issued_epoch}"
        f".{_sig(payload, version)}"
    )


def parse_token(raw: str) -> dict | None:
    """
    Parse a token into claims, or ``None`` on any structural problem.

    Does NOT verify the signature — call ``verify_token`` after loading the
    ticket (to learn its key version) or pass a version you trust for
    offline verification.
    """
    parts = (raw or "").strip().split(".")
    if len(parts) != 5:
        return None
    prefix, public_id, event_id_s, issued_epoch_s, sig = parts
    if (
        prefix != TOKEN_PREFIX
        or not public_id
        or len(public_id) > MAX_PUBLIC_ID_LEN
        or not event_id_s.isdigit()
        or not issued_epoch_s.isdigit()
        or not sig
    ):
        return None

    event_id = int(event_id_s)
    issued_epoch = int(issued_epoch_s)
    if not (0 < event_id <= MAX_EVENT_ID):
        return None
    if not (0 < issued_epoch <= MAX_EPOCH):
        return None

    return {
        "public_id": public_id,
        "event_id": event_id,
        "issued_epoch": issued_epoch,
        "sig": sig,
        "payload": f"{TOKEN_PREFIX}|{public_id}|{event_id}|{issued_epoch}",
    }


def verify_token(claims: dict, version: int) -> bool:
    """Constant-time HMAC verification for the given key version."""
    expected = _sig(claims["payload"], version)
    return hmac.compare_digest(expected, claims["sig"])


def token_hash(raw: str) -> str:
    """Stable SHA-256 hex digest — usable as a DB index / audit key."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()