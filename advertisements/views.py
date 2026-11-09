import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import F, Sum
from django.http import (
    Http404, HttpRequest, HttpResponseRedirect, JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import AdCampaignForm, AdSubscriptionForm
from .models import AdCampaign, AdPlan, AdSubscription

logger = logging.getLogger(__name__)

# Tracking throttle: one impression/click per campaign per IP per window.
_TRACK_WINDOW_SECONDS = 60


def _client_ip(request: HttpRequest) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _throttled(request: HttpRequest, campaign_pk: int, kind: str) -> bool:
    """Return True if the caller should be throttled (already counted recently)."""
    key = f"ad_track:{kind}:{campaign_pk}:{_client_ip(request)}"
    # ``add`` returns True when the key did NOT exist -> first hit in window.
    return not cache.add(key, 1, timeout=_TRACK_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _current_subscription(user):
    return (
        AdSubscription.objects.filter(
            advertiser=user,
            status=AdSubscription.Status.ACTIVE,
            expires_at__gt=timezone.now(),
        )
        .order_by("-expires_at")
        .first()
    )


def _live_campaign_or_404(pk):
    campaign = get_object_or_404(
        AdCampaign.objects.select_related("subscription"),
        pk=pk,
        status=AdCampaign.Status.APPROVED,
    )
    if not campaign.is_live():
        raise Http404
    return campaign


# ---------------------------------------------------------------------------
# Advertiser dashboard
# ---------------------------------------------------------------------------
@login_required
def dashboard(request):
    campaigns = (
        AdCampaign.objects.filter(advertiser=request.user)
        .select_related("subscription")
        .order_by("-created_at")
    )
    subscriptions = (
        AdSubscription.objects.filter(advertiser=request.user)
        .select_related("plan")
        .order_by("-created_at")
    )
    stats = campaigns.aggregate(
        impressions=Sum("impressions"),
        clicks=Sum("clicks"),
    )
    return render(request, "advertisements/dashboard.html", {
        "campaigns": campaigns,
        "subscriptions": subscriptions,
        "plans": AdPlan.objects.filter(is_active=True),
        "impressions": stats["impressions"] or 0,
        "clicks": stats["clicks"] or 0,
    })


# ---------------------------------------------------------------------------
# Subscribe to a plan
# ---------------------------------------------------------------------------
@login_required
def subscribe(request):
    if request.method == "POST":
        # Reject duplicate pending submissions (spam mitigation).
        if AdSubscription.objects.filter(
            advertiser=request.user,
            status=AdSubscription.Status.PENDING,
        ).exists():
            messages.info(
                request,
                "You already have a pending subscription awaiting verification.",
            )
            return redirect("ad_dashboard")

        form = AdSubscriptionForm(request.POST, request.FILES)
        if form.is_valid():
            subscription = form.save(commit=False)
            subscription.advertiser = request.user
            subscription.amount = subscription.plan.price
            subscription.currency = subscription.plan.currency
            subscription.save()

            # Validate + store the proof through the model path (same rules
            # as checkout) rather than via the form's raw FileField.
            proof = form.cleaned_data.get("payment_proof")
            if proof:
                subscription.attach_payment_proof(proof)

            messages.success(
                request,
                "Payment details submitted. Your subscription stays "
                "pending until the platform administrator verifies the "
                "payment.",
            )
            return redirect("ad_dashboard")
    else:
        form = AdSubscriptionForm()
    return render(request, "advertisements/subscribe.html", {"form": form})


# ---------------------------------------------------------------------------
# Create a campaign
# ---------------------------------------------------------------------------
@login_required
def campaign_create(request):
    subscription = _current_subscription(request.user)
    if subscription is None:
        messages.warning(
            request,
            "You need an active, administrator-verified advertising "
            "subscription before submitting a campaign.",
        )
        return redirect("ad_subscribe")

    if AdCampaign.objects.filter(
        advertiser=request.user, status=AdCampaign.Status.PENDING,
    ).count() >= 5:
        messages.warning(
            request,
            "You already have several campaigns awaiting review. "
            "Please wait for those to be processed before submitting more.",
        )
        return redirect("ad_dashboard")

    if request.method == "POST":
        form = AdCampaignForm(request.POST, request.FILES)
        if form.is_valid():
            campaign = form.save(commit=False)
            campaign.advertiser = request.user
            campaign.subscription = subscription
            campaign.status = AdCampaign.Status.PENDING
            campaign.save()

            messages.success(
                request,
                "Campaign submitted for creative review. "
                "It will display after approval.",
            )
            return redirect("ad_dashboard")
    else:
        form = AdCampaignForm()
    return render(
        request, "advertisements/campaign_form.html", {"form": form},
    )


# ---------------------------------------------------------------------------
# Tracking endpoints
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def impression(request: HttpRequest, pk: int) -> JsonResponse:

    if _throttled(request, pk, "impression"):
        return JsonResponse({"ok": True, "throttled": True})

    try:
        campaign = _live_campaign_or_404(pk)
    except Http404:
        return JsonResponse({"ok": False}, status=404)

    AdCampaign.objects.filter(pk=campaign.pk).update(
        impressions=F("impressions") + 1,
    )
    return JsonResponse({"ok": True})


def click(request: HttpRequest, pk: int) -> HttpResponseRedirect:
   
    try:
        campaign = _live_campaign_or_404(pk)
    except Http404:
        # Ad expired between render and click. Still redirect so the user
        # doesn't see a 404, but don't count the click.
        stale = (
            AdCampaign.objects
            .filter(pk=pk)
            .only("id", "target_url")
            .first()
        )
        if stale:
            # Reuse the model's URL whitelist.
            url = AdCampaign(target_url=stale.target_url).safe_target_url
            if url:
                return HttpResponseRedirect(url)
        raise

    if not _throttled(request, pk, "click"):
        AdCampaign.objects.filter(pk=campaign.pk).update(
            clicks=F("clicks") + 1,
        )

    return HttpResponseRedirect(campaign.safe_target_url)