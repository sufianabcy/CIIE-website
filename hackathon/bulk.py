"""Changes to many rows at once: replacing a list on import, and the ticked-rows actions on the Students, Teams and
Faculty lists. Anything with allocations, marks or attendance is kept and listed, never deleted by these."""

from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

from registrations.models import Registration
from registrations.services import RegistrationError, cancel

from .models import Assignment, EventFaculty, Room, Team, TeamMember, clean_team_name
from .services import TeamError, _team_is_used, clean_student_id, find_team, size_limits

REPLACE_KINDS = ("students", "teams", "faculty")


def replace_permission(kind) -> str:
    """The switch that removing old rows needs (admins have every switch)."""
    return "can_add_faculty" if kind == "faculty" else "can_edit_teams"


# --- Replace on import ----------------------------------------------------------------------------


def sheet_keys(kind, headers, rows, mapping) -> dict:
    """Who or what the sheet holds, to tell what is missing from it: KL IDs, employee IDs and emails, or team names."""
    def column(key):
        i = mapping.get(key)
        return [row[i] for row in rows if i is not None and i < len(row) and row[i]]

    if kind == "students":
        return {"student_ids": {clean_student_id(v).lower() for v in column("student_id")}}
    if kind == "faculty":
        return {"employee_ids": {str(v).strip().lower() for v in column("employee_id")},
                "emails": {str(v).strip().lower() for v in column("email")}}
    return {"teams": {clean_team_name(v).lower() for v in column("team")}}


@dataclass
class ReplacePlan:
    remove: list = field(default_factory=list)  # registrations, teams or faculty roles
    keep: list = field(default_factory=list)  # (row, why kept)

    def names(self, rows) -> list[str]:
        return [_label(r) for r in rows]


def _label(row) -> str:
    if isinstance(row, Registration):
        return row.person.display_name + (f" ({row.person.student_id})" if row.person.student_id else "")
    if isinstance(row, EventFaculty):
        return row.person.display_name
    return (f"{row.number}. " if row.number else "") + row.name


def replace_plan(event, kind, keys) -> ReplacePlan:
    """What "Replace the old list" removes: students or faculty not in the sheet, or (teams import) every current team,
    as the sheet makes them again. Kept: anything with allocations, marks, attendance or a round decision."""
    plan = ReplacePlan()
    if kind == "students":
        regs = (Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
                .select_related("person", "team_membership__team").order_by("person__full_name"))
        for reg in regs:
            if (reg.person.student_id or "").lower() in keys["student_ids"]:
                continue
            team = reg.team_membership.team if hasattr(reg, "team_membership") else None
            if team is not None and _team_is_used(team):
                plan.keep.append((reg, f"their team {team.name} has allocations, marks or attendance"))
            elif reg.checkins.filter(undone_at__isnull=True).exists():
                plan.keep.append((reg, "they were checked in"))
            else:
                plan.remove.append(reg)
    elif kind == "faculty":
        for role in EventFaculty.objects.filter(event=event).select_related("person").order_by("person__full_name"):
            p = role.person
            if (p.employee_id or "").lower() in keys["employee_ids"] or (p.email or "").lower() in keys["emails"]:
                continue
            if role.assignments.exists() or role.attendance.exists():
                plan.keep.append((role, "they have allocations or attendance"))
            else:
                plan.remove.append(role)
    else:
        for team in Team.objects.filter(event=event).order_by("name"):
            if _team_is_used(team):
                plan.keep.append((team, "it has allocations, marks, attendance or a round decision"))
            else:
                plan.remove.append(team)
    return plan


def apply_replace(event, kind, keys, by=None) -> ReplacePlan:
    """Do it (inside the import's transaction). Returns what was removed and what was kept."""
    plan = replace_plan(event, kind, keys)
    if kind == "students":
        touched = set()
        for reg in plan.remove:
            membership = TeamMember.objects.filter(registration=reg).select_related("team").first()
            if membership:
                touched.add(membership.team)
                membership.delete()
            cancel(reg, by=by)
        for team in touched:
            if not team.members.exists() and not _team_is_used(team):
                team.delete()  # emptied by the replace, never judged: it isn't in the new list
    elif kind == "faculty":
        for role in plan.remove:
            role.delete()
    else:
        for team in plan.remove:
            team.members.all().delete()
            team.delete()
    return plan


# --- Ticked rows: students -------------------------------------------------------------------------


@transaction.atomic
def remove_students(event, registrations, by) -> tuple[int, list[str]]:
    removed, notes = 0, []
    for reg in registrations:
        TeamMember.objects.filter(registration=reg).delete()
        try:
            cancel(reg, by=by)
        except RegistrationError as exc:
            notes.append(f"{reg.person.display_name}: {exc}")
        else:
            removed += 1
    return removed, notes


@transaction.atomic
def put_in_team(event, registrations, team=None, new_name="") -> tuple[Team, list[str]]:
    """Put these students in one team (moving them out of any other), within the team size limit. A new team, or a
    team with no leader, gets the first of them as its leader."""
    registrations = list(registrations)
    if not registrations:
        raise TeamError("Tick at least one student.")
    if team is not None and clean_team_name(new_name):
        raise TeamError("Choose an existing team or type a new name, not both.")
    _, high = size_limits(event)
    notes = []
    if team is None:
        name = clean_team_name(new_name)
        if not name:
            raise TeamError("Choose a team, or type a name for a new one.")
        if find_team(event, name):
            raise TeamError(f"Another team already has the name \"{name}\". Choose it from the list instead.")
        try:
            with transaction.atomic():
                team = Team.objects.create(event=event, name=name)
        except IntegrityError:
            raise TeamError(f"Another team already has the name \"{name}\".") from None
    elif team.event_id != event.pk:
        raise TeamError("That team belongs to another event.")
    staying = team.members.exclude(registration__in=registrations).count()
    if staying + len(registrations) > high:
        raise TeamError(f"{team.name} would have {staying + len(registrations)} members; teams here have at most {high}.")
    leader = not team.members.filter(is_leader=True).exclude(registration__in=registrations).exists()
    for reg in registrations:
        membership = TeamMember.objects.filter(registration=reg).select_related("team").first()
        if membership and membership.team_id == team.pk:
            continue
        if membership:
            old = membership.team
            if _team_is_used(old):
                notes.append(f"{reg.person.display_name} left {old.name}, which already has allocations or marks.")
            membership.delete()
        TeamMember.objects.create(team=team, registration=reg, is_leader=leader)
        leader = False
    return team, notes


# --- Ticked rows: teams ----------------------------------------------------------------------------


@transaction.atomic
def delete_teams(teams) -> tuple[int, list[str]]:
    deleted, kept = 0, []
    for team in teams:
        if team.assignments.exists():
            kept.append(f"{team.name} has faculty allocated, so it was kept. Remove its allocations first.")
            continue
        team.members.all().delete()
        team.delete()
        deleted += 1
    return deleted, kept


def move_teams_problems(teams, room) -> list[str]:
    """Why moving these teams together to this room would break a rule: room capacity, or a faculty member's teams
    ending up in two rooms in one round."""
    problems = []
    moving = {t.pk for t in teams}
    if room.capacity:
        staying = room.teams.exclude(pk__in=moving).count()
        if staying + len(moving) > room.capacity:
            problems.append(f"{room.name} holds {room.capacity} teams; {staying} are already there and you picked {len(moving)}.")
    seen = set()
    for a in Assignment.objects.filter(team_id__in=moving).select_related("faculty__person", "round"):
        if (a.round_id, a.faculty_id) in seen:
            continue
        seen.add((a.round_id, a.faculty_id))
        others = Assignment.objects.filter(round=a.round, faculty=a.faculty).exclude(team_id__in=moving).exclude(team__room=room)
        if others.exists():
            problems.append(f"{a.faculty} judges teams in {a.round} that you didn't pick; moving only some of them would "
                            "put that faculty member in two rooms. Tick all their teams, or move their whole group.")
    return problems


@transaction.atomic
def move_teams(teams, room: Room) -> int:
    Team.objects.filter(pk__in=[t.pk for t in teams]).update(room=room)
    return len(teams)


# --- Ticked rows: faculty --------------------------------------------------------------------------


@transaction.atomic
def remove_faculty(roles) -> tuple[int, int]:
    """(removed, switched off): a faculty member with allocations is switched off instead, so their marks stay."""
    removed = off = 0
    for role in roles:
        if role.assignments.exists():
            if role.is_active:
                role.is_active = False
                role.save(update_fields=["is_active"])
            off += 1
        else:
            role.delete()
            removed += 1
    return removed, off
