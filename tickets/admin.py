"""Django admin for tickets and scan attempts.

Design notes
------------
* ``Ticket`` is read-only in the admin. Status changes go through
  ``Ticket.mark_used()`` / ``Ticket.void()`` — those are the only code paths
  that uphold the state-machine invariants (used_at is set exactly when
  status is 'scanned', a used ticket can't be voided, etc). The admin
  exposes bulk *voiding* as an action, not as an editable field.
* ``ScanAttempt`` is append-only. Every field is read-only, deletion is
  disabled, and it's registered both as an inline on ``Ticket`` (for
  per-ticket triage) and standalone (for "all scans today across every
  event" queries).
* No action here touches ``Token.token_key_version`` or the ciphertext —
  key rotation is a deliberate operation, not a button in the admin.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.db import transaction
from django.utils.html import format_html

from .models import ScanAttempt, Ticket, TicketStateError


# ===========================================================================
# Inline — read-only scan history on the ticket page
# ===========================================================================
class ScanAttemptInline(admin.TabularInline):
    """Read-only scan history for a ticket."""

    model = ScanAttempt
    extra = 0
    can_delete = False
    fields = (
        "created_at",
        "result",
        "ok",
        "scanner",
        "device_id",
        "ip",
        "user_agent",
    )
    readonly_fields = fields
    ordering = ("-created_at",)
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ===========================================================================
# Ticket
# ===========================================================================
@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "public_id_short",
        "event_name",
        "owner",
        "status",
        "issued_at",
        "used_at",
        "key_version_display",
    )
    list_filter = ("status", "order_item__tier__event")
    search_fields = (
        "public_id",
        "owner__username",
        "owner__email",
        "order_item__tier__event__title",
        "token_hash",
    )
    readonly_fields = (
        "public_id",
        "order_item",
        "owner",
        "status",
        "issued_at",
        "used_at",
        "used_by",
        "used_device_id",
        "token_hash",
        "token_key_version",
        "token_ciphertext_masked",
    )
    list_select_related = ("owner", "order_item__tier__event")
    date_hierarchy = "issued_at"
    ordering = ("-issued_at",)
    actions = ("void_selected",)
    inlines = [ScanAttemptInline]
    list_per_page = 50

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "public_id",
                    "status",
                    "owner",
                    "order_item",
                    "issued_at",
                )
            },
        ),
        (
            "Scan state",
            {
                "fields": ("used_at", "used_by", "used_device_id"),
                "classes": ("collapse",),
                "description": (
                    "Set by the scanner at the moment the QR is consumed. "
                    "There is no way to un-scan a ticket — issue a new one "
                    "via the buyer's order if a mistake was made."
                ),
            },
        ),
        (
            "Token (encrypted at rest)",
            {
                "fields": (
                    "token_hash",
                    "token_key_version",
                    "token_ciphertext_masked",
                ),
                "classes": ("collapse",),
                "description": (
                    "The raw token is never displayed. If a buyer needs their "
                    "QR re-sent, use the ticket's public page — the QR is "
                    "regenerated on demand from this row's hash."
                ),
            },
        ),
    )

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    @admin.display(description="Ticket", ordering="public_id")
    def public_id_short(self, obj):
        return format_html("<code>{}</code>", str(obj.public_id)[:8])

    @admin.display(description="Event", ordering="order_item__tier__event__title")
    def event_name(self, obj):
        return obj.event.title

    @admin.display(description="Key ver.", ordering="token_key_version")
    def key_version_display(self, obj):
        return obj.token_key_version

    @admin.display(description="Token ciphertext")
    def token_ciphertext_masked(self, obj):
        if not obj.token_ciphertext:
            return "—"
        return format_html(
            "<code>{}…</code> <span style='color:#666;'>({} bytes)</span>",
            obj.token_ciphertext[:12],
            len(obj.token_ciphertext),
        )

    # ------------------------------------------------------------------
    # Permissions — Ticket is fully read-only except via action
    # ------------------------------------------------------------------
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Allow opening the change page (to view inlines and fieldsets),
        # but every field is readonly and there's no Save button because
        # ``get_readonly_fields`` returns everything.
        return True

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        # Every field is read-only — no inline edits, no admin save path.
        return [f.name for f in self.model._meta.fields] + [
            "token_ciphertext_masked",
        ]

    # ------------------------------------------------------------------
    # Bulk action: void
    # ------------------------------------------------------------------
    @admin.action(description="Void selected tickets")
    def void_selected(self, request, queryset):
        """
        Void every *valid* ticket in the queryset.

        Skips used tickets (they can't be voided) and already-void ones
        without aborting the whole action — a mixed selection shouldn't
        fail the operation for the eligible tickets.
        """
        voided = 0
        used = 0
        already_void = 0
        errored = 0

        for ticket in queryset.only("pk", "status").iterator():
            if ticket.status == Ticket.Status.USED:
                used += 1
                continue
            if ticket.status == Ticket.Status.VOID:
                already_void += 1
                continue
            try:
                ticket.void()
                voided += 1
            except TicketStateError:
                # State changed between .only() read and .void() — a
                # concurrent scan won the race. Count and continue.
                errored += 1
            except Exception:                             # noqa: BLE001
                errored += 1
                self.message_user(
                    request,
                    f"Unexpected error voiding ticket {ticket.public_id}.",
                    messages.ERROR,
                )

        parts = [f"{voided} ticket(s) voided."]
        if used:
            parts.append(f"{used} already used (skipped).")
        if already_void:
            parts.append(f"{already_void} already void (skipped).")
        if errored:
            parts.append(f"{errored} failed to void (state changed).")
        self.message_user(request, " ".join(parts), messages.SUCCESS)


# ===========================================================================
# ScanAttempt — append-only audit log, standalone
# ===========================================================================
@admin.register(ScanAttempt)
class ScanAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "result",
        "ok_badge",
        "event",
        "ticket_short",
        "scanner",
        "device_id",
        "ip",
    )
    list_filter = (
        "result",
        "ok",
        "event",
        "created_at",
    )
    search_fields = (
        "ticket__public_id",
        "event__title",
        "scanner__username",
        "device_id",
        "ip",
    )
    readonly_fields = (
        "event",
        "ticket",
        "scanner",
        "result",
        "ok",
        "device_id",
        "ip",
        "user_agent",
        "raw_token_hash",
        "created_at",
    )
    list_select_related = ("event", "ticket", "scanner")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 100

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    @admin.display(description="Ticket", ordering="ticket__public_id")
    def ticket_short(self, obj):
        if not obj.ticket_id:
            return "—"
        return format_html("<code>{}</code>", str(obj.ticket.public_id)[:8])

    @admin.display(description="OK", ordering="ok")
    def ok_badge(self, obj):
        if obj.ok:
            return format_html(
                '<span style="color:#047857;font-weight:700;">✓</span>'
            )
        return format_html(
            '<span style="color:#b91c1c;font-weight:700;">✕</span>'
        )

    # ------------------------------------------------------------------
    # Immutable
    # ------------------------------------------------------------------
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True       # allow the read-only detail page

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]