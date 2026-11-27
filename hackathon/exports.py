"""Excel files for hackathons: teams, allocations, final marks, logins, and blank templates."""

import re
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font

from events.exports import PERSON_HEADERS, _about, _local, _person_columns, _sheet

from .models import Assignment, EventFaculty, Round, Supervisor, Team, TeamMember, team_number_order
from .services import mentor_conflicts, room_rule_warnings


def _num(value):
    return float(value) if isinstance(value, Decimal) else value


def _answers(event):
    questions = list(event.questions.all())

    def values(registration):
        answers = registration.answers or {}
        return [", ".join(v) if isinstance(v := answers.get(q.key, ""), list) else v for q in questions]

    return [q.label for q in questions], values


def _where(team):
    """Block, floor and room of a team, for exports."""
    room = team.room
    return [room.block, room.floor, room.name] if room else ["", "", ""]


def _members(team):
    return ", ".join(m.registration.person.display_name for m in team.members.all())


def _custom(event, applies_to):
    from .fields import display, ensure_fields

    defs = [d for d in ensure_fields(event, applies_to) if not d.builtin]
    values = {p.person_id: p.values for p in event.person_details.all()}
    return [d.label for d in defs], lambda person: [display(d, values.get(person.pk, {}).get(d.key)) for d in defs]


def teams_workbook(event, wb):
    """One row per student with their team, project, room and mentor; then one row per team."""
    answer_headers, answer_values = _answers(event)
    custom_headers, custom_values = _custom(event, "student")
    members = sorted(
        TeamMember.objects.filter(team__event=event).select_related("team__room", "team__mentor", "registration__person"),
        key=lambda m: (team_number_order(m.team), m.registration.person.full_name.lower()),
    )
    _sheet(
        wb, "Students",
        ["Team no", "Team", "Project title", "Category", "Room", "Mentor", "Registration ID"] + PERSON_HEADERS + custom_headers + answer_headers,
        [
            [m.team.number, m.team.name, m.team.project_title, m.team.category, m.team.room.name if m.team.room else "",
             m.team.mentor_display, m.registration.pass_code_display] + _person_columns(m.registration.person)
            + custom_values(m.registration.person) + answer_values(m.registration)
            for m in members
        ],
        text_columns=["Team no", "Phone", "KL ID", "Registration ID"],
    )
    teams = sorted(Team.objects.filter(event=event).select_related("room", "mentor").prefetch_related("members__registration__person"),
                   key=team_number_order)
    slots = max([event.team_size_max or 1] + [t.members.count() for t in teams])
    headers = ["Team no", "Team", "Project title", "Project description", "Category", "Room", "Mentor", "Size"]
    for n in range(1, slots + 1):
        headers += [f"Member {n} name" + (" (leader)" if n == 1 else ""), f"Member {n} email", f"Member {n} KL ID"]
    rows = []
    for t in teams:
        row = [t.number, t.name, t.project_title, t.project_description, t.category, t.room.name if t.room else "", t.mentor_display,
               t.members.count()]
        for m in sorted(t.members.all(), key=lambda m: (not m.is_leader, m.registration.person.display_name)):
            p = m.registration.person
            row += [p.display_name, p.email or "", p.student_id]
        rows.append(row)
    _sheet(wb, "Teams", headers, rows, text_columns=["Team no"])


def allocations_workbook(event, wb):
    rows = (
        Assignment.objects.filter(round__event=event)
        .select_related("round", "faculty__person", "team__room")
        .order_by("round__position", "team__room__name", "faculty__person__full_name", "team__name")
    )
    _sheet(
        wb, "Allocations",
        ["Round", "Block", "Floor", "Room", "Faculty", "Faculty email", "Employee ID", "Team no", "Team", "Category", "How", "Status"],
        [
            [a.round.name, *_where(a.team), a.faculty.person.display_name, a.faculty.person.email or "",
             a.faculty.person.employee_id, a.team.number, a.team.name, a.team.category, a.get_source_display(),
             "Submitted" if a.submitted_at else "Not submitted"]
            for a in rows
        ],
        text_columns=["Employee ID", "Team no"],
    )
    problems = room_rule_warnings(event) + mentor_conflicts(event)
    _sheet(wb, "Warnings", ["Check these"], [[p] for p in problems] or [["None: every faculty member judges one room per round, and no mentor judges their own team."]])
    custom_headers, custom_values = _custom(event, "faculty")
    staff = [
        [f.person.display_name, f.person.email or "", f.person.employee_id, f.person.branch, "Faculty", f.assignments.count()]
        + custom_values(f.person)
        for f in EventFaculty.objects.filter(event=event).select_related("person").prefetch_related("assignments")
    ] + [
        [s.person.display_name, s.person.email or "", s.person.employee_id or s.person.student_id, s.person.branch,
         f"Supervisor ({s.get_kind_display()})", ""]
        for s in Supervisor.objects.filter(event=event).select_related("person")
    ]
    _sheet(wb, "Faculty and supervisors", ["Name", "Email", "Employee or KL ID", "Department", "Role", "Teams to judge"] + custom_headers,
           staff, text_columns=["Employee or KL ID"])


def _title(round_):
    return re.sub(r"[\[\]:*?/\\]", "", f"R{round_.position} {round_.name}")[:31]


def _banner(ws, text):
    """A line above the headers (for "marking in progress")."""
    ws.insert_rows(1)
    ws.cell(row=1, column=1, value=text).font = Font(bold=True, color="B45309")
    ws.freeze_panes = "A3"


def _advance_text(round_) -> str:
    parts = []
    if round_.min_total is not None:
        parts.append(f"at least {round_.min_total.normalize():f}")
    if round_.key_criterion_id and round_.key_min_band:
        parts.append(f"{round_.key_criterion.label} at least {round_.key_min_band}")
    text = " and ".join(parts)
    if round_.allow_override:
        text += ("; " if text else "") + "evaluators may recommend advancing anyway"
    return text


def marks_workbook(event, wb):
    """Final (one row per team, ranked), then one sheet per round (one row per evaluator per team), then the rubric."""
    from collections import Counter

    from . import rubric
    from .models import MarkChange
    from .rubric_file import band_rows

    rounds = list(Round.objects.filter(event=event).select_related("key_criterion").prefetch_related("criteria"))
    teams = sorted(Team.objects.filter(event=event).select_related("room").prefetch_related("members__registration__person"),
                   key=team_number_order)
    settings_row = rubric.settings_for(event)
    assignments = sorted(
        Assignment.objects.filter(round__event=event).select_related("round", "faculty__person", "team__room").prefetch_related("scores"),
        key=lambda a: (a.round.position, team_number_order(a.team), a.faculty.person.display_name.lower()),
    )
    submitted = [a for a in assignments if a.submitted_at]
    final_round = rounds[-1] if rounds else None

    # --- Final -----------------------------------------------------------------------------------
    rows = []
    for team in teams:
        people = sorted((m.registration.person for m in team.members.all()), key=lambda p: p.display_name)
        mine = [a for a in submitted if a.team_id == team.pk]
        percents, weighted, gap = [], Decimal(0), None
        for r in rounds:
            score = rubric.team_score(r, team)
            pct = rubric.percent(r, score["total"]) if score else None
            percents.append(pct)
            if pct is not None:
                weighted += pct * rubric.weight(r)
                if score["self_scores"] and settings_row.self_score_max:
                    self_pct = sum(score["self_scores"], Decimal(0)) / len(score["self_scores"]) * 100 / settings_row.self_score_max
                    gap = (pct - self_pct).quantize(Decimal("0.01"))
        complete = bool(rounds) and all(p is not None for p in percents)
        recs = Counter(a.recommendation for a in mine if final_round and a.round_id == final_round.pk and a.recommendation)
        rows.append({
            "cells": [team.number, team.name, team.project_title, team.category, ", ".join(p.display_name for p in people),
                      ", ".join(p.student_id or "" for p in people), team.room.name if team.room else ""]
                     + [_num(p) for p in percents] + [_num(weighted.quantize(Decimal("0.01"))) if complete else None],
            "tail": [rubric.furthest_round(team, rounds), _num(gap),
                     recs.most_common(1)[0][0] if recs else "", sum(len(a.red_flags or []) for a in mine)],
            "key": (sum(p is not None for p in percents), weighted),
        })
    rows.sort(key=lambda r: (-r["key"][0], -r["key"][1]))
    final_rows = [r["cells"] + [rank] + r["tail"] for rank, r in enumerate(rows, start=1)]
    ws = _sheet(
        wb, "Final",
        ["Team no", "Team", "Project title", "Category", "Members", "Members' KL IDs", "Room"]
        + [f"Round {r.position} (out of 100)" for r in rounds]
        + ["Weighted total (out of 100)", "Rank", "Furthest round reached", "Self-score gap (faculty % minus self-score %)",
           "Recommendation", "Red flags"],
        final_rows, text_columns=["Team no", "Members' KL IDs"],
    )
    if len(submitted) < len(assignments):
        _banner(ws, f"Marking in progress: {len(submitted)} of {len(assignments)} marksheets submitted")

    # --- One sheet per round ----------------------------------------------------------------------
    for r in rounds:
        criteria = list(r.criteria.all())
        prompts = rubric.prompts(r)
        headers = ["Team no", "Team", "Room", "Evaluator", "Employee ID"]
        for c in criteria:
            headers += [f"{c.label} (/{c.max_marks.normalize():f})", f"{c.label} band"]
        headers += ["Total", "Out of", "Band"] + prompts + ["Red flags", "Self-score", "Recommendation", "Recommends advancing anyway",
                                                            "Remarks", "Status", "Submitted at"]
        lines = []
        for a in (a for a in assignments if a.round_id == r.pk):
            marks = {s.criterion_id: s.marks for s in a.scores.all()}
            total = sum(marks.values(), Decimal(0)) if marks else None
            cells = [a.team.number, a.team.name, a.team.room.name if a.team.room else "", a.faculty.person.display_name,
                     a.faculty.person.employee_id]
            for c in criteria:
                cells += [_num(marks.get(c.pk)), rubric.band_for(event, marks.get(c.pk), c.max_marks)]
            cells += [_num(total), _num(r.out_of), rubric.band_for(event, total, r.out_of)]
            cells += [(a.comments or {}).get(p, "") for p in prompts]
            cells += [", ".join(a.red_flags or []), _num(a.self_score), a.recommendation, "Yes" if a.advance_anyway else "",
                      a.remarks, "Submitted" if a.submitted_at else ("Draft saved" if marks else "Not started"), _local(a.submitted_at)]
            lines.append(cells)
        ws = _sheet(wb, _title(r), headers, lines, text_columns=["Team no", "Employee ID"])
        done = sum(1 for a in assignments if a.round_id == r.pk and a.submitted_at)
        total_here = sum(1 for a in assignments if a.round_id == r.pk)
        if done < total_here:
            _banner(ws, f"Marking in progress: {done} of {total_here} marksheets submitted")

    # --- Rubric ------------------------------------------------------------------------------------
    lines = []
    for r in rounds:
        lines.append([r.name, "", "", _num(r.total_marks), "", "", _num(rubric.weight(r)), r.focus, _advance_text(r),
                      " / ".join(rubric.prompts(r))])
        for c in r.criteria.all():
            lines.append(["", c.label, c.tag, _num(c.max_marks), c.strong_text, c.weak_text, None])
    lines.append([])
    lines.append(["Bands", "From % of the maximum", "To %", "Meaning"])
    lines += [[b["name"], b["min_percent"], b["max_percent"], b["meaning"]] for b in band_rows(event)]
    lines.append([])
    lines.append(["Student self-score out of", settings_row.self_score_max or "none"])
    lines.append(["Recommendations", "; ".join(settings_row.recommendations or [])])
    lines.append(["Red flags", "; ".join(settings_row.red_flags or [])])
    if settings_row.guidance:
        lines.append(["Guidance for faculty", settings_row.guidance])
    _sheet(wb, "Rubric", ["Round", "Criterion", "Tag", "Max marks", "Strong", "Weak", "Round weight", "Focus", "Advance rule",
                          "Comment prompts"], lines)

    changes = MarkChange.objects.filter(assignment__round__event=event).select_related(
        "assignment__round", "assignment__team", "assignment__faculty__person", "changed_by"
    ).order_by("changed_at")
    _sheet(
        wb, "Mark changes",
        ["Changed at", "Changed by", "What", "Round", "Team no", "Team", "Faculty", "Reason", "Before", "After"],
        [
            [_local(c.changed_at), c.changed_by.display_name, c.get_kind_display(), c.assignment.round.name,
             c.assignment.team.number, c.assignment.team.name,
             c.assignment.faculty.person.display_name, c.reason,
             ", ".join(f"{k}: {v:g}" for k, v in c.before.items()), ", ".join(f"{k}: {v:g}" for k, v in c.after.items())]
            for c in changes
        ],
        text_columns=["Team no"],
    )


def credentials_file(event, issued, user) -> tuple[str, bytes]:
    """issued: [(person, roles, password)]. The only place temporary passwords are ever shown."""
    wb = Workbook()
    url = settings.SITE_URL + reverse("signin")
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
    "students": ("Students", ["Name", "Email", "Phone", "KL ID", "Branch", "Year", "Team No", "Team name", "Project title", "Category",
                              "Project description", "Room", "Mentor"],
                 ["A. Student", "2300012345@kluniversity.in", "9000000000", "2300012345", "CSE", "3", "1", "Team Rocket", "Smart attendance",
                  "Campus", "Face-based attendance for labs.", "R&D 101", "faculty@kluniversity.in"]),
    "faculty": ("Faculty", ["Name", "Email", "Employee ID", "Department", "Phone"],
                ["Dr. A. Example", "a.example@kluniversity.in", "KLF1234", "CSE", "9000000000"]),
    "supervisors": ("Supervisors", ["Name", "Email", "Employee ID", "KL ID", "Department", "Phone", "Type", "Change marks", "Publish results"],
                    ["Dr. B. Example", "b.example@kluniversity.in", "KLF5678", "", "ECE", "9000000001", "Faculty", "No", "No"]),
    "allocations": ("Allocations", ["Round", "Faculty email", "Team", "Member email or KL ID"],
                    ["Round 1", "a.example@kluniversity.in", "Team Rocket", ""]),
    "rooms": ("Rooms", ["Room", "Max teams"], ["L502", "4"]),
    "volunteers": ("Volunteers", ["Name", "Email", "KL ID", "Phone", "Team attendance", "Faculty attendance", "See marking progress", "See floor lists"],
                   ["A. Volunteer", "2300012345@kluniversity.in", "2300012345", "9000000000", "Yes", "Yes", "Yes", "Yes"]),
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
