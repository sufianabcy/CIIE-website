"""The four named Excel files. Each one says it is a copy, not the record."""

from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from checkin.models import CheckIn
from results.models import Result

from .models import Event

TEXT = "@"


def _local(dt):
    return timezone.localtime(dt).replace(tzinfo=None) if dt else None


def _who(person):
    return person.full_name or person.email if person else ""


def _sheet(wb, title, headers, rows, text_columns=()):
    ws = wb.active if wb.active.title == "Sheet" else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    text_idx = {headers.index(h) + 1 for h in text_columns if h in headers}
    for row in rows:
        ws.append(row)
        if text_idx:
            r = ws.max_row
            for idx in text_idx:
                cell = ws.cell(row=r, column=idx)
                if cell.value is not None:
                    cell.value = str(cell.value)
                cell.number_format = TEXT
    for i, header in enumerate(headers, start=1):
        width = max([len(str(header))] + [len(str(c.value or "")) for c in ws[get_column_letter(i)][1:200]])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 50)
    return ws


def _about(wb, event, kind, user):
    ws = wb.create_sheet("About this file")
    lines = [
        ("This file is a copy, not the record.", ""),
        ("The record lives in the portal. Fix mistakes there, then export again.", ""),
        ("Event", event.title),
        ("Event date", _local(event.starts_at)),
        ("File", kind),
        ("Exported at", _local(timezone.now())),
        ("Exported by", _who(user)),
        ("Practice event", "Yes: keep out of reports" if event.is_practice else "No"),
        ("Portal", settings.SITE_URL),
    ]
    for line in lines:
        ws.append(list(line))
    ws["A1"].font = Font(bold=True)
    ws.column_dimensions["A"].width = 70
    ws.column_dimensions["B"].width = 40


def id_label(event) -> str:
    """Hackathons have no passes; the same unique code is shown as a registration ID."""
    return "Registration ID" if event.is_hackathon else "Pass code"


def _person_columns(p):
    return [p.full_name, p.email or "", p.phone, p.institution, p.branch, p.year_of_study, p.student_id, "Yes" if p.is_kl_verified else "No"]


PERSON_HEADERS = ["Name", "Email", "Phone", "Institution", "Branch", "Year", "KL ID", "KL verified"]


def registrations_workbook(event, wb):
    questions = list(event.questions.all())
    code = id_label(event)
    headers = [code, "Status", "Source", "Registered at"] + PERSON_HEADERS + ["Form version"]
    headers += [f"{q.label} [{q.key}]" for q in questions]
    rows = []
    for r in event.registrations.select_related("person").order_by("created_at"):
        answers = r.answers or {}
        row = [r.pass_code_display, r.get_status_display(), r.get_source_display(), _local(r.created_at)]
        row += _person_columns(r.person) + [r.form_version]
        for q in questions:
            value = answers.get(q.key, "")
            row.append(", ".join(value) if isinstance(value, list) else value)
        rows.append(row)
    _sheet(wb, "Registrations", headers, rows, text_columns=["Phone", "KL ID", code])


def attendance_workbook(event, wb):
    headers = PERSON_HEADERS + ["How", "Gate", "Checked in at", "Checked in by", "Recorded at", "Pass code"]
    checkins = CheckIn.objects.filter(event=event).select_related("registration__person", "checked_in_by", "undone_by")
    rows = []
    undone = []
    for c in checkins.order_by("checked_in_at"):
        base = _person_columns(c.registration.person) + [
            c.get_method_display(), c.gate, _local(c.checked_in_at), _who(c.checked_in_by), _local(c.recorded_at), c.registration.pass_code_display,
        ]
        if c.undone_at:
            undone.append(base + [_local(c.undone_at), _who(c.undone_by), c.undo_reason])
        else:
            rows.append(base)
    _sheet(wb, "Attendance", headers, rows, text_columns=["Phone", "KL ID", "Pass code"])
    _sheet(wb, "Undone check-ins", headers + ["Undone at", "Undone by", "Reason"], undone, text_columns=["Phone", "KL ID", "Pass code"])


def results_workbook(event, wb):
    headers = ["Rank", "Position", "Status"] + PERSON_HEADERS + ["Note", "Drafted by", "Checked by", "Published at"]
    rows = []
    for r in Result.objects.filter(event=event).select_related("registration__person", "drafted_by", "checked_by"):
        rows.append(
            [r.rank, r.position, r.get_status_display()]
            + _person_columns(r.registration.person)
            + [r.note, _who(r.drafted_by), _who(r.checked_by), _local(r.published_at)]
        )
    _sheet(wb, "Results", headers, rows, text_columns=["Phone", "KL ID"])


def handoff_workbook(event, wb):
    """The attendance hand-off for points: one row per attendee, KL IDs first."""
    headers = ["KL ID", "Name", "Email", "Event", "Event date", "Earns points", "Points note", "How checked in"]
    rows = []
    checkins = (
        CheckIn.objects.filter(event=event, undone_at__isnull=True)
        .select_related("registration__person")
        .order_by("registration__person__student_id", "registration__person__full_name")
    )
    for c in checkins:
        p = c.registration.person
        rows.append(
            [p.student_id, p.full_name, p.kl_email or p.email or "", event.title, _local(event.starts_at).date(),
             "Yes" if event.earns_points else "No", event.points_note, c.get_method_display()]
        )
    _sheet(wb, "Attendance hand-off", headers, rows, text_columns=["KL ID"])


def marks_workbook(event, wb):
    from hackathon.exports import marks_workbook as build

    build(event, wb)


def allocations_workbook(event, wb):
    from hackathon.exports import allocations_workbook as build

    build(event, wb)


def teams_workbook(event, wb):
    from hackathon.exports import teams_workbook as build

    build(event, wb)


EXPORTS = {
    "registrations": ("Registrations", registrations_workbook),
    "teams": ("Teams, rooms and mentors", teams_workbook),
    "allocations": ("Faculty allocations", allocations_workbook),
    "marks": ("Faculty marks by round", marks_workbook),
    "attendance": ("Attendance", attendance_workbook),
    "results": ("Results", results_workbook),
    "attendance-handoff": ("Attendance hand-off", handoff_workbook),
}


HACKATHON_EXPORTS = {"teams", "allocations", "marks"}
DOOR_EXPORTS = {"attendance", "attendance-handoff"}  # no door check-in at hackathons


def build_export(event: Event, kind: str, user) -> tuple[str, bytes]:
    label, builder = EXPORTS[kind]
    wb = Workbook()
    builder(event, wb)
    _about(wb, event, label, user)
    buffer = BytesIO()
    wb.save(buffer)
    stamp = timezone.localtime().strftime("%Y-%m-%d-%H%M")
    practice = "-PRACTICE" if event.is_practice else ""
    return f"{event.slug}{practice}-{kind}-{stamp}.xlsx", buffer.getvalue()


@login_required
def export(request, slug, kind):
    from hackathon.access import allowed

    event = get_object_or_404(Event, slug=slug)
    if event.is_hackathon:
        if not allowed(request.user, event, "can_download"):
            raise PermissionDenied
        if kind == "marks" and not allowed(request.user, event, "can_view_marks"):
            raise PermissionDenied
    elif not event.coordinated_by(request.user):
        raise PermissionDenied
    if kind in HACKATHON_EXPORTS and not event.is_hackathon:
        raise Http404
    if kind in DOOR_EXPORTS and event.is_hackathon:
        raise Http404
    if kind not in EXPORTS:
        raise Http404
    filename, content = build_export(event, kind, request.user)
    response = HttpResponse(
        content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
