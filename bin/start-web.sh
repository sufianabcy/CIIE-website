#!/bin/sh
# Start the website. Runs migrations first (quick when there is nothing new).
set -e
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  python manage.py migrate --noinput
  python manage.py createcachetable
  python manage.py ensure_admin
fi
if [ "${RUN_WORKER_IN_WEB:-0}" = "1" ]; then
  # Free hosting has no separate worker: run it beside the web process.
  python manage.py run_worker &
fi
exec gunicorn config.wsgi \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${WEB_THREADS:-4}" \
  --timeout 60 \
  --access-logfile -
