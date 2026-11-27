"""Excel imports, room allocation and marks for hackathons.

The Excel layouts are not fixed yet, so every import matches columns by header
name (with common spellings), keeps columns it doesn't know as extra data, and
reports problems row by row instead of failing the whole file.
"""

import datetime
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
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
    "phone": ["phone", "mobile", "contact", "phone number", "mobile number", "contact number", "contact no", "mobile no", "phone no", "whatsapp"],
    "student_id": ["kl id", "student id", "id number", "university id", "roll no", "roll number", "registration number", "reg no", "regd no"],
    "employee_id": ["employee id", "emp id", "empid", "emp no", "employee no", "employee number", "faculty id", "staff id", "id no"],
    "designation": ["designation", "post", "position", "title of the post"],
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
    **{k: PERSON_COLUMNS[k] for k in ("name", "email", "phone", "employee_id", "branch", "student_id", "designation")},
    "kind": ["type", "role", "supervisor type", "faculty or student"],
}
ALLOCATION_COLUMNS = {
    "round": ["round", "round name", "round no", "round number"],
    "faculty": ["faculty", "faculty email", "faculty employee id", "employee id", "emp id", "faculty id", "login id", "faculty login id", "faculty name", "judge", "jury", "evaluator", "email"],
    "room": ["room", "room no", "room number", "lab", "venue"],
    "team": ["team", "team name", "group"],
    "member": ["member", "member email", "student email", "student", "kl id", "student kl id", "roll no"],
}
ROOM_COLUMNS = {
    "name": ["room", "room code", "room no", "room number", "room name", "lab"],
    "capacity": ["max teams", "capacity", "teams", "max", "maximum teams", "no of teams"],
    "block": ["block", "building"],
    "floor": ["floor", "floor no"],
    "notes": ["notes", "note", "remarks", "description"],
}


def _key(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        value = value.date() if value.time() == datetime.time() else value
    if isinstance(value, datetime.date):
        return value.isoformat()
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
    added: list[int] = field(default_factory=list)  # registrations new to the event (for the "you've been added" email)

    def summary(self, noun: str) -> str:
        text = f"{self.created} {noun} added, {self.updated} updated."
        if self.problems:
            text += f" {len(self.problems)} problem(s)."
        if self.warnings:
            text += f" {len(self.warnings)} warning(s)."
        return text


# --- People -------------------------------------------------------------------


def kl_email_for(student_id) -> str:
    """A KL ID's university email (the sign-in address), e.g. 2300012345 -> 2300012345@kluniversity.in."""
    return normalise_email(settings.KL_STUDENT_EMAIL_FORMAT.format(id=(student_id or "").strip())) or ""


def _find_person(email="", student_id="", employee_id="", login_id=""):
    """(person or None, problem or "") by email, KL ID (and its KL email), then employee ID; flags rows that point at two people."""
    found = []
    if email:
        found.append(Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email) | Q(contact_email__iexact=email)).first())
    if student_id:
        found.append(Person.objects.filter(student_id__iexact=student_id).first())
        kl = kl_email_for(student_id)
        if kl and kl != email:
            found.append(Person.objects.filter(Q(email__iexact=kl) | Q(kl_email__iexact=kl)).first())
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
        "designation": " ".join((data.get("designation") or "").split()),
    }
    if values["phone"]:
        values["phone"] = re.sub(r"\D", "", values["phone"]) or values["phone"]  # digits only
    created = person is None
    if created:
        if not values["full_name"]:
            return None, False, "no name"
        if student_id and not employee_id:
            # Students sign in with their KL email, made from the ID; another address is kept as a contact email.
            kl = kl_email_for(student_id)
            person = Person(email=kl, kl_email=kl, kind=Person.Kind.STUDENT, source=Person.Source.IMPORT,
                            profile_completed_at=timezone.now(), contact_email=email if email and email != kl else "")
        else:
            person = Person(email=email or None, source=Person.Source.IMPORT, profile_completed_at=timezone.now())
            if email and is_kl_email(email):
                person.kl_email = email
        person.set_unusable_password()
    elif email and not person.email:
        person.email = email
    elif email and email not in (person.email, person.kl_email) and not person.contact_email:
        person.contact_email = email
    for name, value in values.items():
        if value and not getattr(person, name):
            setattr(person, name, value)
    try:
        person.full_clean(exclude=["password", "kl_email"], validate_unique=True)
    except ValidationError as exc:
        return None, False, "; ".join(m for msgs in exc.message_dict.values() for m in msgs)
    person.save()
    return person, created, ""


def save_custom_values(event, person, data, applies_to):
    """Custom detail fields (data keys "f:<key>") into PersonDetails, checked against their definitions."""
    from .fields import clean_value
    from .models import FieldDefinition, PersonDetails

    given = {k[2:]: v for k, v in data.items() if k.startswith("f:")}
    if not given or not applies_to:
        return
    defs = {d.key: d for d in FieldDefinition.objects.filter(event=event, applies_to=applies_to, builtin="")}
    details, _ = PersonDetails.objects.get_or_create(event=event, person=person)
    for key, raw in given.items():
        if key in defs and raw not in ("", None):
            value, error = clean_value(defs[key], raw)
            if not error:
                details.values[key] = value
    details.save()


def _group(name):
    return Group.objects.get_or_create(name=name)[0]


def make_faculty_type(person):
    if person.kind != "faculty":
        person.kind = "faculty"
        person.save(update_fields=["kind"])
    person.groups.add(_group("Faculty"))


def add_faculty(event, person, extra=None) -> tuple[EventFaculty, bool]:
    make_faculty_type(person)
    role, created = EventFaculty.objects.get_or_create(event=event, person=person, defaults={"extra": extra or {}})
    if not created and extra:
        role.extra = {**role.extra, **extra}
        role.save(update_fields=["extra"])
    return role, created


def _switch_columns():
    """Supervisor Excel columns that set permission switches, e.g. "Change marks" or "can_change_marks"."""
    from .access import PERMISSIONS

    out = {}
    for name, (label, _, _) in PERMISSIONS.items():
        out[name] = [_key(label), _key(name), _key("can " + label)]
    return out


def _volunteer_switch_columns():
    from .access import VOLUNTEER_PERMISSIONS

    return {name: [_key(label), _key(name), _key("can " + label)] for name, (label, _, _) in VOLUNTEER_PERMISSIONS.items()}


TRUE_WORDS, FALSE_WORDS = {"yes", "y", "true", "1", "on", "allow", "allowed"}, {"no", "n", "false", "0", "off", "deny"}


def add_volunteer(event, person, extra=None, switches=None):
    from .models import Volunteer

    person.groups.add(_group("Volunteer"))
    row, created = Volunteer.objects.get_or_create(event=event, person=person, defaults={"extra": extra or {}})
    if not created:
        row.is_active = True
    for name, value in (switches or {}).items():
        setattr(row, name, value)
    row.save()
    return row, created


def add_supervisor(event, person, kind=Supervisor.Kind.FACULTY, extra=None, switches=None) -> tuple[Supervisor, bool]:
    person.groups.add(_group("Supervisor"))
    if kind == Supervisor.Kind.FACULTY:
        make_faculty_type(person)
    role, created = Supervisor.objects.get_or_create(event=event, person=person, defaults={"kind": kind, "extra": extra or {}})
    if not created:
        role.kind = kind
        role.is_active = True
        if extra:
            role.extra = {**role.extra, **extra}
    for name, value in (switches or {}).items():
        setattr(role, name, value)
    if not created or switches:
        role.save()
    return role, created


@transaction.atomic
def import_staff(event, upload, role: str, columns=None, only_rows=None) -> ImportReport:
    """role is "faculty" or "supervisor". Creates or matches accounts and links them to this event.

    columns ({column index: field}) comes from the import's matching screen; without it, headers are matched by name.
    only_rows limits the import to those sheet rows (the ones that passed the row check).
    """
    report = ImportReport()
    headers, rows = read_rows(upload)
    spec = {**STAFF_COLUMNS, **(_switch_columns() if role == "supervisor" else _volunteer_switch_columns() if role == "volunteer" else {})}
    columns = columns if columns is not None else match_columns(headers, spec)
    if "name" not in columns.values():
        report.problems.append("No Name column in the first row of the first sheet.")
        return report
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, extra = _row_data(headers, row, columns)
        with transaction.atomic():
            person, _, problem = _upsert_person(data)
            if problem:
                report.problems.append(f"Row {line}: {problem}.")
                continue
            save_custom_values(event, person, data, "faculty" if role == "faculty" else None)
            if role == "faculty":
                _, created = add_faculty(event, person, extra)
            elif role == "volunteer":
                switches = {}
                for name in _volunteer_switch_columns():
                    word = _key(data.get(name))
                    if word in TRUE_WORDS:
                        switches[name] = True
                    elif word in FALSE_WORDS:
                        switches[name] = False
                _, created = add_volunteer(event, person, extra, switches)
            else:
                kind_text = _key(data.get("kind"))
                kind = Supervisor.Kind.STUDENT if kind_text.startswith("stud") else Supervisor.Kind.FACULTY
                switches = {}
                for name in _switch_columns():
                    word = _key(data.get(name))
                    if word in TRUE_WORDS:
                        switches[name] = True
                    elif word in FALSE_WORDS:
                        switches[name] = False
                    elif word:
                        report.warnings.append(f"Row {line}: \"{data[name]}\" isn't yes or no, so {name.replace('_', ' ')} was left as it was.")
                _, created = add_supervisor(event, person, kind, extra, switches)
        report.created += created
        report.updated += not created
    return report


def _privileged(person) -> bool:
    """Accounts a non-admin must never reset: a new password would be a way into a bigger account."""
    return bool(person.is_superuser or person.is_staff or person.groups.filter(name__in=["Admin", "Coordinator", "Supervisor", "Volunteer"]).exists())


def issue_credentials(event, expires_at, which="new", faculty_only=False):
    """Temporary passwords for this event's faculty (and supervisors, for admins). Returns [(person, roles, password)].

    faculty_only (for supervisors given the switch) skips supervisors and anyone with a bigger role.
    """
    people = {}
    for f in EventFaculty.objects.filter(event=event, is_active=True).select_related("person"):
        if faculty_only and _privileged(f.person):
            continue
        people.setdefault(f.person.pk, [f.person, []])[1].append("Faculty")
    if not faculty_only:
        from .models import Volunteer

        for s in Supervisor.objects.filter(event=event, is_active=True).select_related("person"):
            people.setdefault(s.person.pk, [s.person, []])[1].append("Supervisor")
        for v in Volunteer.objects.filter(event=event, is_active=True).select_related("person"):
            people.setdefault(v.person.pk, [v.person, []])[1].append("Volunteer")
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
    from .models import clean_room_code, parse_room_code

    name = clean_room_code(name) if parse_room_code(name) else " ".join((name or "").split())
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
def import_students(event, upload, by=None, columns=None, only_rows=None) -> ImportReport:
    """Students, their teams, rooms and mentors. A row without a team name becomes a team of one.

    columns and only_rows come from the import's matching screen (see import_staff).
    """
    report = ImportReport()
    headers, rows = read_rows(upload)
    questions = list(event.questions.all())
    also = {}
    for q in questions:
        also[q.key] = f"q:{q.key}"
        also[q.label] = f"q:{q.key}"
    columns = columns if columns is not None else match_columns(headers, STUDENT_COLUMNS, also)
    if not ({"email", "student_id"} & set(columns.values())):
        report.problems.append("The first row needs an Email or KL ID column, so each student can be matched or created.")
        return report
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, extra = _row_data(headers, row, columns)
        answers = {name[2:]: value for name, value in data.items() if name.startswith("q:") and value}
        with transaction.atomic():
            person, _, problem = _upsert_person(data)
            if problem:
                report.problems.append(f"Row {line}: {problem}.")
                continue
            save_custom_values(event, person, data, "student")
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
                report.added.append(registration.pk)
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
    from .rubric import eligible_team_ids

    made, notes = 0, []
    ids = eligible_team_ids(round_)
    for team in room.teams.all():
        if ids is not None and team.pk not in ids:
            notes.append(f"Skipped {team.name}: it did not advance to {round_.name}.")
            continue
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


def find_member_registration(event, text):
    text = (text or "").strip()
    qs = Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
    if "@" in text:
        email = normalise_email(text)
        return qs.filter(Q(person__email__iexact=email) | Q(person__kl_email__iexact=email)).first()
    return qs.filter(person__student_id__iexact=text).first() if text else None


@transaction.atomic
def import_rooms(event, upload, columns=None, only_rows=None) -> ImportReport:
    """Rooms to use, with their max teams. Codes like R206 give the block and floor unless the file says otherwise."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    columns = columns if columns is not None else match_columns(headers, ROOM_COLUMNS)
    if "name" not in columns.values():
        report.problems.append("The first row needs a Room column.")
        return report
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, _ = _row_data(headers, row, columns)
        from .models import clean_room_code, parse_room_code

        name = clean_room_code(data.get("name"))
        if not name:
            report.problems.append(f"Row {line}: no room number.")
            continue
        if not parse_room_code(name):
            report.problems.append(f"Row {line}: \"{data.get('name')}\" isn't a room number like L502.")
            continue
        capacity = data.get("capacity") or ""
        if capacity and not capacity.isdigit():
            report.problems.append(f"Row {line}: max teams \"{capacity}\" is not a whole number.")
            continue
        room = Room.objects.filter(event=event, name__iexact=name).first()
        created = room is None
        room = room or Room(event=event, name=name)
        if capacity:
            room.capacity = int(capacity)
        room.save()  # block and floor come from the number
        report.created += created
        report.updated += not created
    return report


@transaction.atomic
def import_allocations(event, upload, columns=None, only_rows=None) -> ImportReport:
    """Rows of (round, faculty, room or team). Breaking the one-room rule is allowed but warned; mentors never judge their team."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    columns = columns if columns is not None else match_columns(headers, ALLOCATION_COLUMNS)
    names = set(columns.values())
    if "faculty" not in names or not ({"room", "team", "member"} & names):
        report.problems.append("The first row needs a Faculty column (email, employee ID, login ID or name) and a Team, team member or Room column.")
        return report
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, _ = _row_data(headers, row, columns)
        round_ = _find_round(event, data.get("round"))
        if not round_:
            report.problems.append(f"Row {line}: no round matches \"{data.get('round', '')}\". Add the rounds first.")
            continue
        faculty = _find_faculty(event, data.get("faculty"))
        if not faculty:
            report.problems.append(f"Row {line}: no faculty member of this event matches \"{data.get('faculty', '')}\".")
            continue
        if not data.get("team") and data.get("member"):
            registration = find_member_registration(event, data["member"])
            membership = TeamMember.objects.filter(registration=registration).select_related("team").first() if registration else None
            if not membership:
                report.problems.append(f"Row {line}: no student in a team matches \"{data['member']}\".")
                continue
            data["team"] = membership.team.name
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
        from .rubric import eligible_team_ids

        ids = eligible_team_ids(round_)
        for team in teams:
            if ids is not None and team.pk not in ids:
                report.problems.append(f"Row {line}: {team.name} did not advance to {round_.name}. Not allocated.")
                continue
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
def save_marks(assignment, marks: dict, remarks: str, submit: bool, extras: dict | None = None):
    """Faculty saving their own marks. marks: {criterion: Decimal or None}. extras: comments, red flags, self-score,
    recommendation and "advance anyway" from the marksheet."""
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
    for key, value in (extras or {}).items():
        setattr(assignment, key, value if value is not None or key == "self_score" else getattr(assignment, key))
    if submit:
        assignment.submitted_at = timezone.now()
    assignment.save()
    return assignment


# --- Faculty self-registration --------------------------------------------------


class RequestError(Exception):
    pass


def request_faculty(person, data):
    """A signed-in person asks to be faculty. Nothing is granted until an admin approves."""
    from .models import FacultyRequest

    if person.is_faculty:
        raise RequestError("You're already faculty on the portal.")
    if FacultyRequest.objects.filter(person=person, status=FacultyRequest.Status.PENDING).exists():
        raise RequestError("Your request is already waiting for an admin.")
    return FacultyRequest.objects.create(person=person, **data)


def _decision_email(req):
    from mailer.services import enqueue_email

    if not req.person.email:
        return
    if req.status == req.Status.APPROVED:
        subject = "You're registered as faculty"
        text = (
            f"Hello {req.full_name},\n\nAn admin approved your faculty registration on the {settings.CLUB_NAME} portal."
            + (f" You're on the faculty list for {req.event.title}." if req.event_id else "")
            + "\n\nSign in with your email as usual; the Judging page lists the teams allocated to you. "
            "To sign in with a password instead, set one from your profile.\n"
        )
    else:
        subject = "Your faculty registration"
        text = f"Hello {req.full_name},\n\nAn admin did not approve your faculty registration this time. Your account stays as it was.\n"
    if req.note:
        text += f"\nNote from the admin: {req.note}\n"
    enqueue_email(to=req.person.email, subject=subject, text=text, person=req.person)


@transaction.atomic
def approve_faculty_request(req, by, note="", allow_duplicates=False):
    """Give the Faculty role (and the chosen hackathon). Refuses if the person looks like an existing account."""
    from .models import FacultyRequest

    req = FacultyRequest.objects.select_for_update(of=("self",)).select_related("person", "event").get(pk=req.pk)
    if req.status != FacultyRequest.Status.PENDING:
        raise RequestError("This request was already decided.")
    dupes = list(req.duplicates())
    if dupes and not allow_duplicates:
        names = ", ".join(f"{p.display_name} ({p.email or p.employee_id})" for p in dupes)
        raise RequestError(f"Possible duplicate: {names} already has this email or employee ID. Reject this request or fix the accounts first.")
    person = req.person
    person.full_name = person.full_name or req.full_name
    person.phone = person.phone or req.phone
    person.branch = person.branch or req.department
    if not person.employee_id and not dupes:
        person.employee_id = req.employee_id
    person.save()
    if req.event_id:
        if req.designation and not person.designation:
            person.designation = req.designation
            person.save(update_fields=["designation"])
        add_faculty(req.event, person)
    else:
        make_faculty_type(person)
    req.status, req.note, req.decided_by, req.decided_at = FacultyRequest.Status.APPROVED, note, by, timezone.now()
    req.save()
    _decision_email(req)
    return req


@transaction.atomic
def reject_faculty_request(req, by, note=""):
    from .models import FacultyRequest

    req = FacultyRequest.objects.select_for_update(of=("self",)).select_related("person").get(pk=req.pk)
    if req.status != FacultyRequest.Status.PENDING:
        raise RequestError("This request was already decided.")
    req.status, req.note, req.decided_by, req.decided_at = FacultyRequest.Status.REJECTED, note, by, timezone.now()
    req.save()
    _decision_email(req)
    return req


# --- Changing marks after the fact (admins, or supervisors with the switch) ----------


def _marks_by_label(assignment):
    return {s.criterion.label: float(s.marks) for s in assignment.scores.select_related("criterion")}


@transaction.atomic
def change_marks(assignment, by, marks: dict, reason: str, remarks=None):
    """Overwrite marks for one allocation, recording who, when, why, and before/after. marks: {criterion: value or None}."""
    from .models import MarkChange

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for changing these marks.")
    assignment = Assignment.objects.select_for_update().get(pk=assignment.pk)
    before = _marks_by_label(assignment)
    for criterion, value in marks.items():
        if criterion.round_id != assignment.round_id:
            raise ValidationError("That criterion belongs to another round.")
        if value is None:
            Score.objects.filter(assignment=assignment, criterion=criterion).delete()
        else:
            if value < 0 or value > criterion.max_marks:
                raise ValidationError(f"{criterion.label}: between 0 and {criterion.max_marks.normalize():f}.")
            Score.objects.update_or_create(assignment=assignment, criterion=criterion, defaults={"marks": Decimal(value)})
    if remarks is not None:
        assignment.remarks = remarks
        assignment.save(update_fields=["remarks", "updated_at"])
    return MarkChange.objects.create(
        assignment=assignment, changed_by=by, reason=reason, before=before, after=_marks_by_label(assignment)
    )


@transaction.atomic
def reopen_marks(assignment, by, reason: str):
    """Let the faculty member edit a submitted marksheet again (while the round is open)."""
    from .models import MarkChange

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for reopening.")
    assignment = Assignment.objects.select_for_update().get(pk=assignment.pk)
    if not assignment.submitted_at:
        raise ValidationError("These marks aren't submitted, so there's nothing to reopen.")
    assignment.submitted_at = None
    assignment.save(update_fields=["submitted_at", "updated_at"])
    marks = _marks_by_label(assignment)
    return MarkChange.objects.create(
        assignment=assignment, kind=MarkChange.Kind.REOPENED, changed_by=by, reason=reason, before=marks, after=marks
    )


# --- Teams, one row per team ------------------------------------------------------------------


MEMBER_PARTS = {
    "name": ["name", "full name", "student name"],
    "email": ["email", "email id", "mail", "mail id", "e mail"],
    "student_id": ["kl id", "id", "student id", "roll no", "roll number", "reg no", "registration number"],
    "phone": ["phone", "mobile", "contact", "phone number", "mobile number", "whatsapp"],
}


def member_aliases(n: int, part: str) -> list[str]:
    """Header spellings for member n's part, e.g. "member 2 email", "m2 email", "email 2", "leader email" for n = 1."""
    out = []
    for word in MEMBER_PARTS[part]:
        out += [f"member {n} {word}", f"member{n} {word}", f"m{n} {word}", f"{word} {n}", f"participant {n} {word}",
                f"student {n} {word}", f"team member {n} {word}"]
        if n == 1:
            out += [f"leader {word}", f"team leader {word}", f"lead {word}"]
    return [_key(a) for a in out]


TEAM_COLUMNS = {
    "team": ["team", "team name", "group", "group name"],
    "project_title": ["problem statement", "project", "project title", "title", "idea", "problem statement title", "ps"],
    "project_description": ["project description", "description", "abstract", "idea description", "problem statement description"],
    "room": ["room", "room no", "room number", "lab"],
    "mentor": ["mentor", "mentor email", "guide", "faculty mentor"],
}


def member_slots(data, max_size):
    """[(n, {"name", "email", "student_id", "phone"})] for the member slots that have anything in them."""
    slots = []
    for n in range(1, max_size + 1):
        values = {part: (data.get(f"m{n}_{part}") or "").strip() for part in MEMBER_PARTS}
        if any(values.values()):
            slots.append((n, values))
    return slots


@transaction.atomic
def import_teams(event, upload, by=None, columns=None, only_rows=None) -> ImportReport:
    """One row per team: name, project, mentor and members 1..max (member 1 is the leader). Members are found by
    email, then KL ID, or created as students, registered and put in the team."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    max_size = event.team_size_max or 6
    if columns is None:
        spec = dict(TEAM_COLUMNS)
        for n in range(1, max_size + 1):
            for part in MEMBER_PARTS:
                spec[f"m{n}_{part}"] = member_aliases(n, part)
        columns = match_columns(headers, spec)
    if "team" not in columns.values():
        report.problems.append("The first row needs a Team name column.")
        return report
    seen_teams, seen_people = {}, {}
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, extra = _row_data(headers, row, columns)
        name = " ".join((data.get("team") or "").split())
        if not name:
            report.problems.append(f"Row {line}: no team name.")
            continue
        slots = member_slots(data, max_size)
        problem = team_row_problem(event, line, name, slots, seen_teams, seen_people)
        if problem:
            report.problems.append(problem)
            continue
        with transaction.atomic():
            team = Team.objects.create(event=event, name=name, project_title=data.get("project_title") or "",
                                       project_description=data.get("project_description") or "", extra=extra)
            if data.get("room"):
                team.room = get_room(event, data["room"])
            if data.get("mentor"):
                mentor = _find_faculty(event, data["mentor"])
                if mentor:
                    team.mentor = mentor.person
                else:
                    team.mentor_name = data["mentor"]
            team.save()
            failed = None
            for n, member in slots:
                person, _, why = _upsert_person({"name": member["name"], "email": member["email"],
                                                 "student_id": member["student_id"], "phone": member["phone"]})
                if why:
                    failed = f"Row {line}, member {n}: {why}."
                    break
                registration = Registration.objects.filter(event=event, person=person).first()
                if not (registration and registration.is_confirmed):
                    try:
                        registration, _ = register(event=event, person=person, source=Registration.Source.IMPORT, created_by=by)
                    except RegistrationError as exc:
                        failed = f"Row {line}, member {n}: {exc}"
                        break
                    report.added.append(registration.pk)
                TeamMember.objects.create(team=team, registration=registration, is_leader=(n == slots[0][0]))
            if failed:
                transaction.set_rollback(True)
                report.problems.append(failed)
                report.added = [pk for pk in report.added if Registration.objects.filter(pk=pk).exists()]
                continue
        report.created += 1
    return report


def team_row_problem(event, line, name, slots, seen_teams, seen_people) -> str:
    """Why this team row can't be imported, or "". Tracks teams and people seen earlier in the file."""
    low, high = event.team_size_min or 1, event.team_size_max or 6
    if name.lower() in seen_teams:
        return f"Row {line}: the team name \"{name}\" is also in row {seen_teams[name.lower()]}."
    seen_teams[name.lower()] = line
    if Team.objects.filter(event=event, name__iexact=name).exists():
        return f"Row {line}: there is already a team called \"{name}\" in this hackathon."
    if not low <= len(slots) <= high:
        return f"Row {line}: {name} has {len(slots)} member(s); teams here have {low} to {high}."
    for n, m in slots:
        if not (m["email"] or m["student_id"]):
            return f"Row {line}, member {n}: needs an email or a KL ID."
        for key in ("email", "student_id"):
            value = normalise_email(m[key]) if key == "email" else m[key].lower()
            if value and value in seen_people:
                return f"Row {line}, member {n}: {m[key]} is also in {seen_people[value]}."
            if value:
                seen_people[value] = f"row {line}"
        person, clash = _find_person(normalise_email(m["email"]) or "", m["student_id"])
        if clash:
            return f"Row {line}, member {n}: {clash}."
        if person:
            taken = TeamMember.objects.filter(team__event=event, registration__person=person).select_related("team").first()
            if taken:
                return f"Row {line}, member {n}: {person.display_name} is already in team {taken.team.name}."
    return ""


@transaction.atomic
def register_team(event, leader, team_name, members):
    """The leader registers, the teammates are found (or added as students) and registered, and the team is made."""
    lead_reg, created = register(event=event, person=leader)
    regs = [(lead_reg, created, True)]
    for m in members:
        person, _, why = _upsert_person({"name": m["name"], "email": m["email"], "student_id": m["student_id"]})
        if why:
            raise RegistrationError(f"{m['email'] or m['student_id']}: {why}")
        registration = Registration.objects.filter(event=event, person=person).first()
        new = not (registration and registration.is_confirmed)
        if new:
            registration, _ = register(event=event, person=person, created_by=leader)
        regs.append((registration, new, False))
    team = Team.objects.create(event=event, name=team_name)
    for registration, _, is_leader in regs:
        TeamMember.objects.create(team=team, registration=registration, is_leader=is_leader)
    from registrations.notify import notify_added

    transaction.on_commit(lambda: [notify_added(r) for r, new, lead in regs if new and not lead])
    return team, lead_reg
