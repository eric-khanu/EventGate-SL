"""App configuration for the advertisements app."""

from django.apps import AppConfig


class AdvertisementsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "advertisements"
    verbose_name = "Advertisements"