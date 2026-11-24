"""Excel files for hackathons: teams, allocations, marks per round, logins, and blank templates."""

import re
from collections import defaultdict
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from events.exports import PERSON_HEADERS, _about, _local, _person_columns, _sheet

from .models import Assignment, EventFaculty, Round, Supervisor, Team, TeamMember
from .services import mentor_conflicts, room_rule_warnings


def _num(value):
    return float(value) if isinstance(value, Decimal) else value


def _answers(event):
    questions = list(event.questions.all())

    def values(registration):
        answers = registration.answers or {}
        return [", ".join(v) if isinstance(v := answers.get(q.key, ""), list) else v for q in questions]

    return [q.label for q in questions], values


def _members(team):
    return ", ".join(m.registration.person.display_name for m in team.members.all())


def teams_workbook(event, wb):
    """One row per student with their team, project, room and mentor; then one row per team."""
    answer_headers, answer_values = _answers(event)
    members = (
        TeamMember.objects.filter(team__event=event)
        .select_related("team__room", "team__mentor", "registration__person")
        .order_by("team__room__name", "team__name", "registration__person__full_name")
    )
    _sheet(
        wb, "Students",
        ["Team", "Project title", "Room", "Mentor", "Pass code"] + PERSON_HEADERS + answer_headers,
        [
            [m.team.name, m.team.project_title, m.team.room.name if m.team.room else "", m.team.mentor_display,
             m.registration.pass_code_display] + _person_columns(m.registration.person) + answer_values(m.registration)
            for m in members
        ],
        text_columns=["Phone", "KL ID", "Pass code"],
    )
    teams = Team.objects.filter(event=event).select_related("room", "mentor").prefetch_related("members__registration__person")
    _sheet(
        wb, "Teams",
        ["Team", "Project title", "Project description", "Room", "Building / floor", "Mentor", "Members", "Size"],
        [
            [t.name, t.project_title, t.project_description, t.room.name if t.room else "",
             t.room.building if t.room else "", t.mentor_display, _members(t), t.members.count()]
            for t in teams
        ],
    )


def allocations_workbook(event, wb):
    rows = (
        Assignment.objects.filter(round__event=event)
        .select_related("round", "faculty__person", "team__room")
        .order_by("round__position", "team__room__name", "faculty__person__full_name", "team__name")
    )
    _sheet(
        wb, "Allocations",
        ["Round", "Room", "Faculty", "Faculty email", "Employee ID", "Team", "How", "Status"],
        [
            [a.round.name, a.team.room.name if a.team.room else "", a.faculty.person.display_name, a.faculty.person.email or "",
             a.faculty.person.employee_id, a.team.name, a.get_source_display(), "Submitted" if a.submitted_at else "Not submitted"]
            for a in rows
        ],
        text_columns=["Employee ID"],
    )
    problems = room_rule_warnings(event) + mentor_conflicts(event)
    _sheet(wb, "Warnings", ["Check these"], [[p] for p in problems] or [["None: every faculty member judges one room per round, and no mentor judges their own team."]])
    staff = [
        [f.person.display_name, f.person.email or "", f.person.employee_id, f.person.branch, "Faculty", f.assignments.count()]
        for f in EventFaculty.objects.filter(event=event).select_related("person").prefetch_related("assignments")
    ] + [
        [s.person.display_name, s.person.email or "", s.person.employee_id or s.person.student_id, s.person.branch,
         f"Supervisor ({s.get_kind_display()})", ""]
        for s in Supervisor.objects.filter(event=event).select_related("person")
    ]
    _sheet(wb, "Faculty and supervisors", ["Name", "Email", "Employee or KL ID", "Department", "Role", "Teams to judge"], staff, text_columns=["Employee or KL ID"])


def marks_workbook(event, wb):
    """Per round: one row per team per faculty member, then a per-team summary. Then an overall summary."""
    overall = defaultdict(dict)  # team -> {round: average}
    rounds = list(Round.objects.filter(event=event).prefetch_related("criteria"))
    teams = {t.pk: t for t in Team.objects.filter(event=event).select_related("room").prefetch_related("members__registration__person")}
    for round_ in rounds:
        criteria = list(round_.criteria.all())
        crit_headers = [f"{c.label} (/{c.max_marks.normalize():f})" for c in criteria]
        out_of = sum((c.max_marks for c in criteria), Decimal("0"))
        rows, submitted = [], defaultdict(list)
        for a in (
            Assignment.objects.filter(round=round_)
            .select_related("faculty__person", "team__room")
            .prefetch_related("scores")
            .order_by("team__room__name", "team__name", "faculty__person__full_name")
        ):
            marks = {s.criterion_id: s.marks for s in a.scores.all()}
            total = sum(marks.values(), Decimal("0"))
            status = "Submitted" if a.submitted_at else ("Draft saved" if marks else "Not started")
            rows.append(
                [a.team.name, a.team.room.name if a.team.room else "", a.faculty.person.display_name, a.faculty.person.employee_id]
                + [_num(marks.get(c.pk)) for c in criteria]
                + [_num(total) if marks else None, _num(out_of), a.remarks, status, _local(a.submitted_at)]
            )
            if a.submitted_at:
                submitted[a.team_id].append((marks, total))
        title = re.sub(r"[\[\]:*?/\\]", "", f"R{round_.position} {round_.name}")[:22]
        _sheet(
            wb, f"{title} marks",
            ["Team", "Room", "Faculty", "Employee ID"] + crit_headers + ["Total", "Out of", "Remarks", "Status", "Submitted at"],
            rows, text_columns=["Employee ID"],
        )
        summary = []
        for team_id, items in submitted.items():
            team = teams[team_id]
            averages = []
            for c in criteria:
                given = [m[c.pk] for m, _ in items if c.pk in m]
                averages.append(round(float(sum(given) / len(given)), 2) if given else None)
            average = round(float(sum(t for _, t in items) / len(items)), 2)
            overall[team_id][round_.pk] = average
            summary.append([team.name, team.room.name if team.room else "", _members(team), len(items)] + averages + [average, _num(out_of)])
        summary.sort(key=lambda r: -r[-2])
        for rank, row in enumerate(summary, start=1):
            row.insert(0, rank)
        _sheet(
            wb, f"{title} summary",
            ["Rank", "Team", "Room", "Members", "Faculty who submitted"] + [f"Average {h}" for h in crit_headers] + ["Average total", "Out of"],
            summary,
        )
    if rounds:
        rows = []
        for team_id, per_round in overall.items():
            team = teams[team_id]
            values = [per_round.get(r.pk) for r in rounds]
            rows.append([team.name, team.room.name if team.room else "", _members(team)] + values + [round(sum(v for v in values if v is not None), 2)])
        rows.sort(key=lambda r: -r[-1])
        for rank, row in enumerate(rows, start=1):
            row.insert(0, rank)
        _sheet(
            wb, "Overall",
            ["Rank", "Team", "Room", "Members"] + [f"{r.name} average" for r in rounds] + ["Sum of round averages"],
            rows,
        )
    else:
        _sheet(wb, "Overall", ["No rounds yet"], [])


def credentials_file(event, issued, user) -> tuple[str, bytes]:
    """issued: [(person, roles, password)]. The only place temporary passwords are ever shown."""
    wb = Workbook()
    url = settings.SITE_URL + reverse("staff_signin")
    _sheet(
        wb, "Logins",
        ["Name", "Role", "Email", "Login ID", "Temporary password", "Expires", "Sign in at", "Note"],
        [
            [p.display_name, roles, p.email or "", p.login_id, password, _local(p.temp_password_expires_at), url,
             "Sign in with your email or login ID. You'll choose your own password straight away."]
            for p, roles, password in issued
        ],
        text_columns=["Login ID", "Temporary password"],
    )
    _about(wb, event, "Faculty and supervisor logins (keep private: these are passwords)", user)
    buffer = BytesIO()
    wb.save(buffer)
    stamp = timezone.localtime().strftime("%Y-%m-%d-%H%M")
    return f"{event.slug}-logins-{stamp}.xlsx", buffer.getvalue()


TEMPLATES = {
    "students": ("Students", ["Name", "Email", "Phone", "KL ID", "Branch", "Year", "Team name", "Project title", "Project description", "Room", "Mentor"],
                 ["A. Student", "2300012345@kluniversity.in", "9000000000", "2300012345", "CSE", "3", "Team Rocket", "Smart attendance", "Face-based attendance for labs.", "R&D 101", "faculty@kluniversity.in"]),
    "faculty": ("Faculty", ["Name", "Email", "Employee ID", "Department", "Phone"],
                ["Dr. A. Example", "a.example@kluniversity.in", "KLF1234", "CSE", "9000000000"]),
    "supervisors": ("Supervisors", ["Name", "Email", "Employee ID", "KL ID", "Department", "Phone", "Type"],
                    ["Dr. B. Example", "b.example@kluniversity.in", "KLF5678", "", "ECE", "9000000001", "Faculty"]),
    "allocations": ("Allocations", ["Round", "Faculty email", "Room", "Team"],
                    ["Round 1", "a.example@kluniversity.in", "R&D 101", ""]),
}


def template_file(kind) -> tuple[str, bytes]:
    title, headers, sample = TEMPLATES[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(headers)
    ws.append(sample)
    for i in range(len(headers)):
        ws.column_dimensions[chr(65 + i)].width = 24
    buffer = BytesIO()
    wb.save(buffer)
    return f"{kind}-template.xlsx", buffer.getvalue()
