#!/bin/sh
# The background worker: email queue and scheduled backups. Optional on free hosting.
set -e
exec python manage.py run_worker
