"""Account-level role checks and view decorators.

Never imports ``events.models`` at module load — role checks that need event
data live in ``events.permissions``. This module answers "what role is this
user?" and "does this request pass the account-level gate?".

Layering:

    accounts.roles        -- the group names (strings)
    accounts.permissions  -- role predicates + decorators (this file)
    events.permissions    -- event-scoped predicates (can_scan, ...)

    accounts never imports from events. Events imports from accounts.
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .roles import GROUP_ORGANIZERS, GROUP_SCANNERS


# ---------------------------------------------------------------------------
# Role predicates (account-scoped — no event argument)
# ---------------------------------------------------------------------------
def has_scanner_role(user) -> bool:
    """True if ``user`` is allowed to use the scanner system at all."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return user.groups.filter(name=GROUP_SCANNERS).exists()


def has_organizer_role(user) -> bool:
    """True if ``user`` is allowed to create/manage events."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return user.groups.filter(name=GROUP_ORGANIZERS).exists()


def is_scanner_only(user) -> bool:
    """
    True for users whose *only* role is scanner.

    Staff, superusers, and organizers are never scanner-only — they keep
    access to the rest of the app. Used by the middleware to redirect the
    login to the scanner dashboard instead of the buyer/organizer pages.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff or user.is_superuser:
        return False
    in_scanner = user.groups.filter(name=GROUP_SCANNERS).exists()
    if not in_scanner:
        return False
    in_organizer = user.groups.filter(name=GROUP_ORGANIZERS).exists()
    return not in_organizer


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def scanner_required(view_func):
    """
    Account-level gate for the scanner UI and scan API.

    Requires login **and** the scanner role. Whether the user has any
    *event* they can scan is checked by the view (via
    ``events.permissions.scanner_event_ids`` etc.) — but for usability we
    refuse early if the user has the role but no assignment, so we don't
    render an empty scanner dropdown.

    Staff and superusers bypass the assignment check (they can scan any
    event).
    """
    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        u = request.user
        if not has_scanner_role(u):
            raise PermissionDenied

        if u.is_staff or u.is_superuser:
            return view_func(request, *args, **kwargs)

        # Local import keeps the accounts → events dependency out of the
        # module load path. It only resolves when a scanner request arrives.
        from events.permissions import (
            has_any_scanner_assignment,
            organizes_any_event,
        )

        if not (
            has_any_scanner_assignment(u)
            or organizes_any_event(u)
        ):
            raise PermissionDenied

        return view_func(request, *args, **kwargs)
    return wrapped


def organizer_required(view_func):
    """
    Account-level gate for organizer views.

    Views that also need per-event ownership must additionally call
    ``events.permissions.can_manage_scanners(user, event)`` (or the narrower
    ownership check) inside the view.
    """
    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not has_organizer_role(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapped

