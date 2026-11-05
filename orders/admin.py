"""Django admin for orders."""

import io
import zipfile

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils.html import format_html

from .models import Order, OrderItem


def _proof_thumbnail(order):
    """Inline thumbnail for image proofs, PDF badge for PDFs, dash if none."""
    if not order.payment_proof:
        return "—"
    url = order.payment_proof_url()
    if (
        order.payment_proof_content_type
        and order.payment_proof_content_type.startswith("image/")
    ):
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">'
            '<img src="{}" alt="payment proof" '
            'style="max-height:60px;max-width:120px;border:1px solid #ccc;'
            'border-radius:3px;" /></a>',
            url, url,
        )
    return format_html(
        '<a href="{}" target="_blank" rel="noopener">📄 {} proof</a>',
        url,
        (order.payment_proof_content_type or "file").split("/")[-1].upper(),
    )


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = "__all__"

    def clean_payment_proof(self):
        f = self.cleaned_data.get("payment_proof")
        if f and hasattr(f, "read"):
            Order._validate_payment_proof(f)
        return f


class ItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    fields = ("tier", "quantity", "unit_price", "line_total")
    readonly_fields = ("tier", "quantity", "unit_price", "line_total")
    ordering = ("tier",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.action(description="Verify selected orders as paid and issue tickets")
def verify_and_issue(modeladmin, request, queryset):
    """
    For each selected PENDING order: lock it, transition to PAID via
    ``Order.mark_paid()`` (which also issues tickets), and report.

    ``mark_paid`` is atomic and idempotent, so races and double-clicks are
    safe. We pre-filter on status to save a query per order.
    """
    issued = 0
    skipped = 0

    for order in queryset.filter(status=Order.Status.PENDING).select_related("buyer"):
        try:
            issued_tickets = order.mark_paid(actor=request.user)
            issued += len(issued_tickets)
        except ValueError:
            skipped += 1

    message = f"Payment verification complete. {issued} ticket(s) issued."
    if skipped:
        message += f" {skipped} order(s) skipped."
    modeladmin.message_user(request, message, messages.SUCCESS)


@admin.action(description="Download payment proofs as a ZIP")
def download_proofs_zip(modeladmin, request, queryset):
    """
    Bundle every available payment proof in the selection into a single ZIP.

    Capped at 50 orders to avoid buffering gigabytes into memory.
    """
    if queryset.count() > 50:
        modeladmin.message_user(
            request,
            "Too many orders for a single ZIP export (max 50). "
            "Narrow your selection.",
            messages.ERROR,
        )
        return

    buf = io.BytesIO()
    included = 0
    missing = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for order in queryset:
            if not order.payment_proof:
                missing += 1
                continue
            try:
                with order.payment_proof.open("rb") as fh:
                    zf.writestr(order.payment_proof_filename(), fh.read())
                included += 1
            except FileNotFoundError:
                missing += 1

    if included == 0:
        modeladmin.message_user(
            request,
            "No payment proofs found for the selected orders.",
            messages.WARNING,
        )
        return

    buf.seek(0)
    resp = HttpResponse(buf.read(), content_type="application/zip")
    resp["Content-Disposition"] = (
        f'attachment; filename="payment_proofs_{queryset.count()}_orders.zip"'
    )
    if missing:
        modeladmin.message_user(
            request,
            f"{included} proof(s) included, {missing} order(s) had none.",
            messages.INFO,
        )
    return resp


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    form = OrderAdminForm
    list_display = (
        "reference",
        "buyer",
        "total",
        "currency",
        "payment_method",
        "status",
        "has_payment_proof",
        "created_at",
    )
    list_filter = (
        "status",
        "payment_method",
        "currency",
        "payment_proof_content_type",
    )
    search_fields = (
        "reference",
        "email",
        "phone",
        "payment_reference",
    )
    readonly_fields = (
        "reference",
        "buyer",
        "total",
        "currency",
        "created_at",
        "paid_at",
        "status",
        "payment_proof_preview",
        "payment_proof_uploaded_at",
        "payment_proof_content_type",
    )
    list_select_related = ("buyer",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    inlines = [ItemInline]
    actions = [verify_and_issue, download_proofs_zip]

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "reference",
                    "buyer",
                    "status",
                    "created_at",
                    "paid_at",
                )
            },
        ),
        ("Purchase", {"fields": ("total", "currency")}),
        ("Contact", {"fields": ("email", "phone")}),
        (
            "Payment",
            {
                "fields": (
                    "payment_method",
                    "payment_reference",
                    "payment_note",
                )
            },
        ),
        (
            "Payment proof",
            {
                "fields": (
                    "payment_proof",
                    "payment_proof_preview",
                    "payment_proof_uploaded_at",
                    "payment_proof_content_type",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(boolean=True, description="Proof?")
    def has_payment_proof(self, obj):
        return bool(obj.payment_proof)

    @admin.display(description="Preview")
    def payment_proof_preview(self, obj):
        if not obj or not obj.payment_proof:
            return "No payment proof uploaded."
        return _proof_thumbnail(obj)

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        base = list(self.readonly_fields)
        if obj and obj.status == Order.Status.PAID:
            base.extend(["email", "phone", "payment_method", "payment_reference"])
        return base

    def save_model(self, request, obj, form, change):
        """
        Route admin uploads through ``Order.attach_payment_proof`` so the
        validation, atomic swap, and old-file cleanup paths match checkout.
        """
        proof = form.cleaned_data.get("payment_proof")
        if change and proof and hasattr(proof, "read"):
            # Suppress the FileField's own save; attach_payment_proof will
            # do it atomically after we save the rest of the form data.
            obj.payment_proof = None
            super().save_model(request, obj, form, change)
            obj.attach_payment_proof(proof)
        else:
            super().save_model(request, obj, form, change)


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "tier", "quantity", "unit_price", "line_total")
    list_filter = ("tier__event",)
    search_fields = ("order__reference", "tier__name", "tier__event__title")
    list_select_related = ("order", "tier__event")
    readonly_fields = ("order", "tier", "quantity", "unit_price", "line_total")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False