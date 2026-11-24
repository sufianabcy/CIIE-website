"""Excel files for judging: marks, faculty and mapping, login codes, and blank templates."""

from collections import defaultdict
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from events.exports import PERSON_HEADERS, _about, _local, _person_columns, _sheet

from .models import Assignment, Criterion, Judge


def _num(value):
    return float(value) if isinstance(value, Decimal) else value


def _answers(event):
    questions = list(event.questions.all())
    headers = [q.label for q in questions]

    def values(registration):
        answers = registration.answers or {}
        out = []
        for q in questions:
            v = answers.get(q.key, "")
            out.append(", ".join(v) if isinstance(v, list) else v)
        return out

    return headers, values


def _status(assignment, scored):
    if assignment.submitted_at:
        return "Submitted"
    return "Draft saved" if scored else "Not started"


def marks_workbook(event, wb):
    criteria = list(Criterion.objects.filter(event=event))
    answer_headers, answer_values = _answers(event)
    crit_headers = [f"{c.label} (/{c.max_marks.normalize():f})" for c in criteria]
    out_of = sum((c.max_marks for c in criteria), Decimal("0"))

    assignments = (
        Assignment.objects.filter(judge__event=event)
        .select_related("judge", "registration__person")
        .prefetch_related("scores")
        .order_by("judge__name", "registration__created_at")
    )
    rows = []
    per_registration = defaultdict(list)
    for a in assignments:
        marks = {s.criterion_id: s.marks for s in a.scores.all()}
        total = sum(marks.values(), Decimal("0"))
        rows.append(
            [a.judge.name, a.judge.email, a.judge.employee_id, a.judge.department, a.registration.pass_code_display]
            + _person_columns(a.registration.person)
            + answer_values(a.registration)
            + [_num(marks.get(c.pk)) for c in criteria]
            + [_num(total) if marks else None, _num(out_of), a.remarks, _status(a, marks), _local(a.submitted_at)]
        )
        if a.submitted_at:
            per_registration[a.registration_id].append((a.registration, marks, total))
    headers = (
        ["Faculty", "Faculty email", "Faculty employee ID", "Faculty department", "Pass code"]
        + PERSON_HEADERS + answer_headers + crit_headers
        + ["Total", "Out of", "Remarks", "Status", "Submitted at"]
    )
    _sheet(wb, "Marks by faculty", headers, rows, text_columns=["Phone", "KL ID", "Pass code", "Faculty employee ID"])

    # One row per student: averages of the submitted marks, best first.
    summary = []
    for items in per_registration.values():
        registration = items[0][0]
        n = len(items)
        averages = []
        for c in criteria:
            given = [m[c.pk] for _, m, _ in items if c.pk in m]
            averages.append(round(float(sum(given) / len(given)), 2) if given else None)
        average_total = round(float(sum(t for _, _, t in items) / n), 2)
        summary.append(
            [registration.pass_code_display] + _person_columns(registration.person) + answer_values(registration)
            + [n] + averages + [average_total, _num(out_of)]
        )
    summary.sort(key=lambda r: -r[-2])
    for rank, row in enumerate(summary, start=1):
        row.insert(0, rank)
    _sheet(
        wb, "Summary (submitted only)",
        ["Rank", "Pass code"] + PERSON_HEADERS + answer_headers + ["Faculty who submitted"]
        + [f"Average {h}" for h in crit_headers] + ["Average total", "Out of"],
        summary, text_columns=["Phone", "KL ID", "Pass code"],
    )


def faculty_workbook(event, wb):
    judges = Judge.objects.filter(event=event).prefetch_related("assignments")
    rows = [
        [j.name, j.email, j.employee_id, j.department, j.phone, j.login_id or "", "Yes" if j.has_credentials else "No",
         _local(j.credentials_expire_at), len(j.assignments.all()), sum(1 for a in j.assignments.all() if a.submitted_at)]
        for j in judges
    ]
    _sheet(
        wb, "Faculty",
        ["Name", "Email", "Employee ID", "Department", "Phone", "Login ID", "Has access code", "Code expires", "Students mapped", "Marks submitted"],
        rows, text_columns=["Phone", "Employee ID"],
    )
    mapping = (
        Assignment.objects.filter(judge__event=event)
        .select_related("judge", "registration__person")
        .order_by("judge__name", "registration__created_at")
    )
    _sheet(
        wb, "Student-faculty mapping",
        ["Pass code", "Student", "Student email", "KL ID", "Faculty", "Faculty email", "Faculty employee ID", "Status"],
        [
            [a.registration.pass_code_display, a.registration.person.full_name, a.registration.person.email or "",
             a.registration.person.student_id, a.judge.name, a.judge.email, a.judge.employee_id,
             "Submitted" if a.submitted_at else "Not submitted"]
            for a in mapping
        ],
        text_columns=["Pass code", "KL ID", "Faculty employee ID"],
    )


def credentials_file(event, issued, user) -> tuple[str, bytes]:
    """issued: list of (judge, plain code). The only place the codes are ever shown."""
    wb = Workbook()
    url = settings.SITE_URL + reverse("judge_signin")
    _sheet(
        wb, "Faculty logins",
        ["Name", "Email", "Employee ID", "Department", "Login ID", "Access code", "Sign in at", "Code expires"],
        [[j.name, j.email, j.employee_id, j.department, j.login_id, code, url, _local(j.credentials_expire_at)] for j, code in issued],
        text_columns=["Employee ID", "Access code", "Login ID"],
    )
    _about(wb, event, "Faculty logins (keep private: these are passwords)", user)
    buffer = BytesIO()
    wb.save(buffer)
    stamp = timezone.localtime().strftime("%Y-%m-%d-%H%M")
    return f"{event.slug}-faculty-logins-{stamp}.xlsx", buffer.getvalue()


def template_file(kind) -> tuple[str, bytes]:
    wb = Workbook()
    ws = wb.active
    if kind == "faculty":
        ws.title = "Faculty"
        ws.append(["Name", "Email", "Employee ID", "Department", "Phone"])
        ws.append(["Dr. A. Example", "a.example@kluniversity.in", "KL1234", "CSE", "9000000000"])
    else:
        ws.title = "Mapping"
        ws.append(["Pass code", "Faculty email"])
        ws.append(["ABCD-2345", "a.example@kluniversity.in"])
    for col in "ABCDE":
        ws.column_dimensions[col].width = 28
    buffer = BytesIO()
    wb.save(buffer)
    return f"{kind}-template.xlsx", buffer.getvalue()
