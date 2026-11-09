from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import (
    Count, IntegerField, OuterRef, Q, Subquery, Sum,
)
from django.db.models.functions import Coalesce
from django.forms import inlineformset_factory
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import has_scanner_role, organizer_required
from orders.models import Order

from .forms import EventForm, EventVideoForm, TierForm, AddScannerForm
from .models import Event, EventScanner, EventVideo, TicketTier
from .permissions import can_manage_scanners

User = get_user_model()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public browsing
# ---------------------------------------------------------------------------
def home(request: HttpRequest) -> HttpResponse:
    """
    Landing page: published, upcoming events with optional filters.

    Filters come from ``GET`` params (``q``, ``district``, ``category``) and
    are applied additively. Results are paginated so an anonymous client
    can't drag the whole table through a prefetch.
    """
    qs = (
        Event.objects
        .filter(status=Event.Status.PUBLISHED, starts_at__gte=timezone.now())
        .select_related("organizer")
        .prefetch_related("ticket_tiers")
        .order_by("starts_at")
    )

    q        = request.GET.get("q", "").strip()
    district = request.GET.get("district", "").strip()
    category = request.GET.get("category", "").strip()

    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(venue__icontains=q)
            | Q(district__icontains=q)
        )
    if district:
        qs = qs.filter(district__iexact=district)
    if category:
        qs = qs.filter(category__iexact=category)

    page = Paginator(qs, per_page=24).get_page(request.GET.get("page"))

    return render(request, "events/home.html", {
        "events": page,
        "page": page,
        "q": q,
        "district": district,
        "category": category,
    })


def event_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Public detail page: event info, videos, and purchasable tiers."""
    event = get_object_or_404(
        Event.objects
        .select_related("organizer")
        .prefetch_related("ticket_tiers", "videos"),
        slug=slug,
        status=Event.Status.PUBLISHED,
    )
    # ``on_sale`` calls ``timezone.now()`` per tier — one call is enough.
    tiers = [t for t in event.ticket_tiers.all() if t.on_sale]

    return render(request, "events/detail.html", {
        "event": event,
        "tiers": tiers,
    })


# ---------------------------------------------------------------------------
# Organizer dashboard
# ---------------------------------------------------------------------------
@organizer_required
def organizer_dashboard(request: HttpRequest) -> HttpResponse:
    """
    Overview for the logged-in organizer: their events + revenue.

    - Per-event aggregate counts come from annotations on ``ticket_tiers``.
    - Pending-order count is a correlated subquery so it doesn't multiply
      the tier sums.
    - Revenue is reported **per currency**. Never sum SLE + USD.
    """
    pending_sq = (
        Order.objects
        .filter(
            items__tier__event_id=OuterRef("pk"),
            status=Order.Status.PENDING,
        )
        .values("items__tier__event_id")
        .annotate(c=Count("pk", distinct=True))
        .values("c")
    )

    events = (
        Event.objects
        .filter(organizer=request.user)
        .annotate(
            tickets_created=Coalesce(
                Sum("ticket_tiers__quantity"), 0,
                output_field=IntegerField(),
            ),
            tickets_sold=Coalesce(
                Sum("ticket_tiers__sold"), 0,
                output_field=IntegerField(),
            ),
            tier_count=Count("ticket_tiers", distinct=True),
            pending_order_count=Coalesce(
                Subquery(pending_sq[:1]), 0,
                output_field=IntegerField(),
            ),
        )
        .order_by("-starts_at")
    )

    paid_totals = list(
        Order.objects
        .filter(
            items__tier__event__organizer=request.user,
            status=Order.Status.PAID,
        )
        .values("currency")
        .annotate(count=Count("pk", distinct=True), total=Sum("total"))
        .order_by("-total")
    )

    events_list = list(events)

    context = {
        "events": events_list,
        "paid_totals": paid_totals,
        "paid_orders": sum(row["count"] for row in paid_totals),
        "tickets_created": sum(e.tickets_created or 0 for e in events_list),
        "tickets_sold": sum(e.tickets_sold or 0 for e in events_list),
        "pending_orders_total": sum(
            (e.pending_order_count or 0) for e in events_list
        ),
    }
    return render(request, "events/dashboard.html", context)


# ---------------------------------------------------------------------------
# Event create / edit
# ---------------------------------------------------------------------------
def _event_video_formset(*args, **kwargs):
    """Shared factory so ``event_create`` and ``event_edit`` can't drift."""
    EventVideoFormSet = inlineformset_factory(
        Event,
        EventVideo,
        form=EventVideoForm,
        extra=1,
        can_delete=True,
    )
    return EventVideoFormSet(*args, **kwargs)


@organizer_required
def event_create(request: HttpRequest) -> HttpResponse:
    """
    Create an event, optionally attaching video rows in the same step.

    The event is created in ``PENDING`` status; admins review before
    publication.
    """
    if request.method == "POST":
        form = EventForm(request.POST, request.FILES)
        formset = _event_video_formset(request.POST, request.FILES)
        if form.is_valid() and formset.is_valid():
            event = form.save(commit=False)
            event.organizer = request.user
            event.status = Event.Status.PENDING
            event.save()

            formset.instance = event
            formset.save()

            messages.success(
                request,
                "Event submitted for review. "
                "Add ticket tiers from the event page after it is approved.",
            )
            return redirect("events:organizer_dashboard")
    else:
        form = EventForm()
        formset = _event_video_formset()

    return render(
        request,
        "events/event_form.html",
        {"form": form, "formset": formset},
    )


@organizer_required
def event_edit(request: HttpRequest, slug: str) -> HttpResponse:
    """
    Edit an existing event; re-queues for review if it was published.

    A ``CANCELLED`` event cannot be edited — resurrecting it via the edit
    form would silently re-enter it into the review queue.
    """
    event = get_object_or_404(Event, slug=slug, organizer=request.user)

    if event.status == Event.Status.CANCELLED:
        messages.error(
            request,
            "Cancelled events cannot be edited. "
            "Contact an administrator if this was a mistake.",
        )
        return redirect("events:organizer_dashboard")

    if request.method == "POST":
        form = EventForm(request.POST, request.FILES, instance=event)
        formset = _event_video_formset(
            request.POST, request.FILES, instance=event,
        )
        if form.is_valid() and formset.is_valid():
            obj = form.save(commit=False)
            if event.status == Event.Status.PUBLISHED:
                obj.status = Event.Status.PENDING
            obj.save()
            formset.save()

            messages.success(
                request,
                "Event saved. Changes require review before republication.",
            )
            return redirect("events:organizer_dashboard")
    else:
        form = EventForm(instance=event)
        formset = _event_video_formset(instance=event)

    return render(
        request,
        "events/event_form.html",
        {"form": form, "formset": formset, "event": event},
    )


# ---------------------------------------------------------------------------
# Ticket tiers
# ---------------------------------------------------------------------------
def _active_tier_or_redirect(request, event):
    """
    Return the event's single active tier, or None after queuing a redirect
    message. Never use ``get_object_or_404(TicketTier, event=event)`` — the
    DB allows one active *plus* N inactive tiers, so that would eventually
    raise ``MultipleObjectsReturned``.
    """
    tier = event.ticket_tiers.filter(is_active=True).first()
    if tier is None:
        messages.info(
            request,
            "This event has no active ticket tier yet. Create one first.",
        )
    return tier


@organizer_required
def tier_create(request: HttpRequest, slug: str) -> HttpResponse:
    """
    Create the ticket tier for an event.

    A cancelled event cannot receive a new tier. If an active tier already
    exists, redirect to the edit view rather than letting the organizer
    create a duplicate (the DB constraint would reject it anyway — this
    avoids the confusing ``IntegrityError``).
    """
    event = get_object_or_404(Event, slug=slug, organizer=request.user)

    if event.status == Event.Status.CANCELLED:
        messages.error(
            request, "Cannot add a ticket tier to a cancelled event.",
        )
        return redirect("events:organizer_dashboard")

    existing = event.ticket_tiers.filter(is_active=True).first()
    if existing is not None:
        messages.info(
            request,
            "This event already has an active ticket tier. "
            "You can adjust it below.",
        )
        return redirect("events:tier_edit", slug=event.slug)

    if request.method == "POST":
        form = TierForm(request.POST, event=event, organizer=request.user)
        if form.is_valid():
            tier = form.save(commit=False)
            tier.event = event
            tier.save()
            messages.success(request, "Ticket tier created.")
            return redirect("events:organizer_dashboard")
    else:
        form = TierForm(event=event, organizer=request.user)

    return render(
        request,
        "events/tier_form.html",
        {"form": form, "event": event, "mode": "create"},
    )


@organizer_required
def tier_edit(request: HttpRequest, slug: str) -> HttpResponse:
    """
    Edit the event's active ticket tier. Quantity can be increased or
    decreased (but never below ``sold`` — enforced by ``TierForm.clean()``).
    """
    event = get_object_or_404(Event, slug=slug, organizer=request.user)

    if event.status == Event.Status.CANCELLED:
        messages.error(
            request, "Cannot edit a ticket tier on a cancelled event.",
        )
        return redirect("events:organizer_dashboard")

    tier = _active_tier_or_redirect(request, event)
    if tier is None:
        return redirect("events:tier_create", slug=event.slug)

    if request.method == "POST":
        form = TierForm(
            request.POST, instance=tier, event=event, organizer=request.user,
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Ticket tier updated.")
            return redirect("events:organizer_dashboard")
    else:
        form = TierForm(instance=tier, event=event, organizer=request.user)

    return render(
        request,
        "events/tier_form.html",
        {
            "form": form,
            "event": event,
            "tier": tier,
            "mode": "edit",
        },
    )


# ---------------------------------------------------------------------------
# Scanner management (organizer or staff)
# ---------------------------------------------------------------------------
def _get_manageable_event(request: HttpRequest, slug: str) -> Event:
    """Fetch the event, 404 if the user may not manage its scanners."""
    event = get_object_or_404(Event, slug=slug)
    if not can_manage_scanners(request.user, event):
        raise Http404
    return event


@organizer_required
def manage_scanners(request: HttpRequest, slug: str) -> HttpResponse:
    """
    List scanners assigned to an event, and expose the add/remove actions.

    Single entry point for scanner management — no separate add form page.
    The POST is handled by ``add_scanner`` below.
    """
    event = _get_manageable_event(request, slug)

    scanners = (
        EventScanner.objects
        .filter(event=event)
        .select_related("user", "created_by")
        .order_by("user__username")
    )

    return render(
        request,
        "events/manage_scanners.html",
        {"event": event, "scanners": scanners},
    )


@organizer_required
@require_POST
def add_scanner(request: HttpRequest, slug: str) -> HttpResponse:
    """
    Add a user as a scanner for one event.

    - ``identifier`` may be a username or an email address.
    - The target must have an account and the ``scanner`` role.
    - The form is given ``event`` so its ``clean_identifier`` can pre-empt
      the unique ``(event, user)`` constraint with a proper field error
      rather than a generic ``IntegrityError`` bubble.
    """
    event = _get_manageable_event(request, slug)
    form = AddScannerForm(request.POST, event=event, organizer=request.user)

    if not form.is_valid():
        # Preserve field-level errors; surface non-field errors too.
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    error if field == "__all__" else f"{field}: {error}",
                )
        return redirect("events:manage_scanners", slug=event.slug)

    scanner = form.save(commit=False)
    scanner.event = event
    scanner.created_by = request.user

    try:
        with transaction.atomic():
            scanner.save()
    except IntegrityError:
        # Narrow message — the form should have caught this, but a race
        # between two concurrent POSTs is still possible. If the error is
        # *not* the (event, user) unique violation, re-raise so we don't
        # mask real bugs.
        from django.db import connection

        existing = EventScanner.objects.filter(
            event=event, user=scanner.user,
        ).exists()
        if not existing:
            raise
        messages.info(
            request,
            f"{scanner.user.username} is already a scanner for this event.",
        )
        return redirect("events:manage_scanners", slug=event.slug)

    messages.success(
        request,
        f'{scanner.user.username} can now scan tickets for "{event.title}".',
    )
    return redirect("events:manage_scanners", slug=event.slug)

@organizer_required
@require_POST
def remove_scanner(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    event = _get_manageable_event(request, slug)
    scanner = get_object_or_404(EventScanner, pk=pk, event=event)
    username = scanner.user.username
    scanner.delete()
    messages.success(request, f"Removed {username} as a scanner.")
    return redirect("events:manage_scanners", slug=event.slug)


# tickets/views.py

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from accounts.permissions import scanner_required
from events.permissions import scanner_event_ids
from events.models import Event
from tickets.models import Ticket


@scanner_required
def scanner_stats(request):
    """
    Dashboard for a scanner: their recent scans, per-event counts, and
    today-vs-total breakdowns.

    Only tickets the scanner themselves consumed are shown (audit trail).
    Staff/superusers see everything.
    """
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Scope: tickets assigned to events this scanner may scan.
    ticket_qs = Ticket.objects.select_related(
        "order_item__tier__event",
        "order_item__order",
    )

    if not (request.user.is_staff or request.user.is_superuser):
        ids = scanner_event_ids(request.user)
        # Organizers count too (their own events)
        ticket_qs = ticket_qs.filter(
            Q(order_item__tier__event__in=ids)
            | Q(order_item__tier__event__organizer=request.user)
        )
        ticket_qs = ticket_qs.filter(used_by=request.user)

    # Stats over consumed tickets only.
    used_qs = ticket_qs.filter(used_at__isnull=False)

    total_scanned = used_qs.count()
    today_scanned = used_qs.filter(used_at__gte=today_start).count()

    per_event = (
        used_qs
        .values("order_item__tier__event__title",
                "order_item__tier__event__slug",
                "order_item__tier__event__starts_at")
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

    recent = used_qs.order_by("-used_at")[:25]

    return render(request, "tickets/scanner_stats.html", {
        "total_scanned": total_scanned,
        "today_scanned": today_scanned,
        "per_event": per_event,
        "per_day": per_day,
        "recent": recent,
        "now": now,
    })