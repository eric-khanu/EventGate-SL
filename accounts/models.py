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