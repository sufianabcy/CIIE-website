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
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from openpyxl import load_workbook

from people.models import Person, is_kl_email, normalise_email
from people.staff import issue_temp_password
from registrations.models import Registration
from registrations.services import RegistrationError, register

from .models import (
    Assignment, EventFaculty, Room, Round, Score, Supervisor, Team, TeamMember, clean_team_name, clean_team_number, member_conflict,
    room_conflict,
)

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
    **{k: PERSON_COLUMNS[k] for k in ("name", "email", "phone", "branch", "year", "institution")},
    "student_id": PERSON_COLUMNS["student_id"] + ["id", "kl id number", "student kl id"],
    "team": ["team", "team name", "group", "group name", "name of the team", "name of team"],
    "team_number": ["team no", "team number", "team #", "team id", "group no", "team num", "team sno", "group number"],
    "project_title": ["project", "project title", "project name", "title", "idea", "title of the project", "title of project"],
    "project_description": ["project description", "description", "problem statement", "problem", "abstract", "idea description"],
    "category": ["category", "theme", "domain", "project category"],
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


def _find_by_student_id(student_id):
    """The student with this KL ID (ignoring case), or the account with its KL email."""
    if not student_id:
        return None
    person = Person.objects.filter(student_id__iexact=student_id).first()
    if person is None:
        kl = kl_email_for(student_id)
        person = Person.objects.filter(Q(email__iexact=kl) | Q(kl_email__iexact=kl)).first() if kl else None
    return person


def _find_person(email="", student_id="", employee_id="", login_id="", id_first=False):
    """(person or None, problem or "") by email, KL ID (and its KL email), then employee ID; flags rows that point at two people.

    id_first (student imports): the KL ID is the key. A student found by it is that student, whatever the email says.
    """
    if id_first and student_id:
        person = _find_by_student_id(student_id)
        if person is not None:
            return person, ""
        if email:
            other = Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email) | Q(contact_email__iexact=email)).first()
            if other and other.student_id and other.student_id.lower() != student_id.lower():
                return None, f"the email {email} belongs to {other.display_name}, whose KL ID is {other.student_id}"
            return other, ""
        return None, ""
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


def _upsert_person(data, id_first=False) -> tuple[Person | None, bool, str]:
    """Find or create the person in an import row. Fills blanks only; never overwrites what someone typed.

    id_first: the KL ID is the key (student imports); an email that already belongs to someone else is not used.
    """
    email = normalise_email(data.get("email")) or ""
    student_id = clean_student_id(data.get("student_id"))
    employee_id = (data.get("employee_id") or "").strip()
    if not (email or student_id or employee_id):
        return None, False, "needs an email, KL ID or employee ID"
    person, problem = _find_person(email, student_id, employee_id, id_first=id_first)
    if problem:
        return None, False, problem
    if id_first and email:
        owner = Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).first()
        if owner is not None and (person is None or owner.pk != person.pk):
            email = ""  # someone else signs in with it: keep the student matched by KL ID, without that address
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


def clean_student_id(raw) -> str:
    """A KL ID as the key: trimmed, and "2300031234.0" (an Excel number cell read as text) becomes "2300031234"."""
    value = str(raw if raw is not None else "").strip()
    if re.fullmatch(r"\d+\.0+", value):
        value = value.split(".", 1)[0]
    return value


def find_team(event, name) -> Team | None:
    """The team with this name at this hackathon, ignoring case and extra spaces (as the database compares them)."""
    name = clean_team_name(name)
    return Team.objects.filter(event=event, name__iexact=name).first() if name else None


def named_after(team, person) -> bool:
    """True when the team's name is just this person's name (or email or KL ID): a made-up team of one."""
    key = clean_team_name(team.name).lower()
    return bool(key) and key in {clean_team_name(v).lower() for v in (person.full_name, person.email, person.student_id) if v}


def _team_is_used(team) -> bool:
    """Allocations, marks, attendance or a round decision: such a team is never deleted or merged automatically."""
    return team.assignments.exists() or team.attendance.exists() or team.outcomes.exists()


def _drop_empty_team_named_after(team, person) -> bool:
    """Delete a team that was made for this person alone (named after them) once it is empty and unused."""
    if not named_after(team, person) or team.members.exists() or _team_is_used(team):
        return False
    team.delete()
    return True


# How the students import forms teams (chosen on the column step).
TEAM_MODES = {
    "name": "Team name",
    "number": "Team number",
    "number_named": "Team number, named from the team-name column where one is given",
}
TEAM_VALUE_FIELDS = (("project_title", "project title"), ("project_description", "project description"), ("category", "category"))


def team_mode_for(used, chosen="") -> str:
    """The team mode to use: the one chosen, else (c) when both a number and a name column are used, else by what's used."""
    used = set(used)
    if chosen in TEAM_MODES:
        return chosen
    if "team_number" in used:
        return "number_named" if "team" in used else "number"
    return "name"


def team_column_for(mode) -> str:
    """The column that decides who is in which team for this mode."""
    return "team" if mode == "name" else "team_number"


@dataclass
class TeamGroup:
    key: str
    number: str = ""
    name: str = ""  # from the sheet; "" when no row of the team has one (numbered teams are then called "Team <number>")
    lines: list = field(default_factory=list)
    values: dict = field(default_factory=dict)  # project title, description and category: the first non-blank value

    @property
    def label(self) -> str:
        return f"team number {self.number}" if self.number else f"team \"{self.name}\""


@dataclass
class TeamPlan:
    mode: str
    groups: dict = field(default_factory=dict)  # key -> TeamGroup, in sheet order
    row_key: dict = field(default_factory=dict)  # sheet line -> group key (lines without a team are not here)
    no_team: list = field(default_factory=list)  # sheet lines with no team number (or, by name, no team name)
    errors: dict = field(default_factory=dict)  # sheet line -> [problems]
    warnings: list = field(default_factory=list)


def plan_teams(rows, mode) -> TeamPlan:
    """Group sheet rows into teams before anything is saved. rows: [(sheet line, row data)].

    By name (a), rows with the same team name (ignoring case and spaces) form a team. By number (b, c), rows with the
    same team number form a team; with (c) the team takes the name any of its rows gives. Two different names for one
    number is an error on every row of that number. A name that is just a member's name or KL ID is never used.
    """
    plan = TeamPlan(mode=mode)
    names = defaultdict(dict)  # key -> {name key: (name, first line)}
    people = defaultdict(set)  # key -> members' names and KL IDs, lower case
    seen_values = defaultdict(lambda: defaultdict(dict))  # key -> field -> {value: first line}
    for line, data in rows:
        name = clean_team_name(data.get("team")) if mode != "number" else ""
        number = clean_team_number(data.get("team_number")) if mode != "name" else ""
        key = (name.lower() if mode == "name" else number)
        if not key:
            plan.no_team.append(line)
            continue
        group = plan.groups.setdefault(key, TeamGroup(key=key, number=number, name=name if mode == "name" else ""))
        group.lines.append(line)
        plan.row_key[line] = key
        people[key] |= {v.lower() for v in (" ".join(str(data.get("name") or "").split()), clean_student_id(data.get("student_id"))) if v}
        if mode == "number_named" and name:
            names[key].setdefault(name.lower(), (name, line))
        for fname, _ in TEAM_VALUE_FIELDS:
            value = " ".join(str(data.get(fname) or "").split()) if fname != "project_description" else str(data.get(fname) or "").strip()
            if value:
                seen_values[key][fname].setdefault(value, line)
                group.values.setdefault(fname, value)
    for key, group in plan.groups.items():
        found = list(names[key].values())
        if mode == "name" and group.name.lower() in people[key]:
            plan.warnings.append(f"Row {group.lines[0]}: the team name \"{group.name}\" is a student's own name or KL ID. "
                                 "Check the team column.")
        usable = [(n, line) for n, line in found if n.lower() not in people[key]]
        for n, line in found:
            if n.lower() in people[key]:
                plan.warnings.append(f"Row {line}: \"{n}\" is a student's own name or KL ID, so it isn't used as the name of "
                                     f"team number {group.number}.")
        if len(usable) > 1:
            listed = " and ".join(f"\"{n}\" (row {line})" for n, line in usable)
            for line in group.lines:
                plan.errors.setdefault(line, []).append(
                    f"Row {line}: team number {group.number} has different team names: {listed}. Use one name for the whole team.")
        elif usable:
            group.name = usable[0][0]
        for fname, label in TEAM_VALUE_FIELDS:
            options = seen_values[key][fname]
            if len(options) > 1:
                first = next(iter(options.values()))
                rows_text = ", ".join(str(line) for line in sorted(options.values()))
                plan.warnings.append(f"The rows of {group.label} (rows {rows_text}) give different {label}s; "
                                     f"the one in row {first} is used.")
    return plan


def _team_for_group(event, group, mode, cache, report_warnings) -> tuple[Team | None, str]:
    """(team, problem): the team a planned group goes into, made if needed. Same answer for every row of the group."""
    if group.key in cache:
        return cache[group.key]
    if mode == "name":
        result = (find_team(event, group.name) or Team.objects.create(event=event, name=group.name), "")
        cache[group.key] = result
        return result
    team = Team.objects.filter(event=event, number=group.number).first()
    problem = ""
    if team is not None:
        if group.name and clean_team_name(group.name).lower() != team.name.lower():
            clash = find_team(event, group.name)
            if clash and clash.pk != team.pk:
                problem = (f"team number {group.number} is called \"{group.name}\" in the sheet, but another team already has "
                           "that name. Change one of them.")
            else:
                report_warnings.append(f"Team number {group.number} was renamed from \"{team.name}\" to \"{group.name}\".")
                team.name = group.name
                team.save(update_fields=["name"])
    else:
        name = group.name or f"Team {group.number}"
        existing = find_team(event, name)
        if existing is not None and group.name and not existing.number:
            existing.number = group.number  # the same team, made earlier by name: it gets its number
            existing.save(update_fields=["number"])
            team = existing
        elif existing is not None:
            other = f" (team number {existing.number})" if existing.number else ""
            problem = (f"team number {group.number} would be called \"{name}\", but another team{other} already has that name. "
                       "Rename that team, or give this one a name in the sheet.")
        else:
            try:
                with transaction.atomic():
                    team = Team.objects.create(event=event, name=name, number=group.number)
            except IntegrityError:
                problem = f"team number {group.number} or the name \"{name}\" is already used by another team."
    result = (None, problem) if problem else (team, "")
    cache[group.key] = result
    return result


@transaction.atomic
def import_students(event, upload, by=None, columns=None, only_rows=None, team_mode="") -> ImportReport:
    """Students, their teams, rooms and mentors. The KL ID is the key: an ID already here updates that student.

    Only Name and KL ID are required. Teams are formed by team name, or by team number (see plan_teams and
    TEAM_MODES). When the sheet has the column that forms teams, a blank value means "not in a team": a student
    in a team is taken out of it, and no team is ever made up for them. Without that column, teams are left as
    they are. columns and only_rows come from the import's matching screen (see import_staff).
    """
    report = ImportReport()
    headers, rows = read_rows(upload)
    questions = list(event.questions.all())
    also = {}
    for q in questions:
        also[q.key] = f"q:{q.key}"
        also[q.label] = f"q:{q.key}"
    columns = columns if columns is not None else match_columns(headers, STUDENT_COLUMNS, also)
    used = set(columns.values())
    if not {"name", "student_id"} <= used:
        report.problems.append("The first row needs a Name and a KL ID column: each student is matched by their KL ID.")
        return report
    mode = team_mode_for(used, team_mode)
    team_column = team_column_for(mode) in used
    plan = plan_teams([(line, _row_data(headers, row, columns)[0]) for line, row in enumerate(rows, start=2)], mode)
    report.warnings += plan.warnings
    teams_cache = {}
    seen = {}
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, extra = _row_data(headers, row, columns)
        data["student_id"] = clean_student_id(data.get("student_id"))
        if not data["student_id"]:
            report.problems.append(f"Row {line}: no KL ID.")
            continue
        if not " ".join((data.get("name") or "").split()):
            report.problems.append(f"Row {line}: no name.")
            continue
        key = data["student_id"].lower()
        if key in seen:
            report.problems.append(f"Row {line}: KL ID {data['student_id']} is also in row {seen[key]}.")
            continue
        seen[key] = line
        if team_column and line in plan.errors:
            report.problems += plan.errors[line]
            continue
        group = plan.groups.get(plan.row_key.get(line)) if team_column else None
        answers = {name[2:]: value for name, value in data.items() if name.startswith("q:") and value}
        with transaction.atomic():
            planned_team = None
            if group is not None:
                planned_team, problem = _team_for_group(event, group, mode, teams_cache, report.warnings)
                if problem:
                    report.problems.append(f"Row {line}: {problem}")
                    continue
            person, _, problem = _upsert_person(data, id_first=True)
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
            membership = TeamMember.objects.filter(registration=registration).select_related("team").first()
            if planned_team is not None:
                team = planned_team
            elif team_column:
                if membership:
                    old_team = membership.team
                    membership.delete()
                    what = "team name" if mode == "name" else "team number"
                    report.warnings.append(f"Row {line}: {person.display_name} has no {what}, so they were taken out of "
                                           f"{old_team.name} and are listed under Not in a team.")
                    if _drop_empty_team_named_after(old_team, person):
                        report.warnings.append(f"Row {line}: removed the empty team \"{old_team.name}\" that was made for {person.display_name} alone.")
                continue
            elif membership:
                team = membership.team
            else:
                continue  # no team column and not in a team: stays under Not in a team
            if membership and membership.team_id != team.pk:
                old_team = membership.team
                report.warnings.append(f"Row {line}: {person.display_name} moved from {old_team.name} to {team.name}.")
                membership.team = team
                membership.save(update_fields=["team"])
                if _drop_empty_team_named_after(old_team, person):
                    report.warnings.append(f"Row {line}: removed the empty team \"{old_team.name}\" that was made for {person.display_name} alone.")
            elif not membership:
                # By number, a team's first row is its leader (the leader can rename a "Team <number>" team).
                leader = mode != "name" and not team.members.filter(is_leader=True).exists()
                membership = TeamMember.objects.create(team=team, registration=registration, extra=extra, is_leader=leader)
            if extra and membership.extra != {**membership.extra, **extra}:
                membership.extra = {**membership.extra, **extra}
                membership.save(update_fields=["extra"])
            changed = []
            values = group.values if group is not None else {n: data.get(n) for n, _ in TEAM_VALUE_FIELDS}
            for name, _ in TEAM_VALUE_FIELDS:
                if values.get(name) and getattr(team, name) != values[name]:
                    setattr(team, name, values[name])
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
        if member_conflict(faculty.person_id, team):
            notes.append(f"Skipped {team.name}: {faculty} is one of its members.")
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
            if member_conflict(faculty.person_id, team):
                report.problems.append(f"Row {line}: {faculty} is a member of {team.name}, so they can't judge it. Not allocated.")
                continue
            if Assignment.objects.filter(round=round_, faculty=faculty, team=team).exists():
                continue
            clash = room_conflict(round_.pk, faculty.pk, team)
            if clash:
                report.warnings.append(f"Row {line}: {faculty}, {team.name}: {clash}")
            from .models import volunteer_conflict

            clash = volunteer_conflict(faculty.person_id, team)
            if clash:
                report.problems.append(f"Row {line}: {faculty}, {team.name}: {clash}")
                continue
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
            + "\n\nSign in with your email and password as usual; the Judging page lists the teams allocated to you. "
            "Forgot your password? Use \"Forgot password\" on the sign-in page.\n"
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
        values["student_id"] = clean_student_id(values["student_id"])
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
        name = clean_team_name(data.get("team"))
        if not name:
            report.problems.append(f"Row {line}: no team name.")
            continue
        slots = member_slots(data, max_size)
        problem = team_row_problem(event, line, name, slots, seen_teams, seen_people)
        if problem:
            report.problems.append(problem)
            continue
        try:
            with transaction.atomic():
                team = Team.objects.create(event=event, name=name, project_title=data.get("project_title") or "",
                                           project_description=data.get("project_description") or "", extra=extra)
        except IntegrityError:
            report.problems.append(f"Row {line}: there is already a team called \"{name}\" in this hackathon.")
            continue
        with transaction.atomic():
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
    name = clean_team_name(name)
    if name.lower() in seen_teams:
        return f"Row {line}: the team name \"{name}\" is also in row {seen_teams[name.lower()]}."
    seen_teams[name.lower()] = line
    if find_team(event, name):
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
    team_name = clean_team_name(team_name)
    if not team_name:
        raise RegistrationError("Give your team a name.")
    if find_team(event, team_name):
        raise RegistrationError(f"Another team already has the name \"{team_name}\". Choose another name.")
    try:
        with transaction.atomic():
            team = Team.objects.create(event=event, name=team_name)
    except IntegrityError:
        raise RegistrationError(f"Another team already has the name \"{team_name}\". Choose another name.") from None
    for registration, _, is_leader in regs:
        TeamMember.objects.create(team=team, registration=registration, is_leader=is_leader)
    from registrations.notify import notify_added

    transaction.on_commit(lambda: [notify_added(r) for r, new, lead in regs if new and not lead])
    return team, lead_reg


# --- Teams: "Not in a team", teams students form themselves, and tidying made-up teams --------------


class TeamError(Exception):
    """A team change that can't be made, with the reason to show."""


def size_limits(event) -> tuple[int, int]:
    return event.team_size_min or 1, event.team_size_max or 6


def not_in_a_team(event):
    """Confirmed registrations at this hackathon whose student is in no team."""
    return (Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED, team_membership__isnull=True)
            .select_related("person").order_by("person__full_name"))


def teams_locked_reason(event) -> str:
    """Why students can't form or change their own teams right now, or "" when they can."""
    if not event.students_form_teams:
        return "The organisers have locked teams."
    if Assignment.objects.filter(round__event=event).exists():
        return "Teams are locked: faculty have been allocated to judge them."
    return ""


def _new_team(event, name) -> Team:
    name = clean_team_name(name)
    if not name:
        raise TeamError("Give the team a name.")
    if find_team(event, name):
        raise TeamError(f"Another team already has the name \"{name}\". Choose another name.")
    try:
        with transaction.atomic():
            return Team.objects.create(event=event, name=name)
    except IntegrityError:
        raise TeamError(f"Another team already has the name \"{name}\". Choose another name.") from None


@transaction.atomic
def add_to_team(event, registrations, team=None, new_name="") -> Team:
    """Put students who are not in a team into an existing team or a new one, within the team size limits.

    A new team's first student is its leader (change it on the team's page)."""
    low, high = size_limits(event)
    registrations = list(registrations)
    if not registrations:
        raise TeamError("Tick at least one student.")
    taken = [r for r in registrations if TeamMember.objects.filter(registration=r).exists()]
    if taken:
        raise TeamError(f"{taken[0].person.display_name} is already in a team.")
    if any(r.event_id != event.pk or not r.is_confirmed for r in registrations):
        raise TeamError("Only students registered for this hackathon can be put in its teams.")
    if team is not None and clean_team_name(new_name):
        raise TeamError("Choose an existing team or type a new name, not both.")
    if team is None:
        if not clean_team_name(new_name):
            raise TeamError("Choose a team, or type a name for a new one.")
        if not low <= len(registrations) <= high:
            raise TeamError(f"A new team needs {low} to {high} members; you chose {len(registrations)}.")
        team = _new_team(event, new_name)
        leader = True
    else:
        if team.event_id != event.pk:
            raise TeamError("That team belongs to another event.")
        size = team.members.count()
        if size + len(registrations) > high:
            raise TeamError(f"{team.name} has {size} member{'s' if size != 1 else ''}; teams here have at most {high}, "
                            f"so {len(registrations)} more won't fit.")
        leader = not team.members.filter(is_leader=True).exists()
    for registration in registrations:
        TeamMember.objects.create(team=team, registration=registration, is_leader=leader)
        leader = False
    return team


def _registrations_by_ids(event, ids):
    """[(KL ID as typed, registration or None)] for teammates named by KL ID at this hackathon."""
    out = []
    for raw in ids:
        sid = clean_student_id(raw)
        if not sid:
            continue
        reg = (Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED, person__student_id__iexact=sid)
               .select_related("person").first())
        out.append((sid, reg))
    return out


def _teammates(event, leader_reg, ids, exclude=()):
    regs, seen = [], {(leader_reg.person.student_id or "").lower()} - {""}
    for sid, reg in _registrations_by_ids(event, ids):
        if sid.lower() in seen:
            raise TeamError(f"KL ID {sid} is listed twice (or is you).")
        seen.add(sid.lower())
        if reg is None:
            raise TeamError(f"No student with KL ID {sid} is registered for this hackathon. They need to register first.")
        if reg.pk == leader_reg.pk or reg.pk in exclude:
            raise TeamError(f"KL ID {sid} is you.")
        if TeamMember.objects.filter(registration=reg).exists():
            raise TeamError(f"{reg.person.display_name} ({sid}) is already in a team.")
        regs.append(reg)
    return regs


@transaction.atomic
def create_student_team(event, leader_reg, name, teammate_ids) -> Team:
    """A student not in a team creates one, with teammates by KL ID. They are its leader."""
    reason = teams_locked_reason(event)
    if reason:
        raise TeamError(reason)
    if TeamMember.objects.filter(registration=leader_reg).exists():
        raise TeamError("You're already in a team.")
    mates = _teammates(event, leader_reg, teammate_ids)
    low, high = size_limits(event)
    size = len(mates) + 1
    if not low <= size <= high:
        raise TeamError(f"Teams here have {low} to {high} members, including you. This team would have {size}.")
    team = _new_team(event, name)
    TeamMember.objects.create(team=team, registration=leader_reg, is_leader=True)
    for reg in mates:
        TeamMember.objects.create(team=team, registration=reg)
    return team


def _leader_team(event, leader_reg) -> Team:
    reason = teams_locked_reason(event)
    if reason:
        raise TeamError(reason)
    membership = TeamMember.objects.filter(registration=leader_reg, is_leader=True).select_related("team").first()
    if membership is None:
        raise TeamError("Only your team's leader can change its members.")
    return membership.team


@transaction.atomic
def leader_add_teammate(event, leader_reg, teammate_id) -> Registration:
    team = _leader_team(event, leader_reg)
    _, high = size_limits(event)
    if team.members.count() >= high:
        raise TeamError(f"Your team already has {high} members, the most allowed here.")
    reg = _teammates(event, leader_reg, [teammate_id])
    if not reg:
        raise TeamError("Type your teammate's KL ID.")
    TeamMember.objects.create(team=team, registration=reg[0])
    return reg[0]


@transaction.atomic
def leader_rename_team(event, leader_reg, name) -> Team:
    """The leader of a "Team <number>" team gives it a name, until teams lock. Never a member's own name or KL ID."""
    team = _leader_team(event, leader_reg)
    if not team.has_number_name:
        raise TeamError("Your team already has a name. Ask the organisers to change it.")
    name = clean_team_name(name)
    if not name:
        raise TeamError("Type a name for your team.")
    for member in team.members.select_related("registration__person"):
        if name.lower() in {clean_team_name(v).lower() for v in (member.registration.person.full_name, member.registration.person.student_id) if v}:
            raise TeamError("Use a team name, not a person's name or KL ID.")
    clash = find_team(event, name)
    if clash and clash.pk != team.pk:
        raise TeamError(f"Another team already has the name \"{name}\". Choose another name.")
    team.name = name
    try:
        with transaction.atomic():
            team.save(update_fields=["name"])
    except IntegrityError:
        raise TeamError(f"Another team already has the name \"{name}\". Choose another name.") from None
    return team


@transaction.atomic
def leader_remove_teammate(event, leader_reg, member_pk) -> Registration:
    team = _leader_team(event, leader_reg)
    member = team.members.filter(pk=member_pk).select_related("registration__person").first()
    if member is None:
        raise TeamError("That student isn't in your team.")
    if member.registration_id == leader_reg.pk:
        raise TeamError("You're the leader, so you can't remove yourself. Ask the organisers to change the leader.")
    low, _ = size_limits(event)
    if team.members.count() - 1 < low:
        raise TeamError(f"Teams here need at least {low} members. Add someone before removing a teammate.")
    member.delete()
    return member.registration


def one_person_teams_named_after_member(event):
    """([teams to move], [(team, why it is kept)]): one-person teams whose name is their member's name.

    Such teams were made up by older imports. Moving them puts the student under "Not in a team" and deletes the
    team; a team with allocations, marks, attendance or a round decision is kept and listed instead."""
    move, kept = [], []
    teams = Team.objects.filter(event=event).annotate(size=Count("members")).filter(size=1)
    for team in teams.prefetch_related("members__registration__person"):
        person = team.members.all()[0].registration.person
        if not named_after(team, person):
            continue
        reasons = [label for label, used in (("allocations", team.assignments.exists()),
                                             ("marks", Score.objects.filter(assignment__team=team).exists()),
                                             ("attendance", team.attendance.exists()),
                                             ("a round decision", team.outcomes.exists())) if used]
        if reasons:
            kept.append((team, "has " + ", ".join(reasons)))
        else:
            move.append(team)
    return move, kept


@transaction.atomic
def move_one_person_teams(event) -> tuple[int, list]:
    """Do it: (how many students now under "Not in a team", [(team, why kept)])."""
    move, kept = one_person_teams_named_after_member(event)
    for team in move:
        team.members.all().delete()
        team.delete()
    return len(move), kept


# --- Supervisors and volunteers (one tab, one Add form, one import) -----------------------------------


class HelperError(Exception):
    """A supervisor or volunteer row that can't be saved, with the reason to show."""


ROLE_WORDS = {"supervisor": "supervisor", "supervising": "supervisor", "sup": "supervisor",
              "volunteer": "volunteer", "volunteering": "volunteer", "vol": "volunteer"}


def helper_role(text) -> str:
    """ "Supervisor" / "Volunteer" (any case, or the start of the word) -> "supervisor" / "volunteer", else ""."""
    word = _key(text)
    if word in ROLE_WORDS:
        return ROLE_WORDS[word]
    if word.startswith("sup"):
        return "supervisor"
    if word.startswith("vol"):
        return "volunteer"
    return ""


def _clean_id(value) -> str:
    value = str(value if value is not None else "").strip()
    return value.split(".", 1)[0] if re.fullmatch(r"\d+\.0+", value) else value


def match_helper(employee_id="", student_id="", email="") -> Person | None:
    """The person these details belong to: by employee ID, then KL ID (or its KL email), then email."""
    if employee_id:
        person = Person.objects.filter(employee_id__iexact=employee_id).first()
        if person:
            return person
    if student_id:
        person = Person.objects.filter(student_id__iexact=student_id).first()
        kl = kl_email_for(student_id)
        person = person or (Person.objects.filter(Q(email__iexact=kl) | Q(kl_email__iexact=kl)).first() if kl else None)
        if person:
            return person
    if email:
        return Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email) | Q(contact_email__iexact=email)).first()
    return None


def find_or_make_helper(name, employee_id="", student_id="", email="", phone="") -> tuple[Person, bool]:
    """The existing person (a faculty member or student anywhere on the portal) or a new one: an employee ID makes a
    Faculty person, a KL ID a Student. Fills blanks on an existing person; never makes a second account."""
    name = " ".join(str(name or "").split())
    employee_id, student_id = _clean_id(employee_id), _clean_id(student_id)
    email = normalise_email(email) or ""
    phone = re.sub(r"\D", "", str(phone or "")) or str(phone or "").strip()
    if not (employee_id or student_id):
        raise HelperError("needs an employee ID or a KL ID")
    person = match_helper(employee_id, student_id, email)
    created = person is None
    if created:
        if not name:
            raise HelperError("no name")
        if employee_id:
            owner = Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).first() if email else None
            person = Person(email=(email or None) if owner is None else None, full_name=name, kind=Person.Kind.FACULTY,
                            source=Person.Source.IMPORT, profile_completed_at=timezone.now())
            if email and is_kl_email(email) and owner is None:
                person.kl_email = email
        else:
            kl = kl_email_for(student_id)
            person = Person(email=kl, kl_email=kl, full_name=name, kind=Person.Kind.STUDENT, source=Person.Source.IMPORT,
                            profile_completed_at=timezone.now(), contact_email=email if email and email != kl else "")
        person.set_unusable_password()
    else:
        if employee_id and person.employee_id and person.employee_id.lower() != employee_id.lower():
            raise HelperError(f"{person.display_name} already has employee ID {person.employee_id}")
        if student_id and person.student_id and person.student_id.lower() != student_id.lower():
            raise HelperError(f"{person.display_name} already has KL ID {person.student_id}")
        if email and not person.email:
            if not Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).exclude(pk=person.pk).exists():
                person.email = email
    for attr, value in (("full_name", name), ("employee_id", employee_id), ("student_id", student_id), ("phone", phone)):
        if value and not getattr(person, attr):
            setattr(person, attr, value)
    if employee_id and student_id:
        raise HelperError("give an employee ID or a KL ID, not both")
    try:
        person.full_clean(exclude=["password", "kl_email"], validate_unique=True)
    except ValidationError as exc:
        raise HelperError("; ".join(m for msgs in exc.message_dict.values() for m in msgs)) from None
    person.save()
    return person, created


def rooms_from_text(event, text) -> tuple[list, list[str]]:
    """("L502, L503" -> [Room, Room], [codes not found in this hackathon])."""
    from .models import clean_room_code

    rooms, unknown = [], []
    for part in re.split(r"[,;/\n]+", str(text or "")):
        code = clean_room_code(part)
        if not code:
            continue
        room = Room.objects.filter(event=event, name__iexact=code).first()
        if room is None:
            unknown.append(part.strip())
        elif room not in rooms:
            rooms.append(room)
    return rooms, unknown


def volunteer_room_conflicts(event, person, rooms) -> list[str]:
    """A volunteer takes attendance in their rooms, so they can't be allocated to judge teams there."""
    room_ids = [r.pk for r in rooms]
    if not room_ids:
        return []
    clashes = (Assignment.objects.filter(round__event=event, faculty__person=person, team__room_id__in=room_ids)
               .select_related("team__room").order_by("team__room__name", "team__name"))
    seen, out = set(), []
    for a in clashes:
        if a.team_id in seen:
            continue
        seen.add(a.team_id)
        out.append(f"{person.display_name} judges {a.team.name} in {a.team.room.name}, so they can't volunteer there.")
    return out


def set_volunteer_rooms(volunteer, rooms):
    problems = volunteer_room_conflicts(volunteer.event, volunteer.person, rooms)
    if problems:
        raise HelperError(" ".join(problems))
    volunteer.rooms.set(rooms)


@transaction.atomic
def add_helper(event, role, name, employee_id="", student_id="", email="", phone="", rooms=None, switches=None):
    """Add a supervisor or volunteer to this hackathon (the same person if they're already on the portal).
    Returns (role row, person created?)."""
    if role not in ("supervisor", "volunteer"):
        raise HelperError("the role must be Supervisor or Volunteer")
    person, created = find_or_make_helper(name, employee_id, student_id, email, phone)
    if role == "supervisor":
        kind = Supervisor.Kind.STUDENT if (person.kind == Person.Kind.STUDENT and not person.employee_id) else Supervisor.Kind.FACULTY
        row, _ = add_supervisor(event, person, kind, switches=switches)
    else:
        row, _ = add_volunteer(event, person, switches=switches)
        if rooms is not None:
            set_volunteer_rooms(row, rooms)
    return row, created


HELPER_COLUMNS = {
    "name": PERSON_COLUMNS["name"] + ["volunteer", "volunteer name"],
    "role": ["role", "type", "supervisor or volunteer", "as"],
    "employee_id": PERSON_COLUMNS["employee_id"],
    "student_id": PERSON_COLUMNS["student_id"],
    "email": PERSON_COLUMNS["email"],
    "phone": PERSON_COLUMNS["phone"],
    "rooms": ["rooms", "room", "room numbers", "rooms to look after", "assigned rooms"],
}


@transaction.atomic
def import_helpers(event, upload, columns=None, only_rows=None) -> ImportReport:
    """Supervisors and volunteers, one row per person: Name, Role, an employee ID or a KL ID, and optional Email,
    Phone and (volunteers) Rooms such as "L502, L503". Permission switches can be columns too (Yes/No)."""
    report = ImportReport()
    headers, rows = read_rows(upload)
    spec = {**HELPER_COLUMNS, **_switch_columns(), **_volunteer_switch_columns()}
    columns = columns if columns is not None else match_columns(headers, spec)
    used = set(columns.values())
    if not {"name", "role"} <= used or not ({"employee_id", "student_id"} & used):
        report.problems.append("The first row needs Name, Role and an Employee ID or KL ID column.")
        return report
    for line, row in enumerate(rows, start=2):
        if only_rows is not None and line not in only_rows:
            continue
        data, _ = _row_data(headers, row, columns)
        role = helper_role(data.get("role"))
        if not role:
            report.problems.append(f"Row {line}: the role \"{data.get('role') or ''}\" isn't Supervisor or Volunteer.")
            continue
        names = _switch_columns() if role == "supervisor" else _volunteer_switch_columns()
        switches = {}
        for name in names:
            word = _key(data.get(name))
            if word in TRUE_WORDS:
                switches[name] = True
            elif word in FALSE_WORDS:
                switches[name] = False
        rooms = None
        if role == "volunteer" and data.get("rooms"):
            rooms, unknown = rooms_from_text(event, data["rooms"])
            if unknown:
                report.problems.append(f"Row {line}: no room {', '.join(unknown)} in this hackathon. Add the rooms first.")
                continue
        try:
            with transaction.atomic():
                _, created = add_helper(event, role, data.get("name"), data.get("employee_id"), data.get("student_id"),
                                        data.get("email"), data.get("phone"), rooms=rooms, switches=switches)
        except HelperError as exc:
            report.problems.append(f"Row {line}: {exc}.")
            continue
        report.created += created
        report.updated += not created
    return report
