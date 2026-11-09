from django.contrib import admin, messages
from django.db import transaction

from .models import AdCampaign, AdPlan, AdSubscription


# ---------------------------------------------------------------------------
# AdPlan
# ---------------------------------------------------------------------------
@admin.register(AdPlan)
class AdPlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "duration_months",
        "price",
        "currency",
        "is_active",
        "display_order",
    )
    list_editable = (
        "price",
        "currency",
        "is_active",
        "display_order",
    )
    list_filter = (
        "is_active",
        "currency",
        "duration_months",
    )
    search_fields = ("name", "description")
    ordering = ("display_order", "duration_months", "price")


# ---------------------------------------------------------------------------
# AdSubscription
# ---------------------------------------------------------------------------
@admin.register(AdSubscription)
class AdSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "advertiser",
        "plan",
        "amount",
        "currency",
        "status",
        "payment_reference",
        "created_at",
        "expires_at",
    )
    list_filter = (
        "status",
        "payment_method",
        "currency",
        "created_at",
    )
    search_fields = (
        "advertiser__username",
        "advertiser__email",
        "payment_reference",
    )
    readonly_fields = (
        "advertiser",
        "plan",
        "amount",
        "currency",
        "payment_method",
        "payment_reference",
        "payment_notes",
        "payment_proof",
        "verified_by",
        "verified_at",
        "starts_at",
        "expires_at",
        "created_at",
    )
    list_select_related = ("advertiser", "plan")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    actions = ("verify_payments", "reject_payments")

    def has_add_permission(self, request):
        # Subscriptions are created by the advertiser via the public form.
        return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    @admin.action(description="Verify selected payments and activate subscriptions")
    def verify_payments(self, request, queryset):
        activated = 0
        skipped = 0

        for subscription in queryset.select_related("plan", "advertiser"):
            if subscription.status != AdSubscription.Status.PENDING:
                skipped += 1
                continue
            try:
                with transaction.atomic():
                    subscription.activate(request.user)
                activated += 1
            except ValueError:
                skipped += 1

        message = f"{activated} subscription(s) activated after manual verification."
        if skipped:
            message += f" {skipped} skipped (not pending)."
        self.message_user(request, message, messages.SUCCESS)

    @admin.action(description="Reject selected payment submissions")
    def reject_payments(self, request, queryset):
        rejected = 0
        skipped = 0
        for subscription in queryset.filter(
            status=AdSubscription.Status.PENDING
        ).select_related("plan"):
            try:
                subscription.reject(request.user)
                rejected += 1
            except ValueError:
                skipped += 1

        message = f"{rejected} payment submission(s) rejected."
        if skipped:
            message += f" {skipped} skipped."
        self.message_user(request, message, messages.WARNING)


# ---------------------------------------------------------------------------
# AdCampaign
# ---------------------------------------------------------------------------
@admin.register(AdCampaign)
class AdCampaignAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "company_name",
        "advertiser",
        "placement",
        "status",
        "impressions",
        "clicks",
        "created_at",
    )
    list_filter = (
        "status",
        "placement",
        "created_at",
    )
    search_fields = (
        "title",
        "company_name",
        "advertiser__username",
    )
    readonly_fields = (
        "advertiser",
        "subscription",
        "impressions",
        "clicks",
        "created_at",
        "updated_at",
    )
    list_select_related = ("advertiser", "subscription")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    actions = ("approve_campaigns", "reject_campaigns")

    def has_add_permission(self, request):
        # Campaigns are created by advertisers via the public form.
        return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    @admin.action(description="Approve selected campaigns")
    def approve_campaigns(self, request, queryset):
        approved = 0
        skipped = 0

        for campaign in queryset.select_related("subscription"):
            subscription = campaign.subscription
            if not subscription or not subscription.is_current:
                skipped += 1
                continue
            try:
                with transaction.atomic():
                    campaign.approve(request.user)
                    # Bound the campaign window to the subscription window.
                    from django.utils import timezone

                    campaign.starts_at = campaign.starts_at or timezone.now()
                    campaign.ends_at = subscription.expires_at
                    campaign.save(update_fields=["starts_at", "ends_at", "updated_at"])
                approved += 1
            except ValueError:
                skipped += 1

        message = f"{approved} campaign(s) approved."
        if skipped:
            message += (
                f" {skipped} skipped (no active subscription or invalid state)."
            )
        self.message_user(request, message, messages.SUCCESS)

    @admin.action(description="Reject selected campaigns")
    def reject_campaigns(self, request, queryset):
        rejected = 0
        for campaign in queryset.filter(
            status__in=[
                AdCampaign.Status.DRAFT,
                AdCampaign.Status.PENDING,
            ]
        ):
            try:
                campaign.reject(request.user)
                rejected += 1
            except ValueError:
                pass
        self.message_user(
            request,
            f"{rejected} campaign(s) rejected.",
            messages.WARNING,
        )