"""Reading faculty and mapping Excel files, and spreading students across faculty."""

import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from openpyxl import load_workbook

from people.models import normalise_email
from registrations.models import Registration, normalise_pass_code

from .models import Assignment, Judge

# The faculty Excel's final format is not fixed yet, so columns are matched by
# name. Add new spellings here; columns nobody recognises are kept in Judge.extra.
FACULTY_COLUMNS = {
    "name": ["name", "faculty name", "full name", "faculty", "name of the faculty", "judge", "judge name"],
    "email": ["email", "email id", "e-mail", "mail", "mail id", "faculty email", "email address", "official email"],
    "employee_id": ["employee id", "emp id", "empid", "emp no", "employee no", "employee number", "faculty id", "staff id", "id"],
    "department": ["department", "dept", "branch", "school"],
    "phone": ["phone", "mobile", "contact", "phone number", "mobile number", "contact number", "mobile no", "phone no"],
}

MAPPING_STUDENT_COLUMNS = ["pass code", "pass", "student email", "email", "kl id", "student id", "registration id", "id number"]
MAPPING_FACULTY_COLUMNS = ["faculty email", "faculty", "faculty login id", "login id", "faculty employee id", "employee id", "emp id", "faculty name", "judge", "mentor", "evaluator"]


def _key(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _rows(upload):
    """The first sheet as a header list and data rows, skipping blank rows above the header."""
    wb = load_workbook(upload, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self, noun: str) -> str:
        text = f"{self.created} {noun} added, {self.updated} updated."
        if self.problems:
            text += f" {len(self.problems)} row(s) skipped."
        return text


@transaction.atomic
def import_faculty(event, upload) -> ImportReport:
    report = ImportReport()
    headers, rows = _rows(upload)
    lookup = {alias: name for name, aliases in FACULTY_COLUMNS.items() for alias in aliases}
    columns = {}
    for i, header in enumerate(headers):
        name = lookup.get(_key(header))
        if name and name not in columns.values():
            columns[i] = name
    if "name" not in columns.values():
        report.problems.append("No column called Name (or Faculty name) in the first row of the first sheet.")
        return report
    for line, row in enumerate(rows, start=2):
        data = {name: row[i] if i < len(row) else "" for i, name in columns.items()}
        extra = {headers[i]: v for i, v in enumerate(row) if i not in columns and i < len(headers) and headers[i] and v}
        if not data.get("name"):
            report.problems.append(f"Row {line}: no name.")
            continue
        data["email"] = normalise_email(data.get("email")) or ""
        judge = None
        if data["email"]:
            judge = Judge.objects.filter(event=event, email__iexact=data["email"]).first()
        if not judge and data.get("employee_id"):
            judge = Judge.objects.filter(event=event, employee_id=data["employee_id"]).first()
        if not judge and not data["email"] and not data.get("employee_id"):
            judge = Judge.objects.filter(event=event, name__iexact=data["name"]).first()
        if judge:
            for name, value in data.items():
                if value:
                    setattr(judge, name, value)
            judge.extra = {**judge.extra, **extra}
            judge.save()
            report.updated += 1
        else:
            Judge.objects.create(event=event, extra=extra, **{k: v or "" for k, v in data.items()})
            report.created += 1
    return report


def find_registration(event, text):
    text = text.strip()
    code = normalise_pass_code(text)
    qs = Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
    if code and (found := qs.filter(pass_code=code).first()):
        return found
    if "@" in text:
        email = normalise_email(text)
        return qs.filter(person__email__iexact=email).first() or qs.filter(person__kl_email__iexact=email).first()
    return qs.filter(person__student_id=text).first() if text else None


def find_judge(event, text):
    text = text.strip()
    qs = Judge.objects.filter(event=event)
    if not text:
        return None
    if "@" in text:
        return qs.filter(email__iexact=text).first()
    return (
        qs.filter(login_id__iexact=text).first()
        or qs.filter(employee_id__iexact=text).first()
        or qs.filter(name__iexact=text).first()
    )


@transaction.atomic
def import_mapping(event, upload) -> ImportReport:
    """Rows of (student, faculty). Several rows may give one student several faculty."""
    report = ImportReport()
    headers, rows = _rows(upload)
    keys = [_key(h) for h in headers]

    def first(options):
        return next((keys.index(o) for o in options if o in keys), None)

    s_col, f_col = first(MAPPING_STUDENT_COLUMNS), first(MAPPING_FACULTY_COLUMNS)
    if s_col is not None and f_col == s_col:
        rest = [o for o in MAPPING_FACULTY_COLUMNS if o in keys and keys.index(o) != s_col]
        f_col = keys.index(rest[0]) if rest else None
    if s_col is None or f_col is None:
        report.problems.append("The first row needs a student column (Pass code, Student email or KL ID) and a faculty column (Faculty email, Login ID, Employee ID or Faculty name).")
        return report
    for line, row in enumerate(rows, start=2):
        student = row[s_col] if s_col < len(row) else ""
        faculty = row[f_col] if f_col < len(row) else ""
        registration = find_registration(event, student)
        judge = find_judge(event, faculty)
        if not registration:
            report.problems.append(f"Row {line}: no confirmed registration matches \"{student}\".")
            continue
        if not judge:
            report.problems.append(f"Row {line}: no faculty member matches \"{faculty}\". Upload the faculty list first.")
            continue
        _, created = Assignment.objects.get_or_create(judge=judge, registration=registration)
        if created:
            report.created += 1
    return report


@transaction.atomic
def auto_assign(event, per_student: int = 1) -> int:
    """Give every confirmed registration `per_student` faculty, keeping loads even. Returns new mappings."""
    judges = list(Judge.objects.filter(event=event, is_active=True))
    if not judges:
        return 0
    per_student = max(1, min(per_student, len(judges)))
    load = Counter(
        Assignment.objects.filter(judge__in=judges).values_list("judge_id", flat=True)
    )
    made = 0
    registrations = (
        Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
        .prefetch_related("judge_assignments")
        .order_by("created_at", "id")
    )
    for registration in registrations:
        have = {a.judge_id for a in registration.judge_assignments.all()}
        need = per_student - len(have)
        if need <= 0:
            continue
        choices = sorted((j for j in judges if j.pk not in have), key=lambda j: (load[j.pk], j.name, j.pk))
        for judge in choices[:need]:
            Assignment.objects.create(judge=judge, registration=registration)
            load[judge.pk] += 1
            made += 1
    return made
