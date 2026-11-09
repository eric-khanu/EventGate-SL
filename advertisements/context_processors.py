from django.db.models import Q
from django.utils import timezone

from .models import AdCampaign, AdSubscription


def active_advertisements(request):
  
    now = timezone.now()

    ads = (
        AdCampaign.objects.filter(
            status=AdCampaign.Status.APPROVED,
            subscription__status=AdSubscription.Status.ACTIVE,
            subscription__expires_at__gt=now,
        )
        .filter(
            Q(starts_at__isnull=True) | Q(starts_at__lte=now),
            Q(ends_at__isnull=True) | Q(ends_at__gt=now),
        )
        .select_related("subscription")
    )

    # One query for all three buckets; partition in Python.
    # The original ran 3 separate queries with ``order_by("?")`` per bucket
    # which is:
    #   1. 3× DB round-trips on *every* page render, and
    #   2. ``ORDER BY RANDOM()`` which is a full-table sort on large tables.
    #
    # To keep randomisation without the cost, fetch a bounded candidate set
    # once, then shuffle per-bucket in Python. Adjust ``_CANDIDATE_LIMIT``
    # if you want more variety.
    candidates = list(ads.order_by("-created_at")[:_CANDIDATE_LIMIT])

    def _bucket(placements, limit):
        matches = [ad for ad in candidates if ad.placement in placements]
        return _random_sample(matches, limit)

    return {
        "active_banner_ads": _bucket({"hero", "banner"}, 3),
        "active_sidebar_ads": _bucket({"sidebar"}, 2),
        "active_spotlight_ads": _bucket({"spotlight"}, 3),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_CANDIDATE_LIMIT = 60  # cap on ads fetched per page render


def _random_sample(items, limit):
    """Return a random sample of ``items`` up to ``limit``."""
    import random

    if len(items) <= limit:
        return items
    return random.sample(items, limit)


def active_advertisements(request):
    # Skip in admin — ads don't render there, and it saves a query per page.
    resolver = getattr(request, "resolver_match", None)
    if resolver and resolver.app_name == "admin":
        return {}

    now = timezone.now()
    ads = (
        AdCampaign.objects.filter(
            status=AdCampaign.Status.APPROVED,
            subscription__status=AdSubscription.Status.ACTIVE,
            subscription__expires_at__gt=now,
        )
        .filter(
            Q(starts_at__isnull=True) | Q(starts_at__lte=now),
            Q(ends_at__isnull=True) | Q(ends_at__gt=now),
        )
        .select_related("subscription")
    )

    candidates = list(ads.order_by("-created_at")[:_CANDIDATE_LIMIT])

    def _bucket(placements, limit):
        return _random_sample(
            [ad for ad in candidates if ad.placement in placements], limit,
        )

    return {
        "active_banner_ads": _bucket({"hero", "banner"}, 3),
        "active_sidebar_ads": _bucket({"sidebar"}, 2),
        "active_spotlight_ads": _bucket({"spotlight"}, 3),
    }