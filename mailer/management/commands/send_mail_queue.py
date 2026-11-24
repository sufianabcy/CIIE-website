from django.core.management.base import BaseCommand

from mailer.services import send_pending


class Command(BaseCommand):
    help = "Send queued emails once (codes first), within the daily cap."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, limit, **options):
        tried = send_pending(limit=limit)
        self.stdout.write(f"Tried {tried} email(s).")
