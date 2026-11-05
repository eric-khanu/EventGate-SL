"""Django admin configuration for events."""

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .models import Event, EventScanner, EventVideo, TicketTier


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------
class EventVideoInline(admin.TabularInline):
    model = EventVideo
    extra = 0
    fields = ("order", "title", "source", "url", "file")
    ordering = ("order", "id")


class TierInline(admin.TabularInline):
    model = TicketTier
    extra = 0
    fields = (
        "name", "price", "quantity", "sold",
        "discount_percent", "sale_starts", "sale_ends", "is_active",
    )
    readonly_fields = ("sold",)
    ordering = ("price",)

    def has_delete_permission(self, request, obj=None):
        """
        Block tier deletion once anything is sold. The FK is PROTECT, so the
        DB would reject it anyway — this just prevents the ugly
        ``ProtectedError`` traceback.
        """
        if obj is not None and getattr(obj, "sold", 0) > 0:
            return False
        return super().has_delete_permission(request, obj)


class ScannerInline(admin.TabularInline):
    model = EventScanner
    extra = 0
    fields = ("user", "created_by", "created_at")
    readonly_fields = ("created_by", "created_at")
    autocomplete_fields = ("user",)


# ---------------------------------------------------------------------------
# Form: restrict status choices for non-superusers
# ---------------------------------------------------------------------------
class EventAdminForm(forms.ModelForm):
    """
    Non-superusers cannot select ``PUBLISHED`` at all. The dropdown simply
    doesn't offer it, so they can't accidentally self-approve.
    """

    class Meta:
        model = Event
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        user = getattr(self.request, "user", None)
        if user and not user.is_superuser:
            self.fields["status"].choices = [
                (value, label)
                for value, label in Event.Status.choices
                if value != Event.Status.PUBLISHED
            ]


# ---------------------------------------------------------------------------
# Actions (all superuser-only)
# ---------------------------------------------------------------------------
def _require_superuser(request):
    if not request.user.is_superuser:
        raise PermissionDenied


@admin.action(description="Approve & publish selected events (superusers only)")
def approve_events(modeladmin, request, queryset):
    """
    Promote PENDING events to PUBLISHED — but only if they have an active
    ticket tier. Single ``UPDATE ... WHERE EXISTS(active tier)`` so there's
    no race between the existence check and the write.
    """
    _require_superuser(request)

    active_tier = TicketTier.objects.filter(event=OuterRef("pk"), is_active=True)
    pending = queryset.filter(status=Event.Status.PENDING)
    approved = (
        pending
        .annotate(has_active_tier=Exists(active_tier))
        .filter(has_active_tier=True)
        .update(status=Event.Status.PUBLISHED, updated_at=timezone.now())
    )
    skipped = pending.count() - approved

    msg = f"{approved} event(s) approved and published."
    if skipped:
        msg += f" {skipped} skipped (no active tier)."
    modeladmin.message_user(request, msg, messages.SUCCESS)


@admin.action(description="Reject selected events (superusers only)")
def reject_events(modeladmin, request, queryset):
    _require_superuser(request)
    updated = queryset.filter(
        status__in=[Event.Status.PENDING, Event.Status.PUBLISHED],
    ).update(status=Event.Status.REJECTED, updated_at=timezone.now())
    modeladmin.message_user(
        request, f"{updated} event(s) rejected.", messages.SUCCESS,
    )


@admin.action(description="Cancel selected events (superusers only)")
def cancel_events(modeladmin, request, queryset):
    _require_superuser(request)
    updated = queryset.exclude(
        status=Event.Status.CANCELLED,
    ).update(status=Event.Status.CANCELLED, updated_at=timezone.now())
    modeladmin.message_user(
        request, f"{updated} event(s) cancelled.", messages.SUCCESS,
    )


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------
@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    form = EventAdminForm

    list_display = ("title", "organizer", "district", "starts_at", "status")
    list_filter = ("status", "district", "region", "category")
    search_fields = ("title", "venue", "organizer__username")
    # NOTE: no prepopulated_fields — slug is generated in Event.save().
    readonly_fields = ("created_at", "updated_at", "slug")
    date_hierarchy = "starts_at"
    list_select_related = ("organizer",)
    inlines = [EventVideoInline, TierInline, ScannerInline]
    actions = [approve_events, reject_events, cancel_events]
    list_per_page = 50

    fieldsets = (
        (None, {"fields": ("organizer", "title", "slug", "status")}),
        ("Details", {"fields": ("summary", "description", "category")}),
        ("Location", {"fields": ("venue", "district", "region")}),
        ("Schedule", {"fields": ("starts_at", "ends_at")}),
        ("Media & contact", {"fields": ("cover_image", "contact_phone", "currency")}),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    def get_actions(self, request):
        """Hide superuser-only actions from non-superusers in the UI."""
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            for name in ("approve_events", "reject_events", "cancel_events"):
                actions.pop(name, None)
        return actions

    def get_form(self, request, obj=None, **kwargs):
        """Pass the request through so the form can filter status choices."""
        BaseForm = super().get_form(request, obj, **kwargs)

        class _Form(BaseForm):
            def __init__(self, *args, **kw):
                kw["request"] = request
                super().__init__(*args, **kw)

        return _Form

    # ------------------------------------------------------------------
    # Save guards
    # ------------------------------------------------------------------
    def save_model(self, request, obj, form, change):
        """
        Enforce two invariants around the PUBLISHED status:

        1. Only a superuser may ever end up with ``status == PUBLISHED``.
        2. A PUBLISHED event must have at least one active ticket tier.

        The check runs inside a transaction with ``select_for_update`` so a
        concurrent ``TierInline`` save (deactivating the only tier) can't
        slip between the check and the write.
        """
        with transaction.atomic():
            # Guard 1: non-superusers can never publish, even via a forged POST
            # (the form already hides the choice — this is defense in depth).
            if obj.status == Event.Status.PUBLISHED and not request.user.is_superuser:
                obj.status = Event.Status.PENDING
                messages.warning(
                    request,
                    "Only superusers can publish events. Saved as PENDING.",
                )

            # Guard 2: can't be PUBLISHED without an active tier.
            if obj.status == Event.Status.PUBLISHED and obj.pk:
                has_active = (
                    obj.ticket_tiers
                    .select_for_update()
                    .filter(is_active=True)
                    .exists()
                )
                if not has_active:
                    obj.status = Event.Status.PENDING
                    messages.warning(
                        request,
                        "Cannot publish an event with no active ticket tier. "
                        "Saved as PENDING instead.",
                    )

            super().save_model(request, obj, form, change)


# ---------------------------------------------------------------------------
# TicketTier (standalone)
# ---------------------------------------------------------------------------
@admin.register(TicketTier)
class TicketTierAdmin(admin.ModelAdmin):
    list_display = (
        "event", "name", "price", "quantity", "sold",
        "remaining_display", "sell_through_display", "discount_percent", "is_active",
    )
    list_filter = ("is_active", "event__district")
    search_fields = ("event__title", "name")
    list_select_related = ("event",)
    autocomplete_fields = ("event",)
    readonly_fields = ("sold",)
    list_per_page = 50

    @admin.display(description="Remaining", ordering="quantity")
    def remaining_display(self, obj):
        return obj.remaining

    @admin.display(description="Sold %")
    def sell_through_display(self, obj):
        return f"{obj.sell_through_pct}%"


# ---------------------------------------------------------------------------
# EventScanner (standalone, for auditing)
# ---------------------------------------------------------------------------
@admin.register(EventScanner)
class EventScannerAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "created_by", "created_at")
    list_filter = ("event__district",)
    search_fields = ("event__title", "user__username")
    list_select_related = ("event", "user", "created_by")
    autocomplete_fields = ("event", "user")
    readonly_fields = ("created_at",)