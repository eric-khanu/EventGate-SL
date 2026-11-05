"""
tickets/qr.py
~~~~~~~~~~~~~

Secure QR payload generation and authentication for EventGate.

What a QR is
------------
A QR is a **pointer + signature**. It identifies a specific ticket by the
SHA-256 hash of its encrypted token, and it authenticates that pointer with
an HMAC keyed by ``settings.TICKET_QR_SIGNING_KEY``.

What a QR is NOT
----------------
A QR is *not* a bearer credential in the cryptographic sense. It carries
a hash that identifies the ticket; the server does the work of checking
whether the ticket is valid, unused, and for the right event.

Because a QR is public the moment it's rendered, anyone who photographs
it has the same pointer + signature. **Duplication is prevented by the
database**, not by the payload:

  - ``Ticket.mark_used()`` conditionally updates
    ``WHERE status='valid'`` — the second scan raises ``TicketStateError``.
  - ``ScanAttempt`` records every attempt, so suspicious patterns (many
    attempts on one ticket within seconds) are detectable.

The signing key is **separate** from ``SECRET_KEY``, so rotating
``SECRET_KEY`` doesn't invalidate outstanding QRs and vice versa. There is
no fallback list for acceptance: an old-signed payload is rejected. Since
QRs are short-lived (minted at ticket render), a rotation costs at most
"buyers refresh the page."

Payload:
    {
        "v": 1,
        "t": "<ticket_public_id>",
        "h": "<sha256(raw_token)>",     # pointer, NOT the raw credential
        "kv": <token_key_version>,      # binds to ciphertext version
        "s": "<hmac-sha256>"
    }
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import qrcode
from django.conf import settings
from django.utils.crypto import salted_hmac
from qrcode.constants import ERROR_CORRECT_H
from qrcode.image.pil import PilImage

if TYPE_CHECKING:
    from tickets.models import Ticket

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

PAYLOAD_VERSION = 1

DEFAULT_BOX_SIZE = 12
DEFAULT_BORDER = 4
DEFAULT_ERROR_CORRECTION = ERROR_CORRECT_H

MAX_TOKEN_HASH_LEN = 64
MAX_PUBLIC_ID_LEN = 64
MAX_PAYLOAD_BYTES = 1024           # was 2048; smaller payload = faster scan

SIGNATURE_SALT = "eventgate.tickets.qr.payload.v1"

SIGNED_FIELDS = ("v", "t", "h", "kv")


class QRGenerationError(Exception):
    """Raised when a ticket QR code cannot be generated."""


class QRValidationError(Exception):
    """Raised when a QR payload fails validation."""


# ============================================================================
# Signing key — separate from SECRET_KEY, no fallbacks
# ============================================================================

def _signing_key() -> str:
    """
    The key used to sign QR payloads.

    Deliberately *not* ``SECRET_KEY`` — rotating ``SECRET_KEY`` (session,
    CSRF, password reset tokens) should not invalidate every outstanding
    QR on the floor. Set ``TICKET_QR_SIGNING_KEY`` in settings. Refuse to
    fall back to ``SECRET_KEY`` so a misconfigured environment fails loudly
    rather than silently sharing a signing key with unrelated features.
    """
    key = getattr(settings, "TICKET_QR_SIGNING_KEY", None)
    if not key:
        raise QRGenerationError(
            "TICKET_QR_SIGNING_KEY is not configured. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(64))'` "
            "and add it to settings."
        )
    return key


# ============================================================================
# Input validation
# ============================================================================

def _validate_ticket(ticket: "Ticket") -> tuple[str, str, int]:
    """Return ``(public_id, token_hash, key_version)`` from a valid ticket."""
    if getattr(ticket, "status", None) != "valid":
        raise QRGenerationError(
            "Cannot generate a QR code for a non-valid ticket."
        )
    public_id = str(getattr(ticket, "public_id", "") or "")
    if not public_id or len(public_id) > MAX_PUBLIC_ID_LEN:
        raise QRGenerationError("Ticket public ID is invalid.")
    token_hash = getattr(ticket, "token_hash", "")
    if not token_hash or len(token_hash) > MAX_TOKEN_HASH_LEN:
        raise QRGenerationError("Ticket token hash is invalid.")
    key_version = getattr(ticket, "token_key_version", 0)
    if not isinstance(key_version, int) or key_version <= 0:
        raise QRGenerationError("Ticket key version is invalid.")
    return public_id, token_hash, key_version


def _validate_render_options(box_size: int, border: int) -> None:
    if isinstance(box_size, bool) or not isinstance(box_size, int):
        raise QRGenerationError("QR box size must be an integer.")
    if not 6 <= box_size <= 30:
        raise QRGenerationError("QR box size must be between 6 and 30.")
    if isinstance(border, bool) or not isinstance(border, int):
        raise QRGenerationError("QR border must be an integer.")
    if not 4 <= border <= 10:
        raise QRGenerationError("QR border must be between 4 and 10.")


# ============================================================================
# Canonical payload and HMAC signing
# ============================================================================

def _canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(
        data,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")


def _sign(body: dict[str, Any]) -> str:
    message = _canonical_json(body).decode("utf-8")
    return salted_hmac(
        SIGNATURE_SALT,
        message,
        secret=_signing_key(),
        algorithm="sha256",
    ).hexdigest()


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    return {f: payload[f] for f in SIGNED_FIELDS}


# ============================================================================
# Payload generation
# ============================================================================

def qr_payload(ticket: "Ticket") -> str:
    """
    Build the authenticated QR payload for a ticket.

    The raw token is **not** included. Only its SHA-256 hash appears, which
    is the same value ``Ticket.token_hash`` indexes on. A QR therefore does
    not leak the raw token even to anyone who photographs it.
    """
    public_id, token_hash, key_version = _validate_ticket(ticket)

    body = {
        "v": PAYLOAD_VERSION,
        "t": public_id,
        "h": token_hash,
        "kv": key_version,
    }
    body["s"] = _sign(body)

    encoded = _canonical_json(body)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise QRGenerationError("QR payload exceeds the permitted size.")

    return encoded.decode("utf-8")


# ============================================================================
# Payload verification
# ============================================================================

def verify_qr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise QRValidationError("Invalid QR payload.")

    required = {*SIGNED_FIELDS, "s"}
    if set(payload) != required:
        raise QRValidationError("Invalid QR payload structure.")

    if payload["v"] != PAYLOAD_VERSION:
        raise QRValidationError("Unsupported QR payload version.")

    public_id = payload["t"]
    token_hash = payload["h"]
    key_version = payload["kv"]
    signature = payload["s"]

    if not isinstance(public_id, str) or not public_id or len(public_id) > MAX_PUBLIC_ID_LEN:
        raise QRValidationError("Invalid ticket reference.")
    if not isinstance(token_hash, str) or len(token_hash) != MAX_TOKEN_HASH_LEN:
        raise QRValidationError("Invalid ticket pointer.")
    if not isinstance(key_version, int) or isinstance(key_version, bool) or key_version <= 0:
        raise QRValidationError("Invalid ticket key version.")
    if not isinstance(signature, str) or len(signature) != 64:
        raise QRValidationError("Invalid QR signature.")

    body = _body(payload)
    expected = _sign(body)
    if not hmac.compare_digest(expected, signature):
        raise QRValidationError("QR signature verification failed.")

    return {
        "v": PAYLOAD_VERSION,
        "t": public_id,
        "h": token_hash,
        "kv": key_version,
    }


def decode_and_verify_qr_payload(raw_payload: str) -> dict[str, Any]:
    if not isinstance(raw_payload, str):
        raise QRValidationError("Invalid QR data.")
    if len(raw_payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise QRValidationError("QR data exceeds the permitted size.")
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise QRValidationError("QR data is not valid JSON.") from exc
    return verify_qr_payload(payload)


# ============================================================================
# QR rendering
# ============================================================================

def _build_qr(payload: str, *, box_size: int, border: int, error_correction: int) -> qrcode.QRCode:
    qr = qrcode.QRCode(
        version=None,
        error_correction=error_correction,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    return qr


def qr_png_bytes(
    ticket: "Ticket",
    *,
    box_size: int = DEFAULT_BOX_SIZE,
    border: int = DEFAULT_BORDER,
    error_correction: int = DEFAULT_ERROR_CORRECTION,
) -> bytes:
    _validate_ticket(ticket)
    _validate_render_options(box_size, border)

    try:
        payload = qr_payload(ticket)
        qr = _build_qr(
            payload,
            box_size=box_size,
            border=border,
            error_correction=error_correction,
        )
        image: PilImage = qr.make_image(
            image_factory=PilImage,
            fill_color="black",
            back_color="white",
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except QRGenerationError:
        raise
    except Exception as exc:                                  # noqa: BLE001
        logger.exception(
            "QR generation failed for ticket %s",
            getattr(ticket, "public_id", "?"),
        )
        raise QRGenerationError("Could not generate the ticket QR code.") from exc


def qr_png_data_uri(
    ticket: "Ticket",
    *,
    box_size: int = DEFAULT_BOX_SIZE,
    border: int = DEFAULT_BORDER,
    error_correction: int = DEFAULT_ERROR_CORRECTION,
) -> str:
    png = qr_png_bytes(
        ticket,
        box_size=box_size,
        border=border,
        error_correction=error_correction,
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def try_qr_png_data_uri(ticket: "Ticket") -> str | None:
    try:
        return qr_png_data_uri(ticket)
    except QRGenerationError:
        logger.warning(
            "QR unavailable for ticket %s",
            getattr(ticket, "public_id", "?"),
        )
        return None