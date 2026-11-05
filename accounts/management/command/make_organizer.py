"""Create the management commands package structure:

accounts/management/__init__.py
accounts/management/commands/__init__.py
accounts/management/commands/make_organizer.py
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from accounts.roles import GROUP_ORGANIZERS


class Command(BaseCommand):
    help = "Add a user to the Organizers group (event admin)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Remove the user from the group instead of adding.",
        )

    def handle(self, *args, username, remove, **opts):
        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            raise CommandError(f"No user named {username!r}.")

        group, _ = Group.objects.get_or_create(name=GROUP_ORGANIZERS)

        if remove:
            user.groups.remove(group)
            self.stdout.write(
                self.style.SUCCESS(f"{username} is no longer an organizer.")
            )
        else:
            user.groups.add(group)
            self.stdout.write(
                self.style.SUCCESS(f"{username} is now an organizer.")
            )