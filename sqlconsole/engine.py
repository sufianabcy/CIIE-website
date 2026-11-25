"""Running SQL from the admin safely.

Reads: one SELECT, WITH or EXPLAIN statement, in a READ ONLY transaction, with a timeout
and a row limit. Writes (superusers only): one INSERT, UPDATE or DELETE, first as a dry run
that is rolled back, then committed only when the person confirms the row count. Writes are
recorded in the change history (pghistory) as the admin, from "sql-console".
"""

import re
import time

import pghistory
import sqlparse
from django.apps import apps
from django.db import connection, transaction

TIMEOUT = "10s"
ROW_LIMIT = 500
PREVIEW_ROWS = 20
MASK = "•••••• (hidden)"

READ_STARTS = {"SELECT", "WITH", "EXPLAIN"}
WRITE_STARTS = {"INSERT", "UPDATE", "DELETE"}
# Checked as SQL keywords (not inside strings or names). The statement must also start with
# INSERT, UPDATE or DELETE, so none of these could run on their own anyway.
DANGEROUS = {"DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX", "CLUSTER"}
# Never read or written: sign-in secrets.
SECRET_WORDS = ("password", "secret", "token", "django_session", "socialaccount_socialapp", "account_emailconfirmation")
# Never written: accounts, permissions, sessions, and the change history itself.
PROTECTED_PREFIXES = ("auth_", "django_", "account_", "socialaccount_", "pghistory_", "pgtrigger_")
SECRET_COLUMN = re.compile(r"password|secret|token|session_key|session_data", re.I)
HASH_VALUE = re.compile(r"^(pbkdf2_|argon2|bcrypt|scrypt|sha1\$|md5\$|!)")


class Refused(Exception):
    pass


def _statement(sql: str) -> str:
    statements = [s for s in sqlparse.split(sql or "") if s.strip().strip(";").strip()]
    if not statements:
        raise Refused("Type a query first.")
    if len(statements) > 1:
        raise Refused("One statement at a time.")
    return statements[0].strip().rstrip(";").strip()


def _keywords(statement: str) -> set[str]:
    """SQL keywords in the statement, ignoring strings, comments and quoted names."""
    parsed = sqlparse.parse(statement)[0]
    return {t.normalized.upper() for t in parsed.flatten() if t.ttype is not None and t.ttype in sqlparse.tokens.Keyword}


def _first_word(statement: str) -> str:
    return statement.split(None, 1)[0].upper()


def _check_secrets(statement: str):
    lowered = statement.lower()
    for word in SECRET_WORDS:
        if word in lowered:
            raise Refused(f"Queries that touch sign-in secrets are refused (found \"{word}\").")


def history_tables() -> set[str]:
    from pghistory.models import Event

    return {m._meta.db_table for m in apps.get_models() if issubclass(m, Event)}


def check_read(sql: str) -> str:
    statement = _statement(sql)
    if _first_word(statement) not in READ_STARTS:
        raise Refused("Read mode runs one SELECT, WITH or EXPLAIN statement.")
    _check_secrets(statement)
    return statement


def write_target(statement: str) -> str:
    match = re.match(r"\s*(?:insert\s+into|update|delete\s+from)\s+(?:only\s+)?(\"?[\w.]+\"?)", statement, re.I)
    if not match:
        raise Refused("Couldn't tell which table this changes.")
    return match.group(1).strip('"').split(".")[-1].lower()


def check_write(sql: str) -> str:
    statement = _statement(sql)
    if _first_word(statement) not in WRITE_STARTS:
        raise Refused("Write mode runs one INSERT, UPDATE or DELETE statement.")
    bad = sorted(DANGEROUS & _keywords(statement))
    if bad:
        raise Refused(f"Refused: {', '.join(bad)} isn't allowed here.")
    _check_secrets(statement)
    table = write_target(statement)
    if table.startswith(PROTECTED_PREFIXES) or table in history_tables():
        raise Refused(f"The table {table} can't be changed from here (accounts, sessions and change history are protected).")
    return statement


def _clean(columns, rows):
    masked = [bool(SECRET_COLUMN.search(c)) for c in columns]
    out = []
    for row in rows:
        out.append([MASK if (masked[i] or (isinstance(v, str) and HASH_VALUE.match(v))) else v for i, v in enumerate(row)])
    return out


def run_read(sql: str, limit: int = ROW_LIMIT):
    """(columns, rows, truncated, ms). Raises Refused or a database error."""
    statement = check_read(sql)
    start = time.monotonic()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(f"SET LOCAL statement_timeout = '{TIMEOUT}'")
            cursor.execute(statement)
            columns = [c.name for c in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit + 1) if cursor.description else []
        transaction.set_rollback(True)
    truncated = len(rows) > limit
    return columns, _clean(columns, rows[:limit]), truncated, int((time.monotonic() - start) * 1000)


class _Rollback(Exception):
    pass


def run_write(sql: str, user, commit: bool, expected_rows: int | None = None):
    """(rows affected, preview columns, preview rows, ms). A dry run (commit=False) always rolls back.

    Committing re-runs the statement and keeps it only if it affects exactly expected_rows.
    """
    statement = check_write(sql)
    start = time.monotonic()
    result = {}
    try:
        with transaction.atomic():
            with pghistory.context(user=user.pk, source="sql-console"):
                with connection.cursor() as cursor:
                    cursor.execute(f"SET LOCAL statement_timeout = '{TIMEOUT}'")
                    preview_sql = statement if re.search(r"\breturning\b", statement, re.I) else f"{statement} RETURNING *"
                    cursor.execute(preview_sql)
                    result["rows"] = cursor.rowcount
                    result["columns"] = [c.name for c in cursor.description] if cursor.description else []
                    result["preview"] = _clean(result["columns"], cursor.fetchmany(PREVIEW_ROWS)) if cursor.description else []
            if not commit:
                raise _Rollback
            if expected_rows is None or result["rows"] != expected_rows:
                raise Refused(f"This now affects {result['rows']} rows, not {expected_rows}. Nothing was changed; run the dry run again.")
    except _Rollback:
        pass
    return result["rows"], result["columns"], result["preview"], int((time.monotonic() - start) * 1000)
