from django.contrib.auth.models import AnonymousUser

from advertisements.models import AdSubscription
from events.models import Event
from events.permissions import has_any_scanner_assignment
from .roles import GROUP_ORGANIZERS, GROUP_ADVERTISERS


def user_roles(request):
    """
    Adds role flags to every template context so the nav can adapt.

    - ``is_organizer`` : user belongs to ``GROUP_ORGANIZERS`` **or** owns
      at least one event. The group is the canonical signal; the event
      fallback exists so organizers invited via direct event assignment
      (without an admin manually adding them to the group) still get the
      dashboard link.
    - ``is_advertiser`` : user belongs to ``GROUP_ADVERTISERS`` **or** has
      at least one active ad subscription.
    - ``can_scan`` : user has at least one ``EventScanner`` row (or is
      superuser). Gates the "Gate scanner" nav link.

    Results are cached on ``request`` for the rest of the request.
    """
    user = getattr(request, "user", None) or AnonymousUser()

    if not user.is_authenticated:
        return {
            "is_organizer": False,
            "is_advertiser": False,
            "can_scan": False,
        }

    cache = getattr(request, "_roles_context_cache", None)
    if cache is not None:
        return cache

    groups = set(
        user.groups
        .filter(name__in=[GROUP_ORGANIZERS, GROUP_ADVERTISERS])
        .values_list("name", flat=True)
    )

    has_event = (
        True if GROUP_ORGANIZERS in groups
        else Event.objects.filter(organizer=user).exists()
    )
    has_subscription = (
        True if GROUP_ADVERTISERS in groups
        else AdSubscription.objects.filter(advertiser=user).exists()
    )

    ctx = {
        "is_organizer": GROUP_ORGANIZERS in groups or has_event,
        "is_advertiser": GROUP_ADVERTISERS in groups or has_subscription,
        "can_scan": has_any_scanner_assignment(user),
    }
    request._roles_context_cache = ctx
    return ctx