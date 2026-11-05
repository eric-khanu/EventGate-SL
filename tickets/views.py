"""Views for checkout, ticket display, scanning, and scanner stats."""

from __future__ import annotations

import json
import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import TruncDate
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import organizer_required, scanner_required
from events.models import Event, TicketTier
from events.permissions import can_scan, scanner_event_ids
from orders.models import Order, OrderItem
from tickets.models import Ticket, TicketTokenError
from tickets.qr import try_qr_png_data_uri
from tickets.security import build_security_context
from tickets.services import validate_and_consume

from .forms import CheckoutForm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_DEVICE_ID_LEN = 100
MAX_QR_PAYLOAD_BYTES = 2048

# Must mirror ``tickets.services`` result codes. Missing a code silently
# downgrades a specific status (e.g. 409 STALE_QR) to the 400 fallback.
_SCAN_STATUS_MAP: dict[str, int] = {
    "USED": 200,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "WRONG_EVENT": 409,
    "ALREADY_USED": 409,
    "STALE_QR": 409,
    "VOID": 410,
    "EXPIRED": 410,
    "TOO_EARLY": 425,
    "EVENT_ENDED": 410,
    "EVENT_INACTIVE": 410,
    "RATE_LIMITED": 429,
    "INVALID": 400,
    "BAD_PAYLOAD": 400,
    "SERVER_ERROR": 500,
}


class TicketUnavailable(Exception):
    """Raised when ticket inventory is no longer available."""


# ===========================================================================
# Checkout
# ===========================================================================
@login_required
def checkout(request: HttpRequest, tier_id: int) -> HttpResponse:
    """Create a ticket order after validating payment and inventory."""
    tier = get_object_or_404(
        TicketTier.objects.select_related("event"),
        pk=tier_id,
        event__status=Event.Status.PUBLISHED,
        is_active=True,
    )

    if request.method == "POST":
        form = CheckoutForm(
            request.POST, request.FILES, tier=tier, user=request.user,
        )
        if not form.is_valid():
            return render(
                request, "tickets/checkout.html",
                {"tier": tier, "form": form},
            )

        try:
            _create_order(request.user, tier, form.cleaned_data)
        except TicketUnavailable:
            messages.error(
                request,
                "Sorry, these tickets are no longer available. "
                "Please select another quantity or ticket tier.",
            )
            return redirect("events:event_detail", slug=tier.event.slug)
        except ValidationError:
            logger.warning(
                "Payment proof validation failed during checkout "
                "for user %s and tier %s", request.user.pk, tier.pk,
            )
            messages.error(
                request,
                "The payment proof could not be accepted. "
                "Please upload a valid file.",
            )
            return redirect("tickets:checkout", tier_id=tier.pk)
        except Exception:
            logger.exception(
                "Checkout failed for user %s and tier %s",
                request.user.pk, tier.pk,
            )
            messages.error(
                request,
                "We could not complete your order. Please try again.",
            )
            return redirect("events:event_detail", slug=tier.event.slug)

        messages.success(
            request,
            "Your order has been created. Tickets will be issued "
            "after the organiser verifies your payment.",
        )
        return redirect("tickets:my_orders")

    form = CheckoutForm(tier=tier, user=request.user)
    return render(
        request, "tickets/checkout.html",
        {"tier": tier, "form": form},
    )


@transaction.atomic
def _create_order(buyer, tier: TicketTier, data: dict[str, Any]) -> Order:
    """
    Atomic order-creation core. Locks the tier, reserves stock, persists the
    order + line item, attaches the payment proof, returns the order.

    Any exception propagates and rolls back the whole transaction.
    """
    locked_tier = (
        TicketTier.objects
        .select_for_update()
        .select_related("event")
        .get(pk=tier.pk)
    )

    quantity = data["quantity"]
    if not locked_tier.on_sale or locked_tier.remaining < quantity:
        raise TicketUnavailable

    unit_price = locked_tier.current_price
    total = unit_price * quantity

    if not locked_tier.reserve(quantity):
        raise TicketUnavailable

    order = Order.objects.create(
        buyer=buyer,
        email=data["email"],
        phone=data["phone"],
        currency=locked_tier.event.currency,
        total=total,
        payment_method=data["payment_method"],
        payment_reference=data.get("payment_reference", ""),
        payment_note=data.get("payment_note", ""),
    )

    OrderItem.objects.create(
        order=order,
        tier=locked_tier,
        quantity=quantity,
        unit_price=unit_price,
        line_total=total,
    )

    proof = data.get("payment_proof")
    if proof:
        order.attach_payment_proof(proof)

    return order


# ===========================================================================
# Buyer views
# ===========================================================================
@login_required
def my_orders(request: HttpRequest) -> HttpResponse:
    """Display orders belonging exclusively to the current buyer."""
    orders = (
        Order.objects
        .filter(buyer=request.user)
        .prefetch_related("items__tier__event", "items__tickets")
        .order_by("-created_at")
    )
    return render(request, "tickets/my_orders.html", {"orders": orders})


@login_required
def ticket_detail(request: HttpRequest, public_id: str) -> HttpResponse:
    """
    Display a ticket, its QR code, and its security features to its owner.

    The QR is rendered from ``ticket`` alone — the raw token is not passed
    to the QR module anymore (the payload signs only ``token_hash``). Do
    NOT call ``ticket.get_raw_token()`` here: that method triggers a lazy
    re-encryption on key rotation, which would invalidate every previously
    captured screenshot the moment its owner views the page.
    """
    ticket = get_object_or_404(
        Ticket.objects.select_related("order_item__tier__event"),
        public_id=public_id,
        owner=request.user,
    )

    qr: str | None = None
    if ticket.status == Ticket.Status.VALID:
        try:
            qr = try_qr_png_data_uri(ticket)
        except TicketTokenError:
            qr = None

    return render(
        request, "tickets/ticket_detail.html",
        {
            "ticket": ticket,
            "qr": qr,
            # Nested context (label / value / help per feature); the
            # template reads ``security.fingerprint.value`` etc.
            "security": build_security_context(ticket),
        },
    )


# ===========================================================================
# Scanner
# ===========================================================================
def _scan_scope_qs(user) -> QuerySet[Ticket]:
    """
    Queryset of tickets a user may see in the scanner UI / stats.

    - Staff/superusers: everything.
    - Everyone else: tickets for events they're assigned to as a scanner,
      **plus** events they organize (matches ``can_scan``).
    """
    qs = Ticket.objects.all()
    if user.is_staff or user.is_superuser:
        return qs

    assigned_ids = scanner_event_ids(user)
    return qs.filter(
        Q(order_item__tier__event__in=assigned_ids)
        | Q(order_item__tier__event__organizer=user)
    )


@scanner_required
def scanner(request: HttpRequest) -> HttpResponse:
    """Scanner UI: stats, recent scans, and the event dropdown."""
    if request.user.is_staff or request.user.is_superuser:
        events_qs = Event.objects.filter(status=Event.Status.PUBLISHED)
    else:
        assigned_ids = scanner_event_ids(request.user)
        events_qs = (
            Event.objects
            .filter(status=Event.Status.PUBLISHED)
            .filter(Q(pk__in=assigned_ids) | Q(organizer=request.user))
            .distinct()
        )

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    scanned_qs = _scan_scope_qs(request.user).filter(used_at__isnull=False)
    if not (request.user.is_staff or request.user.is_superuser):
        # A scanner sees only what they themselves scanned. Organizers see
        # only their own scans too (not every scanner's), matching the "this
        # is your audit trail" framing.
        scanned_qs = scanned_qs.filter(used_by=request.user)

    return render(
        request, "tickets/scanner.html",
        {
            "events": events_qs.order_by("starts_at"),
            "now": now,
            "today_scanned": scanned_qs.filter(used_at__gte=today_start).count(),
            "total_scanned": scanned_qs.count(),
            "recent": (
                scanned_qs
                .select_related("order_item__tier__event")
                .order_by("-used_at")[:25]
            ),
        },
    )


@scanner_required
def scanner_stats(request: HttpRequest) -> HttpResponse:
    """Dedicated scanner statistics page with per-event / per-day breakdowns."""
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    used_qs = _scan_scope_qs(request.user).filter(used_at__isnull=False)
    if not (request.user.is_staff or request.user.is_superuser):
        used_qs = used_qs.filter(used_by=request.user)

    per_event = (
        used_qs
        .values(
            "order_item__tier__event__title",
            "order_item__tier__event__slug",
            "order_item__tier__event__starts_at",
        )
        .annotate(count=Count("pk"))
        .order_by("-order_item__tier__event__starts_at")
    )

    per_day = (
        used_qs
        .annotate(day=TruncDate("used_at"))
        .values("day")
        .annotate(count=Count("pk"))
        .order_by("-day")[:14]
    )

    return render(
        request, "tickets/scanner_stats.html",
        {
            "now": now,
            "total_scanned": used_qs.count(),
            "today_scanned": used_qs.filter(used_at__gte=today_start).count(),
            "per_event": per_event,
            "per_day": per_day,
            "recent": (
                used_qs
                .select_related("order_item__tier__event")
                .order_by("-used_at")[:50]
            ),
        },
    )


@scanner_required
@require_POST
def scanner_scan(request: HttpRequest) -> JsonResponse:
    """
    Validate and consume a ticket through the scanner API.

    The scanner UI POSTs a JSON body:
        {"event": "<event-id>", "qr": "<raw QR string>", "device_id": "<id>"}
    """
    payload, error = _parse_scan_payload(request)
    if error is not None:
        return error

    event = _resolve_scan_event(request.user, payload["event"])
    if event is None:
        return _json_error(
            "FORBIDDEN",
            "You do not have permission to scan tickets for this event.",
            403,
        )

    try:
        result = validate_and_consume(
            request=request,        # ← used for IP / UA in ScanAttempt
            raw_qr_data=payload["qr"],
            scanner=request.user,
            event=event,
            device_id=payload["device_id"],
        )
    except Exception:
        logger.exception(
            "Scanner validation failed for event %s", event.pk,
        )
        return _json_error(
            "SERVER_ERROR", "Scanner error. Please try again.", 500,
        )

    http_status = (
        200 if result.get("ok")
        else _SCAN_STATUS_MAP.get(result.get("code"), 400)
    )
    return JsonResponse(result, status=http_status)


def _parse_scan_payload(
    request: HttpRequest,
) -> tuple[dict[str, str], JsonResponse | None]:
    """Decode and validate the scanner POST body."""
    try:
        raw = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}, _json_error("BAD_PAYLOAD", "Malformed scan payload.", 400)

    if not isinstance(raw, dict):
        return {}, _json_error("BAD_PAYLOAD", "Invalid scan payload.", 400)

    qr = str(raw.get("qr") or "").strip()
    device_id = str(raw.get("device_id") or "").strip()
    event_id = str(raw.get("event") or "").strip()

    if (
        not qr
        or len(qr.encode("utf-8")) > MAX_QR_PAYLOAD_BYTES
        or len(device_id) > MAX_DEVICE_ID_LEN
        or not event_id.isdigit()
    ):
        return {}, _json_error("BAD_PAYLOAD", "Invalid scan payload.", 400)

    return {"qr": qr, "device_id": device_id, "event": event_id}, None


def _resolve_scan_event(user, event_id: str) -> Event | None:
    """
    Return the event or ``None`` — indistinguishable from "doesn't exist"
    so we don't leak which events exist.
    """
    event = Event.objects.filter(
        pk=event_id, status=Event.Status.PUBLISHED,
    ).first()
    if event is None:
        return None
    return event if can_scan(user, event) else None


def _json_error(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "code": code, "message": message}, status=status,
    )


# ===========================================================================
# Organizer sales
# ===========================================================================
@organizer_required
def organizer_sales(request: HttpRequest) -> HttpResponse:
    """Display sales for events owned by the organizer, or all for staff."""
    orders = Order.objects.prefetch_related("items__tier__event")
    if not request.user.is_staff:
        orders = orders.filter(
            items__tier__event__organizer=request.user,
        ).distinct()
    orders = orders.order_by("-created_at")
    return render(request, "tickets/sales.html", {"orders": orders})


# ===========================================================================
# Health check
# ===========================================================================
#: Cached sentinel set by the app on first successful health check. A health
#: check that writes the key it's about to read proves only that the cache
#: accepts writes — it tells you nothing about whether *application* cache
#: keys are being populated.
_HEALTHZ_CACHE_KEY = "eventgrid:healthz:cache-ok"


def healthz(request: HttpRequest) -> JsonResponse:
    """
    Liveness + readiness in one. Returns 200 only if the DB and cache respond.

    No authentication — this is a load-balancer probe, not a status page.
    It must not leak any internal state: no version strings, no error details.

    The cache probe is a **read** of a sentinel that some other code path
    sets. That's the only way a health check can tell "cache is up" from
    "cache is up *and* application traffic is populating it." Writing a
    sentinel then reading it, as the previous version did, proves only
    that the backend accepts writes — not that any real key survives.
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:                                 # noqa: BLE001
        db_ok = False

    cache_ok = True
    try:
        cache_ok = cache.get(_HEALTHZ_CACHE_KEY) is not None
    except Exception:                                 # noqa: BLE001
        cache_ok = False

    healthy = db_ok and cache_ok
    return JsonResponse({"ok": healthy}, status=200 if healthy else 503)