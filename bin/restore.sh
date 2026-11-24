#!/bin/sh
# Restore an encrypted backup into an EMPTY database.
#   bin/restore.sh klciie-20261117T101500Z.dump.age age-key.txt "postgres://user:pass@host/newdb"
# Practise this on a scratch database every season: a backup never restored is not proven.
set -eu
if [ $# -ne 3 ]; then
  echo "usage: $0 BACKUP_FILE AGE_KEY_FILE TARGET_DATABASE_URL" >&2
  exit 2
fi
age --decrypt -i "$2" "$1" | pg_restore --no-owner --no-privileges --exit-on-error -d "$3"
echo "Restored $1. Run 'python manage.py migrate' against it, then check the row counts on the status page."
