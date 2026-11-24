"""The background worker: email queue now, scheduled backups if configured.

Optional on free hosting. Without it, emails go out straight after each
page saves them, and /ops/tick/ or send_mail_queue pick up retries.
"""

import logging
import signal
import time
from datetime import timedelta

import pghistory
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from mailer.services import send_pending
from ops.models import BackupRun

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the background worker (email queue and scheduled backups)."

    def add_arguments(self, parser):
        parser.add_argument("--sleep", type=float, default=5.0)

    def handle(self, *args, sleep, **options):
        self.running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self.stdout.write("Worker started.")
        while self.running:
            close_old_connections()
            try:
                with pghistory.context(source="worker"):
                    send_pending(limit=settings.MAIL_RATE_PER_MINUTE)
                    self._maybe_backup()
            except Exception:  # noqa: BLE001 - keep the worker alive, log and carry on
                log.exception("Worker loop failed")
            time.sleep(sleep)
        self.stdout.write("Worker stopped.")

    def _stop(self, *args):
        self.running = False

    def _maybe_backup(self):
        if not settings.BACKUP_RCLONE_REMOTES or not settings.BACKUP_AGE_RECIPIENT:
            return
        last = BackupRun.objects.first()
        due = timedelta(minutes=settings.BACKUP_INTERVAL_MINUTES)
        if last is None or last.started_at < timezone.now() - due:
            call_command("backup_now", triggered_by="worker")
