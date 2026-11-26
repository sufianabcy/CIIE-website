"""Send one plain email through the configured transport, straight away (not through the outbox).

    python manage.py send_test_email --to someone@kluniversity.in

Prints which transport and server were used, and the exact error if sending fails. Never prints credentials.
"""

import smtplib

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send one test email through the configured email transport."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Where to send the test email.")

    def handle(self, *args, to, **options):
        backend = settings.EMAIL_BACKEND
        where = getattr(settings, "EMAIL_HOST", "") if backend.endswith("smtp.EmailBackend") else ""
        self.stdout.write(f"Transport: {backend}{f' via {where}:{settings.EMAIL_PORT}' if where else ''}")
        self.stdout.write(f"From: {settings.DEFAULT_FROM_EMAIL}")
        try:
            sent = send_mail(
                f"[{settings.CLUB_NAME}] Test email",
                f"This is a test email from the {settings.CLUB_NAME} portal. If you can read it, sending works.",
                settings.DEFAULT_FROM_EMAIL, [to],
            )
        except smtplib.SMTPAuthenticationError as exc:
            raise CommandError(f"The server refused the sign-in ({exc.smtp_code}): {_text(exc.smtp_error)}") from exc
        except smtplib.SMTPResponseException as exc:
            raise CommandError(f"The server said {exc.smtp_code}: {_text(exc.smtp_error)}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise CommandError(f"Sending failed: {exc.__class__.__name__}: {exc}") from exc
        if not sent:
            raise CommandError("The transport accepted nothing.")
        self.stdout.write(self.style.SUCCESS(f"Sent to {to}."))


def _text(error) -> str:
    return error.decode(errors="replace") if isinstance(error, bytes) else str(error)
