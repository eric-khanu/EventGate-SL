"""Django admin for the accounts app."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import UserProfile

User = get_user_model()


class UserProfileInline(admin.StackedInline):
    """Edit a user's profile on the same admin page."""

    model = UserProfile
    can_delete = False
    fk_name = "user"
    extra = 0
    max_num = 1


# Django's django.contrib.auth.admin registers the stock User model with
# `auth.UserAdmin` when its autodiscover runs. To install our own subclass
# with the profile inline, we must:
#
#   1. Be loaded AFTER django.contrib.auth in INSTALLED_APPS (so the auth
#      admin has already registered and unregister() finds it).
#   2. Unregister the stock admin, then register ours.
#
# If both orderings can't be guaranteed, wrap unregister in try/except
# AlreadyRegistered... but NotRegistered is the exception we'd need to
# swallow here, and swallowing it hides real misconfiguration. Keep the
# bare call — if the order is wrong, we want a loud failure.
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    # Some deployments override AUTH_USER_MODEL or remove contrib.auth.admin.
    # If the stock registration isn't there, we don't need to remove it.
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = BaseUserAdmin.inlines + (UserProfileInline,)
    list_display = BaseUserAdmin.list_display + ("role_summary",)
    list_filter = BaseUserAdmin.list_filter + ("groups",)

    @admin.display(description="Roles")
    def role_summary(self, obj):
        return ", ".join(obj.groups.values_list("name", flat=True)) or "—"