"""Faculty first, rooms second: random allocation, automatic room assignment, safe room moves,
attendance and the floor-wise view.

The rule everywhere: in one round, all of one faculty member's teams are in one room.
"""

import random
from collections import defaultdict
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Assignment, Attendance, EventFaculty, Room, Round, Team


# --- Faculty to teams ---------------------------------------------------------------------


@transaction.atomic
def random_allocate(round_, per_team=1, copy_from=None, only_unallocated=True, seed=None):
    """Give each team `per_team` faculty for this round, evenly and at random; never a team's own mentor.

    Faculty are grouped into panels of `per_team`, and each panel takes a share of the teams, so a
    team's judges always judge the same teams (which keeps the room assignment possible).
    copy_from (another round) copies that round's allocations instead. Returns (new allocations, notes).
    """
    event = round_.event
    made, notes = 0, []
    if copy_from is not None:
        for a in Assignment.objects.filter(round=copy_from).select_related("team", "faculty"):
            _, created = Assignment.objects.get_or_create(
                round=round_, faculty=a.faculty, team=a.team, defaults={"source": Assignment.Source.RANDOM}
            )
            made += created
        return made, notes
    faculty = list(EventFaculty.objects.filter(event=event, is_active=True).select_related("person"))
    if not faculty:
        raise ValidationError("Add faculty first.")
    per_team = max(1, min(int(per_team), len(faculty)))
    teams = list(Team.objects.filter(event=event))
    if only_unallocated:
        done = set(Assignment.objects.filter(round=round_).values_list("team_id", flat=True))
        teams = [t for t in teams if t.pk not in done]
    if not teams:
        return 0, ["Every team already has faculty in this round."]
    rng = random.Random(seed)
    rng.shuffle(faculty)
    rng.shuffle(teams)
    panels = [faculty[i:i + per_team] for i in range(0, len(faculty) - per_team + 1, per_team)]
    leftover = faculty[len(panels) * per_team:]
    if leftover:
        notes.append(f"{', '.join(str(f) for f in leftover)} not used: faculty split into panels of {per_team}.")
    shares = [[] for _ in panels]
    for i, team in enumerate(teams):
        shares[i % len(panels)].append(team)
    _fix_mentors(panels, shares)
    for panel, share in zip(panels, shares):
        for team in share:
            for member in panel:
                if team.mentor_id == member.person_id:
                    notes.append(f"{team.name} not given to {member}: they mentor it.")
                    continue
                _, created = Assignment.objects.get_or_create(
                    round=round_, faculty=member, team=team, defaults={"source": Assignment.Source.RANDOM}
                )
                made += created
    return made, notes


def _fix_mentors(panels, shares):
    """Swap teams between panels so no panel judges a team one of its members mentors."""
    mentors = [{f.person_id for f in p} for p in panels]
    for i, share in enumerate(shares):
        for t_index, team in enumerate(share):
            if team.mentor_id not in mentors[i]:
                continue
            for j, other in enumerate(shares):
                if j == i:
                    continue
                for o_index, swap in enumerate(other):
                    if team.mentor_id not in mentors[j] and swap.mentor_id not in mentors[i]:
                        share[t_index], other[o_index] = swap, team
                        break
                else:
                    continue
                break


# --- Groups: faculty and teams that must share a room ------------------------------------------


@dataclass
class Group:
    teams: list = field(default_factory=list)
    faculty: list = field(default_factory=list)

    @property
    def size(self):
        return len(self.teams)

    def who(self):
        names = [str(f) for f in self.faculty]
        if not names:
            return f"{self.teams[0].name} (no faculty yet)"
        return (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else names[0]


def groups_for(round_) -> list[Group]:
    """Every team of the event, grouped so each group's faculty judge only inside it (union-find over shared teams)."""
    teams = {t.pk: t for t in Team.objects.filter(event=round_.event).select_related("room")}
    parent = {f"t{pk}": f"t{pk}" for pk in teams}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    faculty = {}
    for a in Assignment.objects.filter(round=round_).select_related("faculty__person"):
        fkey = f"f{a.faculty_id}"
        faculty[fkey] = a.faculty
        parent.setdefault(fkey, fkey)
        parent[find(fkey)] = find(f"t{a.team_id}")
    groups = defaultdict(Group)
    for pk, team in teams.items():
        groups[find(f"t{pk}")].teams.append(team)
    for fkey, f in faculty.items():
        groups[find(fkey)].faculty.append(f)
    out = list(groups.values())
    for g in out:
        g.teams.sort(key=lambda t: t.name)
        g.faculty.sort(key=lambda f: str(f))
    return out


@dataclass
class Plan:
    rooms: list  # [(room, [groups], teams used)]
    errors: list
    warnings: list

    def assignments(self):
        return {t.pk: room for room, groups, _ in self.rooms for g in groups for t in g.teams}


def plan_rooms(round_) -> Plan:
    """Pack the groups into rooms, biggest first, each into the room with the most space left (rooms fill evenly)."""
    event = round_.event
    rooms = list(Room.objects.filter(event=event).order_by("block", "floor", "name"))
    errors, warnings = [], []
    usable = [r for r in rooms if r.capacity]
    for r in rooms:
        if not r.capacity:
            warnings.append(f"{r.name} has no max teams, so it isn't used. Set it on the Rooms page.")
    groups = sorted(groups_for(round_), key=lambda g: (-g.size, g.who()))
    total = sum(g.size for g in groups)
    if not usable:
        return Plan([], ["Add rooms with a max number of teams first."], warnings)
    capacity = sum(r.capacity for r in usable)
    if total > capacity:
        errors.append(f"{total} teams, but the rooms hold {capacity} in total. Add rooms or raise their max teams.")
    largest = max(r.capacity for r in usable)
    for g in groups:
        if g.size > largest:
            errors.append(f"{g.who()}'s {g.size} teams must share one room, but the largest room holds {largest}. "
                          "Add a bigger room, or allocate fewer teams to them.")
    if errors:
        return Plan([], errors, warnings)
    space = {r.pk: r.capacity for r in usable}
    placed = {r.pk: [] for r in usable}
    for g in groups:
        candidates = [r for r in usable if space[r.pk] >= g.size]
        if not candidates:
            errors.append(f"{g.who()}'s {g.size} teams don't fit in what's left of any room. Add a room or raise max teams.")
            continue
        room = max(candidates, key=lambda r: (space[r.pk], -usable.index(r)))
        space[room.pk] -= g.size
        placed[room.pk].append(g)
    if errors:
        return Plan([], errors, warnings)
    return Plan([(r, placed[r.pk], r.capacity - space[r.pk]) for r in usable], [], warnings)


@transaction.atomic
def apply_plan(round_) -> int:
    plan = plan_rooms(round_)
    if plan.errors:
        raise ValidationError(plan.errors)
    moved = 0
    for team_pk, room in plan.assignments().items():
        moved += Team.objects.filter(pk=team_pk).exclude(room=room).update(room=room)
    return moved


# --- Moving teams by hand -------------------------------------------------------------------------


def move_problems(team, room) -> list[str]:
    """Why moving this one team to room would break a rule, or []."""
    problems = []
    if room is not None and room.capacity:
        used = room.teams.exclude(pk=team.pk).count()
        if used + 1 > room.capacity:
            problems.append(f"{room.name} already has {used} of {room.capacity} teams.")
    for a in team.assignments.select_related("faculty__person", "round"):
        others = Assignment.objects.filter(round=a.round, faculty=a.faculty).exclude(team=team)
        if others.exclude(team__room=room).exists():
            problems.append(f"{a.faculty} judges {team.name} and other teams in {a.round}; moving only this team would put "
                            f"them in two rooms. Move their whole group instead.")
    return problems


@transaction.atomic
def move_group(round_, faculty, room):
    """Move every team in this faculty member's group (for this round) to room, if it fits."""
    group = next((g for g in groups_for(round_) if faculty in g.faculty), None)
    if group is None:
        raise ValidationError(f"{faculty} has no teams in {round_}.")
    if room.capacity:
        staying = room.teams.exclude(pk__in=[t.pk for t in group.teams]).count()
        if staying + group.size > room.capacity:
            raise ValidationError(f"{room.name} holds {room.capacity} teams; {staying} are already there and this group has {group.size}.")
    Team.objects.filter(pk__in=[t.pk for t in group.teams]).update(room=room)
    return group


# --- Attendance -----------------------------------------------------------------------------------


def live_attendance(round_):
    """({team_id: Attendance}, {faculty_id: Attendance}) for this round, not undone."""
    teams, faculty = {}, {}
    for a in Attendance.objects.filter(round=round_, undone_at__isnull=True).select_related("marked_by"):
        (teams if a.team_id else faculty)[a.team_id or a.faculty_id] = a
    return teams, faculty


@transaction.atomic
def mark_attendance(round_, by, present, team=None, faculty=None, members=None):
    """Record present/absent; a new mark replaces the live one (which is kept as undone, with the reason)."""
    target = {"team": team} if team is not None else {"faculty": faculty}
    for old in Attendance.objects.select_for_update().filter(round=round_, undone_at__isnull=True, **target):
        old.undone_at, old.undone_by, old.undo_reason = timezone.now(), by, "Replaced by a new mark"
        old.save(update_fields=["undone_at", "undone_by", "undo_reason"])
    return Attendance.objects.create(round=round_, present=present, marked_by=by, members_present=list(members or []), **target)


@transaction.atomic
def undo_attendance(mark, by, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for undoing.")
    if mark.undone_at:
        raise ValidationError("That mark is already undone.")
    mark.undone_at, mark.undone_by, mark.undo_reason = timezone.now(), by, reason
    mark.save(update_fields=["undone_at", "undone_by", "undo_reason"])


# --- By floor -----------------------------------------------------------------------------------------


def floors(event, round_, block="", floor=""):
    """[(block, floor, [room view])], each room view with its faculty, teams, attendance and marking status.

    Marking status is only "submitted or not" per faculty and team: this never carries marks.
    """
    teams_att, faculty_att = live_attendance(round_) if round_ else ({}, {})
    rooms = Room.objects.filter(event=event).order_by("block", "floor", "name")
    if block:
        rooms = rooms.filter(block__iexact=block)
    if floor:
        rooms = rooms.filter(floor=floor)
    status = defaultdict(list)  # team_id -> [(faculty, submitted)]
    room_faculty = defaultdict(dict)
    if round_:
        for a in Assignment.objects.filter(round=round_).select_related("faculty__person", "team"):
            status[a.team_id].append((a.faculty, a.submitted_at is not None))
            room_faculty[a.team.room_id][a.faculty_id] = a.faculty
    out = defaultdict(list)
    for room in rooms:
        teams = []
        for t in room.teams.prefetch_related("members__registration__person").order_by("name"):
            marks = sorted(status.get(t.pk, []), key=lambda x: str(x[0]))
            teams.append({
                "team": t, "members": [m.registration.person.display_name for m in t.members.all()],
                "member_regs": [(m.registration_id, m.registration.person.display_name) for m in t.members.all()],
                "attendance": teams_att.get(t.pk), "progress": marks,
                "done": bool(marks) and all(s for _, s in marks),
            })
        faculty = [{"faculty": f, "attendance": faculty_att.get(f.pk)} for f in sorted(room_faculty[room.pk].values(), key=str)]
        out[(room.block or "?", room.floor or "?")].append({
            "room": room, "teams": teams, "faculty": faculty,
            "submitted": sum(1 for t in teams for _, s in t["progress"] if s), "allocations": sum(len(t["progress"]) for t in teams),
        })
    unroomed = list(Team.objects.filter(event=event, room__isnull=True).order_by("name"))
    return sorted(out.items()), unroomed


def default_round(event):
    rounds = list(Round.objects.filter(event=event))
    return next((r for r in rounds if r.is_open()), None) or (rounds[0] if rounds else None)
