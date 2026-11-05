"""Views for order verification and payment proof."""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from accounts.permissions import organizer_required
from events.models import Event
from .forms import PaymentProofForm
from .models import Order, OrderItem

logger = logging.getLogger(__name__)


# Statuses in which the buyer is *allowed* to (re)upload proof.
UPLOADABLE_STATUSES = {
    getattr(Order.Status, "PENDING_PAYMENT", "pending_payment"),
    getattr(Order.Status, "AWAITING_VERIFICATION", "awaiting_verification"),
    getattr(Order.Status, "REJECTED", "rejected"),
}


# ===========================================================================
# Helpers
# ===========================================================================
def _can_view_proof(user, order: Order) -> bool:
    """Buyer, event organizer, or staff may view the payment proof."""
    if user.is_staff:
        return True
    if order.buyer_id == user.pk:
        return True
    return OrderItem.objects.filter(
        order=order, tier__event__organizer=user,
    ).exists()


def _get_event_for_organizer(request: HttpRequest, slug: str) -> Event:
    """Fetch an event the current user may administer (owner or staff)."""
    event = get_object_or_404(Event, slug=slug)
    if event.organizer_id != request.user.id and not request.user.is_staff:
        raise Http404  # don't leak existence
    return event


def _get_order_for_event(event: Event, reference) -> Order:
    return get_object_or_404(
        Order, reference=reference, items__tier__event=event,
    )


# ===========================================================================
# Organizer: verification queue + approve / reject
# ===========================================================================
@organizer_required
def verify_orders(request: HttpRequest, slug: str) -> HttpResponse:
    event = _get_event_for_organizer(request, slug)

    pending = (
        Order.objects
        .filter(items__tier__event=event, status=Order.Status.PENDING)
        .prefetch_related("items__tier__event")
        .distinct()
        .order_by("created_at")
    )
    recent = (
        Order.objects
        .filter(
            items__tier__event=event,
            status__in=[
                Order.Status.PAID,
                Order.Status.REJECTED,
                Order.Status.REFUNDED,
            ],
        )
        .prefetch_related("items__tier__event")
        .distinct()
        .order_by("-paid_at", "-created_at")[:20]
    )

    return render(
        request,
        "orders/verify_orders.html",
        {"event": event, "pending": pending, "recent": recent},
    )


@organizer_required
@require_POST
def approve_order(request: HttpRequest, slug: str, reference) -> HttpResponse:
    event = _get_event_for_organizer(request, slug)
    order = _get_order_for_event(event, reference)

    try:
        issued = order.mark_paid(actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("orders:verify_orders", slug=event.slug)

    if not issued:
        messages.info(
            request,
            "This order was already marked as paid. No new tickets were issued.",
        )
        return redirect("orders:verify_orders", slug=event.slug)

    emailed = _send_ticket_emails(order, issued)
    if emailed == len(issued):
        messages.success(
            request,
            f"Order approved — {len(issued)} ticket(s) issued and emailed "
            f"to {order.email}.",
        )
    else:
        messages.warning(
            request,
            f"Order approved — {len(issued)} ticket(s) issued. "
            f"{emailed} of {len(issued)} emailed; the buyer can view them "
            f"from their account.",
        )
    return redirect("orders:verify_orders", slug=event.slug)


@organizer_required
@require_POST
def reject_order(request: HttpRequest, slug: str, reference) -> HttpResponse:
    event = _get_event_for_organizer(request, slug)
    order = _get_order_for_event(event, reference)

    try:
        order.mark_rejected(actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("orders:verify_orders", slug=event.slug)

    notified = _try_send_rejection_email(order)
    if notified:
        messages.success(
            request,
            f"Order rejected. {order.email} has been notified.",
        )
    else:
        messages.warning(
            request,
            f"Order rejected. The buyer ({order.email}) could not be "
            f"emailed — please notify them manually.",
        )
    return redirect("orders:verify_orders", slug=event.slug)


# ===========================================================================
# Payment proof: upload / inline view / download
# ===========================================================================
@login_required
@require_http_methods(["GET", "POST"])
def order_payment_proof(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Let the buyer attach (or replace) a payment proof for their own order.

    - Only the order's owner (or staff) can access this view.
    - Uploads are only accepted while the order is in an uploadable state.
    - On success the order moves to AWAITING_VERIFICATION.
    """
    order = get_object_or_404(Order, pk=pk)

    if order.buyer_id != request.user.pk and not request.user.is_staff:
        raise Http404  # don't leak existence of other users' orders

    if order.status not in UPLOADABLE_STATUSES:
        messages.error(
            request,
            "Payment proof can no longer be uploaded for this order "
            f"(status: {order.get_status_display()}).",
        )
        return redirect("tickets:my_orders")

    if request.method == "POST":
        form = PaymentProofForm(request.POST, request.FILES, instance=order)
        if form.is_valid():
            with transaction.atomic():
                order.attach_payment_proof(form.cleaned_data["payment_proof"])
                order.status = getattr(
                    Order.Status, "AWAITING_VERIFICATION", order.status,
                )
                order.save()
            messages.success(
                request,
                "Payment proof uploaded. We'll verify it shortly.",
            )
            return redirect("tickets:my_orders")
    else:
        form = PaymentProofForm(instance=order)

    return render(
        request,
        "tickets/order_payment_proof.html",
        {"order": order, "form": form},
    )

@login_required
def order_payment_proof_view(request: HttpRequest, pk: int) -> FileResponse:
    """Inline payment-proof viewer for buyer, event organizer, or staff."""
    order = get_object_or_404(Order, pk=pk)

    if not _can_view_proof(request.user, order):
        raise Http404
    if not order.payment_proof:
        raise Http404("Payment proof not found.")

    # The DB row can point at a path that no longer exists (manual edit,
    # wiped media dir, restored DB, etc.). Treat that as 404 — never leak
    # a filesystem traceback to the browser.
    if not order.payment_proof.storage.exists(order.payment_proof.name):
        logger.warning(
            "Payment proof file missing on disk: order=%s path=%s",
            order.pk, order.payment_proof.name,
        )
        raise Http404("Payment proof file is missing.")

    try:
        return FileResponse(
            order.payment_proof.open("rb"),
            as_attachment=False,
            filename=order.payment_proof_filename(),
            content_type=order.payment_proof_content_type or "application/octet-stream",
        )
    except FileNotFoundError:
        # Belt and braces: race between exists() and open().
        logger.warning(
            "Payment proof disappeared between exists() and open(): "
            "order=%s path=%s", order.pk, order.payment_proof.name,
        )
        raise Http404("Payment proof file is missing.")


@login_required
def download_payment_proof(request: HttpRequest, reference) -> FileResponse:
    """Force-download payment proof for buyer, event organizer, or staff."""
    order = get_object_or_404(Order, reference=reference)

    if not _can_view_proof(request.user, order):
        raise Http404
    if not order.payment_proof:
        raise Http404("Payment proof not found.")

    if not order.payment_proof.storage.exists(order.payment_proof.name):
        logger.warning(
            "Payment proof file missing on disk (download): order=%s path=%s",
            order.pk, order.payment_proof.name,
        )
        raise Http404("Payment proof file is missing.")

    try:
        return FileResponse(
            order.payment_proof.open("rb"),
            as_attachment=True,
            filename=order.payment_proof_filename(),
            content_type=order.payment_proof_content_type or "application/octet-stream",
        )
    except FileNotFoundError:
        logger.warning(
            "Payment proof disappeared between exists() and open() (download): "
            "order=%s path=%s", order.pk, order.payment_proof.name,
        )
        raise Http404("Payment proof file is missing.")

# ===========================================================================
# Email helpers (best-effort — never block the approval flow)
# ===========================================================================
def _send_ticket_emails(order: Order, issued) -> int:
    try:
        from tickets.emails import send_ticket_email
    except ImportError:
        return 0
    sent = 0
    for ticket in issued:
        try:
            send_ticket_email(ticket)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not email ticket %s for order %s", ticket.pk, order.pk,
            )
    return sent


def _try_send_rejection_email(order: Order) -> bool:
    try:
        from tickets.emails import send_rejection_email
    except ImportError:
        return False
    try:
        send_rejection_email(order)
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not send rejection email for order %s", order.pk,
        )
        return False