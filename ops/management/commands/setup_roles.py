from django.core.management.base import BaseCommand

from ops.roles import ensure_roles


class Command(BaseCommand):
    help = "Create or refresh the Coordinator and Admin roles and their permissions."

    def handle(self, *args, **options):
        ensure_roles()
        self.stdout.write("Roles are up to date.")
