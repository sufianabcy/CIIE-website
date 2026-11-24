"""Create the first admin from FIRST_ADMIN_EMAIL if the portal has none yet.

Runs at every start (bin/start-web.sh), so a fresh free deploy needs no shell.
Does nothing once any admin exists.
"""

import os

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from people.models import Person


class Command(BaseCommand):
    help = "Create the first admin from FIRST_ADMIN_EMAIL when no admin exists."

    def handle(self, *args, **options):
        email = os.environ.get("FIRST_ADMIN_EMAIL", "").strip().lower()
        if not email:
            return
        if Person.objects.filter(is_superuser=True).exists():
            return
        person = Person.objects.filter(email__iexact=email).first()
        if person:
            person.is_superuser = True
            person.is_staff = True
            person.save(update_fields=["is_superuser", "is_staff"])
        else:
            person = Person.objects.create_superuser(email)
        person.groups.add(Group.objects.get(name="Admin"))
        self.stdout.write(f"First admin is {email}. Sign in at /signin/ with that email.")
