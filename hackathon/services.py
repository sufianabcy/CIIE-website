"""Excel imports, room allocation and marks for hackathons.

The Excel layouts are not fixed yet, so every import matches columns by header
name (with common spellings), keeps columns it doesn't know as extra data, and
reports problems row by row instead of failing the whole file.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from openpyxl import load_workbook

from people.models import Person, is_kl_email, normalise_email
from people.staff import issue_temp_password
from registrations.models import Registration
from registrations.services import RegistrationError, register

from .models import Assignment, EventFaculty, Room, Round, Score, Supervisor, Team, TeamMember, room_conflict

# --- Reading Excel ------------------------------------------------------------

PERSON_COLUMNS = {
    "name": ["name", "full name", "student name", "participant name", "faculty name", "name of the faculty", "faculty", "judge", "judge name", "supervisor", "supervisor name"],
    "email": ["email", "email id", "e mail", "mail", "mail id", "email address", "student email", "faculty email", "official email", "kl email"],
    "phone": ["phone", "mobile", "contact", "phone number", "mobile number", "contact number", "mobile no", "phone no", "whatsapp"],
    "student_id": ["kl id", "student id", "id number", "university id", "roll no", "roll number", "registration number", "reg no", "regd no"],
    "employee_id": ["employee id", "emp id", "empid", "emp no", "employee no", "employee number", "faculty id", "staff id"],
    "branch": ["branch", "department", "dept", "school", "programme", "program"],
    "year": ["year", "year of study", "study year"],
    "institution": ["institution", "college", "university", "institute"],
}
STUDENT_COLUMNS = {
    **{k: PERSON_COLUMNS[k] for k in ("name", "email", "phone", "student_id", "branch", "year", "institution")},
    "team": ["team", "team name", "group", "group name"],
    "project_title": ["project", "project title", "project name", "title", "idea"],
    "project_description": ["project description", "description", "problem statement", "problem", "abstract", "idea description"],
    "room": ["room", "room no", "room number", "lab", "venue", "allocated room"],
    "mentor": ["mentor", "mentor name", "mentor email", "guide", "faculty mentor"],
}
STAFF_COLUMNS = {
    **{k: PERSON_COLUMNS[k] for k in ("name", "email", "phone", "employee_id", "branch", "student_id")},
    "kind": ["type", "role", "supervisor type", "faculty or student"],
}
ALLOCATION_COLUMNS = {
    "round": ["round", "round name", "round no", "round number"],
    "faculty": ["faculty", "faculty email", "faculty employee id", "employee id", "emp id", "faculty id", "login id", "faculty login id", "faculty name", "judge", "jury", "evaluator", "email"],
    "room": ["room", "room no", "room number", "lab", "venue"],
    "team": ["team", "team name", "group"],
}


def _key(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_rows(upload):
    """The first sheet as (headers, rows), skipping blank rows. Raises ImportFileError if unreadable."""
    try:
        wb = load_workbook(upload, read_only=True, data_only=True)
    except Exception as exc:  # a damaged or non-Excel file
        raise ImportFileError("That file could not be read as Excel (.xlsx).") from exc
    ws = wb.worksheets[0]
    rows = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def match_columns(headers, spec, also=None):
    """{column index: field} for headers we know, by exact alias; first match wins."""
    lookup = {}
    for name, aliases in spec.items():
        for alias in aliases:
            lookup.setdefault(alias, name)
    for alias, name in (also or {}).items():
        lookup.setdefault(_key(alias), name)
    columns = {}
    for i, header in enumerate(headers):
        name = lookup.get(_key(header))
        if name and name not in columns.values():
            columns[i] = name
    return columns


def _row_data(headers, row, columns):
    data = {name: (row[i] if i < len(row) else "") for i, name in columns.items()}
    extra = {headers[i]: v for i, v in enumerate(row) if i not in columns and i < len(headers) and headers[i] and v}
    return data, extra


class ImportFileError(Exception):
    pass


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    problems: list[str] = field(default_factory=list)  # rows (or parts) skipped
    warnings: list[str] = field(default_factory=list)  # done, but someone should look

    def summary(self, noun: str) -> str:
        text = f"{self.created} {noun} added, {self.updated} updated."
        if self.problems:
            text += f" {len(self.problems)} problem(s)."
        if self.warnings:
            text += f" {len(self.warnings)} warning(s)."
        return text


# --- People -------------------------------------------------------------------


def _find_person(email="", student_id="", employee_id="", login_id=""):
    """(person or None, problem or "") by email, then KL ID, then employee ID; flags rows that point at two people."""
    found = []
    if email:
        found.append(Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).first())
    if student_id:
        found.append(Person.objects.filter(student_id__iexact=student_id).first())
    if employee_id:
        found.append(Person.objects.filter(employee_id__iexact=employee_id).first())
    if login_id:
        found.append(Person.objects.filter(login_id__iexact=login_id).first())
    people = {p.pk: p for p in found if p}
    if len(people) > 1:
        names = " and ".join(p.display_name for p in people.values())
        return None, f"the email and IDs belong to different people ({names})"
    return (next(iter(people.values())) if people else None), ""


def _upsert_person(data) -> tuple[Person | None, bool, str]:
    """Find or create the person in an import row. Fills blanks only; never overwrites what someone typed."""
    email = normalise_email(data.get("email")) or ""
    student_id = (data.get("student_id") or "").strip()
    employee_id = (data.get("employee_id") or "").strip()
    if not (email or student_id or employee_id):
        return None, False, "needs an email, KL ID or employee ID"
    person, problem = _find_person(email, student_id, employee_id)
    if problem:
        return None, False, problem
    values = {
        "full_name": " ".join((data.get("name") or "").split()),
        "phone": data.get("phone") or "",
        "branch": data.get("branch") or "",
        "year_of_study": data.get("year") or "",
        "institution": data.get("institution") or "",
        "student_id": student_id,
        "employee_id": employee_id,
    }
    created = person is None
    if created:
        if not values["full_name"]:
            return None, False, "no name"
        person = Person(email=email or None, source=Person.Source.IMPORT, profile_completed_at=timezone.now())
        if email and is_kl_email(email):
            person.kl_email = email
        person.set_unusable_password()
    elif email and not person.email:
        person.email = email
    for name, value in values.items():
        if value and not getattr(person, name):
            setattr(person, name, value)
    try:
        person.full_clean(exclude=["password", "kl_email"], validate_unique=True)
    except ValidationError as exc:
        return None, False, "; ".join(m for msgs in exc.message_dict.values() for m in msgs)
    person.save()
    return person, created, ""


def _group(name):
    return Group.objects.get_or_create(name=name)[0]


def add_faculty(event, person, extra=None) -> tuple[EventFaculty, bool]:
    person.groups.add(_group("Faculty"))
    role, created = EventFaculty.objects.get_or_create(event=event, person=person, defaults={"extra": extra or {}})
    if not created and extra:
        role.extra = {**role.extra, **extra}
        role.save(update_fields=["extra"])
    return role, created


def add_supervisor(event, person, kind=Supervisor.Kind.FACULTY, extra=None) -> tuple[Supervisor, bool]:
    person.groups.add(_group("Supervisor"))
    if kind == Supervisor.Kind.FACULTY:
        person.groups.add(_group("Faculty"))
    if not person.is_staff:
        person.is_staff = True  # supervisors edit rooms, teams and allocations in the admin
        person.save(update_fields=["is_staff"])
    role, created = Supervisor.objects.get_or_create(event=event, person=person, defaults={"kind": kind, "extra": extra or {}})
    if not created:
        role.kind = kind
        role.is_active = True
        if extra:
            role.extra = {**role.extra, **extra}
        role.save()
    return role, created


@transaction.atomic
def import_staff(event, upload, role: str) -> ImportReport:
    """role is "faculty" or "supervisor". Creates or matches accounts and links them to this event."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    columns = match_columns(headers, STAFF_COLUMNS)
    if "name" not in columns.values():
        report.problems.append("No Name column in the first row of the first sheet.")
        return report
    for line, row in enumerate(rows, start=2):
        data, extra = _row_data(headers, row, columns)
        with transaction.atomic():
            person, _, problem = _upsert_person(data)
            if problem:
                report.problems.append(f"Row {line}: {problem}.")
                continue
            if role == "faculty":
                _, created = add_faculty(event, person, extra)
            else:
                kind_text = _key(data.get("kind"))
                kind = Supervisor.Kind.STUDENT if kind_text.startswith("stud") else Supervisor.Kind.FACULTY
                _, created = add_supervisor(event, person, kind, extra)
        report.created += created
        report.updated += not created
    return report


def issue_credentials(event, expires_at, which="new"):
    """Temporary passwords for this event's faculty and supervisors. Returns [(person, roles, password)]."""
    people = {}
    for f in EventFaculty.objects.filter(event=event, is_active=True).select_related("person"):
        people.setdefault(f.person.pk, [f.person, []])[1].append("Faculty")
    for s in Supervisor.objects.filter(event=event, is_active=True).select_related("person"):
        people.setdefault(s.person.pk, [s.person, []])[1].append("Supervisor")
    issued = []
    with transaction.atomic():
        for person, roles in sorted(people.values(), key=lambda pr: pr[0].display_name):
            if which == "new" and person.has_usable_password():
                continue
            issued.append((person, ", ".join(roles), issue_temp_password(person, expires_at)))
    return issued


# --- Students and teams -------------------------------------------------------


def _find_faculty(event, text) -> EventFaculty | None:
    text = (text or "").strip()
    if not text:
        return None
    qs = EventFaculty.objects.filter(event=event).select_related("person")
    if "@" in text:
        email = normalise_email(text)
        return qs.filter(Q(person__email__iexact=email) | Q(person__kl_email__iexact=email)).first()
    return (
        qs.filter(person__employee_id__iexact=text).first()
        or qs.filter(person__login_id__iexact=text).first()
        or qs.filter(person__full_name__iexact=text).first()
    )


def get_room(event, name) -> Room | None:
    name = " ".join((name or "").split())
    if not name:
        return None
    return Room.objects.filter(event=event, name__iexact=name).first() or Room.objects.create(event=event, name=name)


def room_change_warnings(team, new_room) -> list[str]:
    """What a move to new_room would break: faculty who would then judge teams in two rooms in one round."""
    if team.pk is None or (new_room and new_room.pk) == team.room_id:
        return []
    out = []
    for a in team.assignments.select_related("faculty__person", "round"):
        clash = (
            Assignment.objects.filter(round=a.round, faculty=a.faculty)
            .exclude(team=team)
            .exclude(team__room=new_room)
            .exists()
        )
        if clash:
            out.append(
                f"{a.faculty} judges {team.name} in {a.round}, and also teams in other rooms; after this move they "
                f"would judge in two rooms. Change the allocation too."
            )
    return out


def set_team_room(team, room) -> list[str]:
    warnings = room_change_warnings(team, room)
    team.room = room
    team.save(update_fields=["room"])
    return warnings


@transaction.atomic
def import_students(event, upload, by=None) -> ImportReport:
    """Students, their teams, rooms and mentors. A row without a team name becomes a team of one."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    questions = list(event.questions.all())
    also = {}
    for q in questions:
        also[q.key] = f"q:{q.key}"
        also[q.label] = f"q:{q.key}"
    columns = match_columns(headers, STUDENT_COLUMNS, also)
    if not ({"email", "student_id"} & set(columns.values())):
        report.problems.append("The first row needs an Email or KL ID column, so each student can be matched or created.")
        return report
    for line, row in enumerate(rows, start=2):
        data, extra = _row_data(headers, row, columns)
        answers = {name[2:]: value for name, value in data.items() if name.startswith("q:") and value}
        with transaction.atomic():
            person, _, problem = _upsert_person(data)
            if problem:
                report.problems.append(f"Row {line}: {problem}.")
                continue
            registration = Registration.objects.filter(event=event, person=person).first()
            if registration and registration.is_confirmed:
                if answers:
                    registration.answers = {**registration.answers, **answers}
                    registration.save(update_fields=["answers"])
                report.updated += 1
            else:
                try:
                    registration, _ = register(
                        event=event, person=person, answers=answers, source=Registration.Source.IMPORT, created_by=by
                    )
                except RegistrationError as exc:
                    report.problems.append(f"Row {line}: {exc}")
                    continue
                report.created += 1
            team_name = " ".join((data.get("team") or "").split())
            membership = TeamMember.objects.filter(registration=registration).select_related("team").first()
            if team_name:
                team = Team.objects.filter(event=event, name__iexact=team_name).first() or Team.objects.create(event=event, name=team_name)
            elif membership:
                team = membership.team
            else:
                solo = person.display_name
                team = Team.objects.filter(event=event, name__iexact=solo).first() or Team.objects.create(event=event, name=solo)
            if membership and membership.team_id != team.pk:
                report.warnings.append(f"Row {line}: {person.display_name} moved from {membership.team.name} to {team.name}.")
                membership.team = team
                membership.save(update_fields=["team"])
            elif not membership:
                membership = TeamMember.objects.create(team=team, registration=registration, extra=extra)
            if extra and membership.extra != {**membership.extra, **extra}:
                membership.extra = {**membership.extra, **extra}
                membership.save(update_fields=["extra"])
            changed = []
            for name in ("project_title", "project_description"):
                if data.get(name) and getattr(team, name) != data[name]:
                    setattr(team, name, data[name])
                    changed.append(name)
            if data.get("mentor"):
                mentor = _find_faculty(event, data["mentor"])
                if mentor and team.mentor_id != mentor.person_id:
                    team.mentor = mentor.person
                    team.mentor_name = ""
                    changed += ["mentor", "mentor_name"]
                    if team.assignments.filter(faculty=mentor).exists():
                        report.warnings.append(f"Row {line}: {mentor} now mentors {team.name} but is also allocated to judge it. Remove that allocation.")
                elif not mentor and team.mentor_name != data["mentor"] and not team.mentor_id:
                    team.mentor_name = data["mentor"]
                    changed.append("mentor_name")
            if changed:
                team.save(update_fields=changed)
            if data.get("room"):
                room = get_room(event, data["room"])
                if team.room_id != room.pk:
                    for w in set_team_room(team, room):
                        report.warnings.append(f"Row {line}: {w}")
    return report


# --- Allocation ---------------------------------------------------------------


class AllocationError(Exception):
    pass


@transaction.atomic
def allocate_room(round_, faculty, room) -> tuple[int, list[str]]:
    """Website allocation: this faculty member judges every team in this room in this round.

    One faculty member judges one room per round. Teams they mentor are skipped.
    Returns (new allocations, notes).
    """
    if round_.event_id != faculty.event_id or round_.event_id != room.event_id:
        raise AllocationError("The round, faculty member and room must belong to the same event.")
    other = (
        Assignment.objects.filter(round=round_, faculty=faculty).exclude(team__room=room).select_related("team__room").first()
    )
    if other:
        where = other.team.room.name if other.team.room else "a team with no room"
        raise AllocationError(
            f"{faculty} already judges in {where} in {round_}. One faculty member judges one room per round; "
            f"remove that allocation first."
        )
    made, notes = 0, []
    for team in room.teams.all():
        if team.mentor_id == faculty.person_id:
            notes.append(f"Skipped {team.name}: {faculty} mentors it.")
            continue
        assignment = Assignment(round=round_, faculty=faculty, team=team, source=Assignment.Source.WEBSITE)
        if Assignment.objects.filter(round=round_, faculty=faculty, team=team).exists():
            continue
        assignment.full_clean()
        assignment.save()
        made += 1
    if not room.teams.exists():
        notes.append(f"{room.name} has no teams yet. Allocate again after adding them.")
    return made, notes


@transaction.atomic
def unallocate_room(round_, faculty, room) -> tuple[int, int]:
    """Remove this faculty member from this room in this round. Allocations that already have marks stay."""
    qs = Assignment.objects.filter(round=round_, faculty=faculty, team__room=room)
    kept = qs.filter(scores__isnull=False).distinct().count()
    removed, _ = qs.filter(scores__isnull=True).delete()
    return removed, kept


def room_rule_warnings(event) -> list[str]:
    """Faculty who judge teams in more than one room in one round (possible through imports or room moves)."""
    rooms = defaultdict(set)
    for round_name, faculty_name, room_name in Assignment.objects.filter(round__event=event).values_list(
        "round__name", "faculty__person__full_name", "team__room__name"
    ):
        rooms[(round_name, faculty_name)].add(room_name or "no room")
    return [
        f"{faculty} judges in {len(r)} rooms in {round_name}: {', '.join(sorted(r))}."
        for (round_name, faculty), r in sorted(rooms.items())
        if len(r) > 1
    ]


def mentor_conflicts(event) -> list[str]:
    """Allocations where the judge mentors the team (possible if the mentor was set after allocating)."""
    return [
        f"{a.faculty} mentors {a.team.name} but is allocated to judge it in {a.round}."
        for a in Assignment.objects.filter(round__event=event, team__mentor=F("faculty__person")).select_related(
            "faculty__person", "team", "round"
        )
    ]


def _find_round(event, text):
    text = (text or "").strip()
    qs = Round.objects.filter(event=event)
    if not text:
        return qs.first() if qs.count() == 1 else None
    found = qs.filter(name__iexact=text).first()
    if found:
        return found
    digits = re.sub(r"\D", "", text)
    return qs.filter(position=int(digits)).first() if digits else None


@transaction.atomic
def import_allocations(event, upload) -> ImportReport:
    """Rows of (round, faculty, room or team). Breaking the one-room rule is allowed but warned; mentors never judge their team."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    columns = match_columns(headers, ALLOCATION_COLUMNS)
    names = set(columns.values())
    if "faculty" not in names or not ({"room", "team"} & names):
        report.problems.append("The first row needs a Faculty column (email, employee ID, login ID or name) and a Room or Team column.")
        return report
    for line, row in enumerate(rows, start=2):
        data, _ = _row_data(headers, row, columns)
        round_ = _find_round(event, data.get("round"))
        if not round_:
            report.problems.append(f"Row {line}: no round matches \"{data.get('round', '')}\". Add the rounds first.")
            continue
        faculty = _find_faculty(event, data.get("faculty"))
        if not faculty:
            report.problems.append(f"Row {line}: no faculty member of this event matches \"{data.get('faculty', '')}\".")
            continue
        if data.get("team"):
            teams = list(Team.objects.filter(event=event, name__iexact=data["team"]))
            if not teams:
                report.problems.append(f"Row {line}: no team called \"{data['team']}\".")
                continue
        else:
            room = Room.objects.filter(event=event, name__iexact=data.get("room", "")).first()
            if not room:
                report.problems.append(f"Row {line}: no room called \"{data.get('room', '')}\".")
                continue
            teams = list(room.teams.all())
        for team in teams:
            if team.mentor_id == faculty.person_id:
                report.problems.append(f"Row {line}: {faculty} mentors {team.name}, so they can't judge it. Not allocated.")
                continue
            if Assignment.objects.filter(round=round_, faculty=faculty, team=team).exists():
                continue
            clash = room_conflict(round_.pk, faculty.pk, team)
            if clash:
                report.warnings.append(f"Row {line}: {faculty}, {team.name}: {clash}")
            Assignment.objects.create(round=round_, faculty=faculty, team=team, source=Assignment.Source.IMPORT)
            report.created += 1
    return report


# --- Rounds and marks ---------------------------------------------------------


def open_round_now(round_):
    now = timezone.now()
    round_.opens_at = now
    if round_.closes_at and round_.closes_at <= now:
        round_.closes_at = None
    round_.save(update_fields=["opens_at", "closes_at"])


def close_round_now(round_):
    now = timezone.now()
    if not round_.opens_at or round_.opens_at >= now:
        round_.opens_at = now - timezone.timedelta(seconds=1)
    round_.closes_at = now
    round_.save(update_fields=["opens_at", "closes_at"])


class MarkingClosed(Exception):
    pass


@transaction.atomic
def save_marks(assignment, marks: dict, remarks: str, submit: bool):
    """Faculty saving their own marks. marks: {criterion: Decimal or None}."""
    assignment = Assignment.objects.select_for_update().select_related("round").get(pk=assignment.pk)
    if assignment.is_submitted:
        raise MarkingClosed("These marks are already submitted. Ask an admin if something needs changing.")
    if not assignment.round.is_open():
        raise MarkingClosed(f"Marking for {assignment.round} is closed.")
    for criterion, value in marks.items():
        if value is None:
            Score.objects.filter(assignment=assignment, criterion=criterion).delete()
        else:
            Score.objects.update_or_create(assignment=assignment, criterion=criterion, defaults={"marks": Decimal(value)})
    assignment.remarks = remarks
    if submit:
        assignment.submitted_at = timezone.now()
    assignment.save()
    return assignment
