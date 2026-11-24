"""pg_dump, encrypted with age, copied by rclone to one or more places.

Needs pg_dump, age and rclone on the PATH (they are in the Docker image),
BACKUP_AGE_RECIPIENT (an age public key) and BACKUP_RCLONE_REMOTES (for
example "gdrive:klciie-backups,onedrive:klciie-backups"). The private age key
stays offline with the keyholders.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ops.models import BackupRun

COUNTED = ["people.Person", "events.Event", "registrations.Registration", "checkin.CheckIn", "results.Result"]


def pg_env() -> dict:
    db = settings.DATABASES["default"]
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": db.get("HOST") or "localhost",
            "PGPORT": str(db.get("PORT") or 5432),
            "PGUSER": db.get("USER") or "",
            "PGPASSWORD": db.get("PASSWORD") or "",
            "PGDATABASE": db.get("NAME") or "",
        }
    )
    options = db.get("OPTIONS") or {}
    if options.get("sslmode"):
        env["PGSSLMODE"] = options["sslmode"]
    if options.get("channel_binding"):
        env["PGCHANNELBINDING"] = options["channel_binding"]
    return env


class Command(BaseCommand):
    help = "Back up the database now: pg_dump | age | rclone to every configured remote."

    def add_arguments(self, parser):
        parser.add_argument("--triggered-by", default="command")

    def handle(self, *args, triggered_by, **options):
        remotes = settings.BACKUP_RCLONE_REMOTES
        recipient = settings.BACKUP_AGE_RECIPIENT
        if not remotes or not recipient:
            raise CommandError("Set BACKUP_RCLONE_REMOTES and BACKUP_AGE_RECIPIENT first.")
        run = BackupRun.objects.create(triggered_by=triggered_by, destinations=", ".join(remotes))
        stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
        name = f"klciie-{stamp}.dump.age"
        try:
            counts = {label: apps.get_model(label).objects.count() for label in COUNTED}
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                with open(path, "wb") as out:
                    dump = subprocess.Popen(["pg_dump", "--format=custom", "--no-owner"], stdout=subprocess.PIPE, env=pg_env())
                    encrypt = subprocess.run(["age", "--encrypt", "-r", recipient], stdin=dump.stdout, stdout=out, check=False)
                    dump.stdout.close()
                    dump_rc = dump.wait()
                if dump_rc != 0 or encrypt.returncode != 0:
                    raise CommandError(f"pg_dump exited {dump_rc}, age exited {encrypt.returncode}")
                size = path.stat().st_size
                for remote in remotes:
                    subprocess.run(["rclone", "copyto", str(path), f"{remote.rstrip('/')}/{name}"], check=True)
        except Exception as exc:
            run.ok = False
            run.error = f"{type(exc).__name__}: {exc}"[:4000]
            run.finished_at = timezone.now()
            run.save()
            raise CommandError(run.error) from exc
        run.ok = True
        run.size_bytes = size
        run.row_counts = counts
        run.finished_at = timezone.now()
        run.save()
        host = urlparse(settings.SITE_URL).hostname
        self.stdout.write(f"Backup {name} ({size} bytes) from {host} copied to {len(remotes)} place(s).")
