import logging

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="accounts.ensure_profile")
def ensure_profile(sender, instance, created, **kwargs):
    """
    Create an empty ``UserProfile`` when a user is first created.

    Best-effort: a transient failure here must never abort user creation.
    If it fails, ``get_profile(user)`` will create the row on next access.
    """
    if not created:
        return
    try:
        from .models import UserProfile
        UserProfile.objects.get_or_create(user=instance)
    except Exception:                     # noqa: BLE001
        logger.exception("Could not create UserProfile for user %s", instance.pk)