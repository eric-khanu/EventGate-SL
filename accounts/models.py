"""
accounts/models.py
~~~~~~~~~~~~~~~~~~
User profile — the *only* model this app owns.

Deliberately **not** here:

* A custom User model. The project uses ``settings.AUTH_USER_MODEL``
  throughout, so the stock ``django.contrib.auth.models.User`` is the
  contract. Swapping it now is a migration hazard with no upside.
* A ``role`` field. Roles are ``Group`` membership (see ``accounts.roles``
  and the ``user_roles`` context processor). A second role store would let
  the two disagree about who can reach the organizer dashboard.

What *is* here: contact details and notification preferences that the
stock User model doesn't provide, one row per user, created lazily via a
``post_save`` signal or on demand.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    """Extra, non-authoritative data attached to ``settings.AUTH_USER_MODEL``.

    Created lazily — do not assume every user has one in queries. The
    ``ensure_profile`` signal creates one on user creation, but data
    imported before the signal was installed may still lack a row.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        primary_key=True,   # profile.id would be redundant; reuse user.id
    )

    # ---- Contact ----
    phone = models.CharField(
        max_length=30,
        blank=True,
        help_text="Default contact number; prefilled at checkout.",
    )

    # ---- Preferences ----
    email_marketing = models.BooleanField(
        default=False,
        help_text="Send event recommendations and offers by email.",
    )
    sms_ticket_updates = models.BooleanField(
        default=True,
        help_text="Send ticket confirmations and gate reminders by SMS.",
    )

    # ---- Audit ----
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "user profile"
        verbose_name_plural = "user profiles"

    def __str__(self) -> str:
        return f"Profile for {self.user}"


def get_profile(user) -> UserProfile:
    """
    Return ``user``'s profile, creating an empty one if needed.

    Safe to call from views after ``login_required`` — ``get_or_create``
    is one query when the row exists, one INSERT when it doesn't.
    """
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile