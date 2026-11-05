"""Event-scoped access predicates.

These are the *only* functions that decide whether a user may scan tickets
for an event, or manage scanners on it. Views, decorators, and the context
processor all go through them — otherwise the nav and the view can disagree.

Do NOT put role/group logic here. "Is this user a scanner at all?" belongs
in ``accounts.permissions``. This module answers questions about a *specific
event* and a specific user.
"""

from __future__ import annotations

from django.db.models import QuerySet


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def can_scan(user, event) -> bool:
    """
    True if ``user`` may scan tickets for ``event``.

    Rules, in order:
      * unauthenticated users never can
      * superusers and staff can scan any event
      * the event's organizer can scan their own event (no assignment needed)
      * anyone else must have an ``EventScanner`` row for this event
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if event.organizer_id == user.pk:
        return True
    # Avoid the query if we can prove the answer is already False.
    if not _is_scanner_role(user):
        return False
    from .models import EventScanner  # local import: avoid AppRegistry issues
    return EventScanner.objects.filter(event=event, user=user).exists()


def has_any_scanner_assignment(user) -> bool:
    """
    True if ``user`` is assigned to scan at least one event.

    Superusers are **not** automatically True — they can scan anything, but
    they may not have any assignments. Callers that want "can this user
    reach the scanner UI at all?" should combine this with a role check;
    the decorator in ``accounts.permissions`` does that.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    from .models import EventScanner
    return EventScanner.objects.filter(user=user).exists()


def scanner_event_ids(user) -> QuerySet | None:
    """
    Event IDs the user may scan.

    Returns ``None`` for superusers/staff — callers should skip the ``__in``
    filter entirely rather than materialise every event id. Everyone else
    gets a lazy queryset of ``event_id`` values, suitable for ``__in=...``.
    """
    if not getattr(user, "is_authenticated", False):
        from .models import Event
        return Event.objects.none()
    if user.is_superuser or user.is_staff:
        return None
    from .models import EventScanner
    return EventScanner.objects.filter(user=user).values_list("event_id", flat=True)


# ---------------------------------------------------------------------------
# Scanner management
# ---------------------------------------------------------------------------
def can_manage_scanners(user, event) -> bool:
    """True if ``user`` may add/remove scanners for ``event``."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return event.organizer_id == user.pk


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _is_scanner_role(user) -> bool:
    """
    Account-level role check — delegates to ``accounts.permissions``.

    Kept private so event-scoped callers can short-circuit before running
    the DB query, without duplicating role logic here.
    """
    from accounts.permissions import has_scanner_role
    return has_scanner_role(user)

def organizes_any_event(user) -> bool:
    """
    True if ``user`` owns at least one event.

    Used by ``accounts.permissions.scanner_required`` — organizers get
    scanner access to their own events without an ``EventScanner`` row,
    matching ``can_scan``.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    # Local import — ``events.permissions`` is imported by
    # ``accounts.permissions`` inside a function body (see the module
    # docstring), so at module-load time ``Event`` is not yet resolvable.
    from .models import Event
    return Event.objects.filter(organizer=user).exists()