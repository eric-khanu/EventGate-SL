from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import UserProfile

User = get_user_model()


class UserProfileInline(admin.StackedInline):
    """Edit a user's profile on the same admin page."""
    model = UserProfile
    can_delete = False
    fk_name = "user"
    extra = 0
    max_num = 1


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = BaseUserAdmin.inlines + (UserProfileInline,)
    list_display = BaseUserAdmin.list_display + ("role_summary",)
    list_filter = BaseUserAdmin.list_filter + ("groups",)

    # 1. Add 'groups' to the Add User form
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'password1', 'password2', 'groups'),
        }),
    )

    # 2. Ensure 'groups' is in the Edit User form (usually it is by default,
    # but this guarantees it)
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('Personal info'), {'fields': ('first_name', 'last_name', 'email')}),
        (_('Permissions'), {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )

    @admin.display(description="Roles")
    def role_summary(self, obj):
        return ", ".join(obj.groups.values_list("name", flat=True)) or "—"