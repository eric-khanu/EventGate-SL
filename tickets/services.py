from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from events.permissions import can_scan
from .models import ScanAttempt, Ticket, TicketStateError
from .qr import QRValidationError, decode_and_verify_qr_payload

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser
    from django.http import HttpRequest
    from events.models import Event


logger = logging.getLogger(__name__)


# Minutes before ``starts_at`` when scanning becomes allowed.
SCAN_WINDOW_MINUTES = 120

# Per-ticket rate limit (successful and failed scans both count).
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_ATTEMPTS = 5


# ===========================================================================
# Result codes — used by views to map to HTTP statuses
# ===========================================================================
CODE_USED = "USED"
CODE_ALREADY_USED = "ALREADY_USED"
CODE_VOID = "VOID"
CODE_EXPIRED = "EXPIRED"
CODE_INVALID = "INVALID"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_WRONG_EVENT = "WRONG_EVENT"
CODE_STALE_QR = "STALE_QR"
CODE_TOO_EARLY = "TOO_EARLY"
CODE_EVENT_ENDED = "EVENT_ENDED"
CODE_EVENT_INACTIVE = "EVENT_INACTIVE"
CODE_FORBIDDEN = "FORBIDDEN"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_SERVER_ERROR = "SERVER_ERROR"


# ===========================================================================
# ScanAttempt logging — best-effort, never masks the primary result
# ===========================================================================
def _log_attempt(
    request: "HttpRequest | None",
    *,
    event: "Event",
    scanner: "AbstractBaseUser",
    ticket: "Ticket | None",
    result: str,
    detail: str = "",
    raw_token: str = "",
) -> None:
    """Write a ScanAttempt row. Never raises into the scan path."""
    try:
        ip = None
        ua = ""
        if request is not None:
            ip = request.META.get("REMOTE_ADDR")
            ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]
        ScanAttempt.objects.create(
            event=event,
            ticket=ticket,
            scanner=scanner,
            result=result,
            ok=(result == ScanAttempt.Result.USED),
            device_id="",
            ip=ip or None,
            user_agent=ua,
            nonce_state="",
            nonce_hash="",
            raw_token_hash=Ticket.hash_token(raw_token) if raw_token else "",
        )
    except Exception:                                     # noqa: BLE001
        logger.exception(
            "Could not write ScanAttempt (event=%s, ticket=%s, result=%s)",
            getattr(event, "pk", None),
            getattr(ticket, "pk", None),
            result,
        )


def _too_many_attempts(ticket: "Ticket") -> bool:
    """True if this ticket has been attempted too often recently."""
    since = timezone.now() - timezone.timedelta(
        seconds=RATE_LIMIT_WINDOW_SECONDS
    )
    return (
        ScanAttempt.objects
        .filter(ticket=ticket, created_at__gte=since)
        .count() >= RATE_LIMIT_MAX_ATTEMPTS
    )


# ===========================================================================
# The one and only validate_and_consume
# ===========================================================================
def validate_and_consume(
    *,
    request: "HttpRequest | None" = None,
    raw_qr_data: str,
    scanner: "AbstractBaseUser",
    event: "Event",
    device_id: str = "",
) -> dict[str, Any]:
    """
    Validate a QR payload and consume the ticket.

    Returns a plain dict suitable for ``JsonResponse``. ``ok=True`` only on
    a fresh, successful scan. Every other path has a distinct ``code`` so
    the scanner UI can render an actionable message.

    ``request`` is optional — pass it to record IP / User-Agent in
    ``ScanAttempt``. Passing ``None`` is fine for tests and management
    commands.
    """
    raw = (raw_qr_data or "").strip()

    # ---- 1. Decode + verify HMAC --------------------------------------
    if not raw:
        _log_attempt(
            request, event=event, scanner=scanner, ticket=None,
            result=ScanAttempt.Result.INVALID,
        )
        return {"ok": False, "code": CODE_INVALID,
                "message": "Empty ticket payload."}

    try:
        claims = decode_and_verify_qr_payload(raw)
    except QRValidationError as exc:
        _log_attempt(
            request, event=event, scanner=scanner, ticket=None,
            result=ScanAttempt.Result.INVALID,
            detail=str(exc)[:240], raw_token=raw,
        )
        return {"ok": False, "code": CODE_INVALID,
                "message": "Invalid or forged ticket QR."}

    # ---- 2–9. All DB work inside one transaction ----------------------
    with transaction.atomic():
        try:
            ticket = (
                Ticket.objects
                .select_for_update()
                .select_related("order_item__tier__event", "owner")
                .get(token_hash=claims["h"])
            )
        except Ticket.DoesNotExist:
            _log_attempt(
                request, event=event, scanner=scanner, ticket=None,
                result=ScanAttempt.Result.NOT_FOUND, raw_token=raw,
            )
            return {"ok": False, "code": CODE_NOT_FOUND,
                    "message": "Ticket not recognised."}

        # ---- 4. Stale-QR check (key version rotated after this QR signed) --
        if ticket.token_key_version != claims["kv"]:
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.INVALID, raw_token=raw,
            )
            return {
                "ok": False, "code": CODE_STALE_QR,
                "message": "This QR is out of date. Ask the ticket holder "
                           "to refresh their ticket screen.",
            }

        # ---- 5. Event scope ------------------------------------------
        if ticket.order_item.tier.event_id != event.pk:
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.WRONG_EVENT, raw_token=raw,
            )
            return {"ok": False, "code": CODE_WRONG_EVENT,
                    "message": "This ticket is for a different event."}

        # ---- 6. Scanner authorization --------------------------------
        if not can_scan(scanner, ticket.event):
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.INVALID, raw_token=raw,
            )
            return {"ok": False, "code": CODE_FORBIDDEN,
                    "message": "You are not authorised to scan this event."}

        # ---- 7. Event window / status --------------------------------
        ev = ticket.event
        now = timezone.now()

        if ev.status != ev.Status.PUBLISHED:
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.EVENT_INACTIVE, raw_token=raw,
            )
            return {"ok": False, "code": CODE_EVENT_INACTIVE,
                    "message": "This event is not currently active."}

        scan_opens = ev.starts_at - timezone.timedelta(minutes=SCAN_WINDOW_MINUTES)
        if now < scan_opens:
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.TOO_EARLY, raw_token=raw,
            )
            return {
                "ok": False, "code": CODE_TOO_EARLY,
                "message": f"Scanning opens at {scan_opens:%d %b %H:%M}.",
            }
        if now > ev.ends_at:
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.EVENT_ENDED, raw_token=raw,
            )
            return {"ok": False, "code": CODE_EVENT_ENDED,
                    "message": "This event has ended."}

        # ---- 8. Rate limit (per ticket, short window) ----------------
        if _too_many_attempts(ticket):
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=ScanAttempt.Result.RATE_LIMITED, raw_token=raw,
            )
            return {
                "ok": False, "code": CODE_RATE_LIMITED,
                "message": "This ticket has been scanned too many times "
                           "recently. Wait a minute and try again.",
            }

        # ---- 9. Consume: conditional UPDATE inside the row lock ------
        try:
            ticket.mark_used(
                scanner=scanner, device_id=device_id, now=now,
            )
        except TicketStateError:
            # Someone else consumed it between our SELECT and UPDATE,
            # or the ticket is VOID.
            current = ticket.status
            result_code = (
                CODE_ALREADY_USED
                if current == Ticket.Status.USED
                else CODE_VOID
            )
            log_result = (
                ScanAttempt.Result.ALREADY_USED
                if current == Ticket.Status.USED
                else ScanAttempt.Result.VOID
            )
            _log_attempt(
                request, event=event, scanner=scanner, ticket=ticket,
                result=log_result, raw_token=raw,
            )
            return {
                "ok": False,
                "code": result_code,
                "message": (
                    "This ticket has already been scanned."
                    if current == Ticket.Status.USED
                    else "This ticket has been voided."
                ),
            }

        # ---- 10. Audit row (inside the transaction — atomically
        #          committed with the state flip) -----------------------
        _log_attempt(
            request, event=event, scanner=scanner, ticket=ticket,
            result=ScanAttempt.Result.USED, raw_token=raw,
        )


    # At the very end of validate_and_consume:
    return {
        "ok": True,
        "code": CODE_USED,
        # Keep the server message detailed, but JS will override the main headline
        "message": f"Valid: {ticket.order_item.tier.name} — {ticket.owner.username}",
        "ticket": {
            "public_id": str(ticket.public_id),
            "tier": ticket.order_item.tier.name,
            "owner": ticket.owner.get_full_name() or ticket.owner.username, # <-- Added this
            "status": ticket.status,
            "used_at": now.isoformat(),
        },
        "event": {"id": ev.pk, "title": ev.title},
    }