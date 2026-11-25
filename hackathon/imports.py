"""The Excel import steps: upload, match columns to fields, check every row, import the valid rows.

Works the same for students, faculty, supervisors and allocations. The fields come from the
hackathon's field definitions (hackathon.fields), so custom fields can be matched too.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import BytesIO

from django.db import transaction
from openpyxl.utils import get_column_letter

from people.models import normalise_email

from . import services
from .fields import clean_value, ensure_fields

KINDS = {"students": "Students", "faculty": "Faculty", "supervisors": "Supervisors", "volunteers": "Volunteers",
         "allocations": "Allocations", "rooms": "Rooms"}


@dataclass
class Target:
    key: str  # the data key the import functions use: name, email, branch, f:<custom>, q:<question>, team, ...
    label: str
    required: bool = False
    aliases: list = field(default_factory=list)
    defn: object = None  # a FieldDefinition when values are checked by type


def _k(text) -> str:
    return services._key(text)


def col_name(i: int, headers) -> str:
    header = headers[i] if i < len(headers) else ""
    return f"{get_column_letter(i + 1)} (column {i + 1}): {header or '(no header)'}"


def targets(event, kind, user=None) -> list[Target]:
    out = []
    if kind in ("students", "faculty"):
        applies = "student" if kind == "students" else "faculty"
        builtin_aliases = services.PERSON_COLUMNS
        for d in ensure_fields(event, applies):
            if d.builtin:
                key = "branch" if d.builtin == "department" else d.builtin
                aliases = builtin_aliases.get(key, []) + [_k(d.label)]
                out.append(Target(key, d.label, d.required or d.always_required, aliases, d))
            else:
                out.append(Target(f"f:{d.key}", d.label, d.required, [_k(d.label), _k(d.key)], d))
        if kind == "students":
            for key in ("team", "project_title", "project_description", "room", "mentor"):
                out.append(Target(key, key.replace("_", " ").capitalize(), False, services.STUDENT_COLUMNS[key]))
            for q in event.questions.filter(is_hidden=False):
                out.append(Target(f"q:{q.key}", f"Question: {q.label}", False, [_k(q.label), _k(q.key)]))
    elif kind == "rooms":
        for key, label in (("name", "Room code (like R206)"), ("capacity", "Max teams"), ("block", "Block"), ("floor", "Floor"), ("notes", "Notes")):
            out.append(Target(key, label, key == "name", services.ROOM_COLUMNS[key]))
    elif kind == "volunteers":
        from .access import VOLUNTEER_PERMISSIONS

        for key, label in (("name", "Name"), ("email", "Email"), ("student_id", "KL ID"), ("employee_id", "Employee ID"), ("phone", "Phone")):
            out.append(Target(key, label, key == "name", services.STAFF_COLUMNS.get(key, [])))
        for key, aliases in services._volunteer_switch_columns().items():
            out.append(Target(key, f"Permission: {VOLUNTEER_PERMISSIONS[key][0]} (Yes/No)", False, aliases))
    elif kind == "supervisors":
        for key, label in (("name", "Name"), ("email", "Email"), ("employee_id", "Employee ID"), ("student_id", "KL ID"),
                           ("branch", "Department"), ("phone", "Phone"), ("kind", "Type (Faculty or Student)")):
            out.append(Target(key, label, key == "name", services.STAFF_COLUMNS.get(key, [])))
        for key, aliases in services._switch_columns().items():
            from .access import PERMISSIONS

            out.append(Target(key, f"Permission: {PERMISSIONS[key][0]} (Yes/No)", False, aliases))
    else:
        for key, label in (("round", "Round"), ("faculty", "Faculty (email, employee ID, login ID or name)"),
                           ("team", "Team"), ("member", "Or a team member (email or KL ID)"), ("room", "Or a whole room")):
            out.append(Target(key, label, key == "faculty", services.ALLOCATION_COLUMNS[key]))
    return out


def read(content):
    return services.read_rows(BytesIO(bytes(content)))


def auto_match(headers, targets_, saved=None) -> dict:
    """{target key: column index or None}: the saved mapping's header first, then by name."""
    keys = [_k(h) for h in headers]
    used, mapping = set(), {}
    for t in targets_:
        choice = None
        remembered = (saved or {}).get(t.key)
        if remembered and _k(remembered) in keys:
            choice = keys.index(_k(remembered))
        if choice is None:
            for alias in [_k(t.label)] + list(t.aliases):
                if alias in keys and keys.index(alias) not in used:
                    choice = keys.index(alias)
                    break
        if choice is not None and choice in used:
            choice = None
        mapping[t.key] = choice
        if choice is not None:
            used.add(choice)
    return mapping


def sheet_checks(headers, rows, mapping, targets_, kind):
    """(errors that block the import, warnings)."""
    errors, warnings = [], []
    if not headers:
        return ["The first sheet is empty."], []
    if not rows:
        errors.append("The sheet has a header row but no data rows.")
    for t in targets_:
        if t.required and mapping.get(t.key) is None:
            errors.append(f"\"{t.label}\" is required, but no column is matched to it.")
    if kind == "students" and mapping.get("email") is None and mapping.get("student_id") is None:
        errors.append("Match an Email or a KL ID column, so each student can be matched or created.")
    if kind == "allocations" and all(mapping.get(k) is None for k in ("room", "team", "member")):
        errors.append("Match a Team, a team member or a Room column.")
    chosen = [i for i in mapping.values() if i is not None]
    for i, n in Counter(chosen).items():
        if n > 1:
            errors.append(f"Column {col_name(i, headers)} is matched to more than one field.")
    counts = Counter(_k(h) for h in headers if h)
    for name, n in counts.items():
        if n > 1:
            cols = ", ".join(get_column_letter(i + 1) for i, h in enumerate(headers) if _k(h) == name)
            warnings.append(f"Columns {cols} have the same header \"{name}\". Check which one is matched.")
    for i, h in enumerate(headers):
        if not h and any(i < len(r) and r[i] for r in rows):
            warnings.append(f"Column {get_column_letter(i + 1)} (column {i + 1}) has data but no header.")
    extra = [col_name(i, headers) for i, h in enumerate(headers) if h and i not in chosen]
    if extra:
        warnings.append("Not matched to any field, so ignored: " + "; ".join(extra) + ". Match them below to use them.")
    return errors, warnings


def row_checks(event, kind, headers, rows, mapping, targets_) -> dict:
    """{sheet row: [problems]} before anything is saved: types, required values, choices, duplicates, clashes."""
    problems = defaultdict(list)
    by_key = {t.key: t for t in targets_}
    seen = defaultdict(dict)
    for line, row in enumerate(rows, start=2):
        for key, i in mapping.items():
            if i is None:
                continue
            t = by_key[key]
            raw = row[i] if i < len(row) else ""
            where = f"column {get_column_letter(i + 1)} ({headers[i] or t.label})"
            if t.defn is not None:
                _, error = clean_value(t.defn, raw)
                if error:
                    problems[line].append(f"Row {line}, {where}: {error}.")
            elif t.required and not raw:
                problems[line].append(f"Row {line}, {where}: required, but empty.")
            if key in ("email", "student_id", "employee_id") and raw and kind not in ("allocations", "rooms"):
                value = normalise_email(raw) if key == "email" else raw.strip().lower()
                if value in seen[key]:
                    problems[line].append(f"Row {line}, {where}: \"{raw}\" is also in row {seen[key][value]}.")
                else:
                    seen[key][value] = line
        if kind in ("students", "faculty", "supervisors", "volunteers"):
            data = {k: (row[i] if i is not None and i < len(row) else "") for k, i in mapping.items()}
            _, clash = services._find_person(normalise_email(data.get("email")) or "", (data.get("student_id") or "").strip(),
                                             (data.get("employee_id") or "").strip())
            if clash:
                problems[line].append(f"Row {line}: {clash}.")
    # What the import itself would refuse (unknown round, a mentor judging their team, ...), tried and rolled back.
    report = _dry_run(event, kind, mapping, rows, headers, skip=set(problems))
    for message in report.problems:
        m = re.match(r"Row (\d+)", message)
        if m:
            problems[int(m.group(1))].append(message)
    return dict(problems)


class _Rollback(Exception):
    pass


def _columns(mapping):
    return {i: key for key, i in mapping.items() if i is not None}


def _run(event, kind, content_or_rows, mapping, only_rows, user):
    upload = content_or_rows
    columns = _columns(mapping)
    if kind == "students":
        return services.import_students(event, upload, by=user, columns=columns, only_rows=only_rows)
    if kind == "faculty":
        return services.import_staff(event, upload, "faculty", columns=columns, only_rows=only_rows)
    if kind == "supervisors":
        return services.import_staff(event, upload, "supervisor", columns=columns, only_rows=only_rows)
    if kind == "volunteers":
        return services.import_staff(event, upload, "volunteer", columns=columns, only_rows=only_rows)
    if kind == "rooms":
        return services.import_rooms(event, upload, columns=columns, only_rows=only_rows)
    return services.import_allocations(event, upload, columns=columns, only_rows=only_rows)


def _dry_run(event, kind, mapping, rows, headers, skip):
    """Run the import inside a transaction that is always rolled back, to collect its row problems."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(headers)
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    only = {line for line in range(2, len(rows) + 2) if line not in skip}
    result = services.ImportReport()
    try:
        with transaction.atomic():
            buffer.seek(0)
            result = _run(event, kind, buffer, mapping, only, None)
            raise _Rollback
    except _Rollback:
        pass
    return result


def run_import(upload, mapping, only_rows, user):
    report = _run(upload.event, upload.kind, BytesIO(bytes(upload.content)), mapping, only_rows, user)
    return report


def remember(event, kind, headers, mapping):
    from .models import ImportMapping

    saved = {key: headers[i] for key, i in mapping.items() if i is not None and i < len(headers)}
    ImportMapping.objects.update_or_create(event=event, kind=kind, defaults={"mapping": saved})


def saved_mapping(event, kind) -> dict:
    from .models import ImportMapping

    row = ImportMapping.objects.filter(event=event, kind=kind).first()
    return row.mapping if row else {}


def skipped_workbook(upload) -> bytes:
    from openpyxl import Workbook

    headers, rows = read(upload.content)
    skipped = upload.result.get("skipped", {})
    wb = Workbook()
    ws = wb.active
    ws.title = "Skipped rows"
    ws.append(["Sheet row", "Why it was skipped"] + headers)
    for line, reasons in sorted(skipped.items(), key=lambda kv: int(kv[0])):
        index = int(line) - 2
        ws.append([int(line), " ".join(reasons)] + (rows[index] if 0 <= index < len(rows) else []))
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()

