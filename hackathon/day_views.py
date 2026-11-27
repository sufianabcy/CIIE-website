"""Allocation tools (random, automatic rooms, group moves), the By floor page and attendance.

Volunteers use the By floor page and attendance; they never see marks. The floor views here carry
only "submitted or not" per faculty and team, for everyone.
"""

from io import BytesIO

from django import forms
from django.db import transaction
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from openpyxl import Workbook

from events.exports import _local, _sheet
from events.models import Event

from . import rooms as room_tools
from .access import allowed, day_allowed, is_admin, oversees, overseen_event, require, volunteer_row
from .attendance import sessions_for_taker
from .models import Assignment, Attendance, EventFaculty, Room, Round, Team

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _download(filename, wb):
    buffer = BytesIO()
    wb.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def day_event(request, slug, tool):
    """The hackathon, if this person may use this day-of tool there (overseers by switch, volunteers by theirs)."""
    event = get_object_or_404(Event, slug=slug, kind=Event.Kind.HACKATHON)
    if not day_allowed(request.user, event, tool):
        raise PermissionDenied
    return event


def _round(request, event):
    pk = request.GET.get("round") or request.POST.get("round")
    if pk and str(pk).isdigit():
        return get_object_or_404(Round, pk=pk, event=event)
    return room_tools.default_round(event)


# --- Faculty to teams, then rooms --------------------------------------------------------------


class RandomForm(forms.Form):
    round = forms.ModelChoiceField(queryset=Round.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    per_team = forms.IntegerField(label="Faculty per jury", min_value=1, max_value=10, initial=1,
                                  help_text="How many faculty judge each team together.",
                                  widget=forms.NumberInput(attrs={"class": "form-control", "style": "max-width:6rem"}))
    teams_per_jury = forms.IntegerField(label="Teams per jury", min_value=1, max_value=100, required=False,
                                        help_text="Empty: share all teams evenly.",
                                        widget=forms.NumberInput(attrs={"class": "form-control", "style": "max-width:6rem"}))
    every_round = forms.BooleanField(label="Same juries and teams in every round", required=False)
    copy_from = forms.ModelChoiceField(label="Or copy from round", queryset=Round.objects.none(), required=False,
                                       empty_label="No, allocate randomly", widget=forms.Select(attrs={"class": "form-select"}))
    only_unallocated = forms.BooleanField(label="Only teams with no faculty yet in this round", required=False, initial=True)

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["round"].queryset = self.fields["copy_from"].queryset = Round.objects.filter(event=event)


@login_required
@require_POST
def random_allocate(request, slug):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_allocations")
    form = RandomForm(request.POST, event=event)
    if not form.is_valid():
        messages.error(request, "Choose a round and how many faculty per team.")
        return redirect("hackathon_dashboard", slug)
    d = form.cleaned_data
    if d["copy_from"] == d["round"]:
        messages.error(request, "Choose a different round to copy from.")
        return redirect("hackathon_dashboard", slug)
    back = "hackathon_manage_allocations" if request.POST.get("back") == "allocations" else "hackathon_dashboard"
    try:
        with transaction.atomic():
            made, notes = room_tools.random_allocate(d["round"], d["per_team"], d["copy_from"], d["only_unallocated"],
                                                     teams_per_jury=d["teams_per_jury"])
            if d["every_round"] and not d["copy_from"]:
                for other in Round.objects.filter(event=event).exclude(pk=d["round"].pk):
                    made += room_tools.random_allocate(other, copy_from=d["round"])[0]
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(back, slug)
    where = "every round (the same juries and teams)" if d["every_round"] and not d["copy_from"] else str(d["round"])
    messages.success(request, f"{made} allocation(s) made in {where}.")
    for note in notes:
        messages.warning(request, note)
    if back == "hackathon_manage_allocations":
        return redirect(back, slug)
    messages.info(request, "Next: assign rooms.")
    return redirect(f"{reverse('hackathon_rooms_plan', args=[slug])}?round={d['round'].pk}")


@never_cache
@login_required
def rooms_plan(request, slug):
    """Preview the automatic room assignment for a round; save only on confirm. Also move whole groups."""
    event = overseen_event(request, slug)
    round_ = _round(request, event)
    can_edit = allowed(request.user, event, "can_edit_teams")
    if request.method == "POST":
        require(request.user, event, "can_edit_teams")
        if request.POST.get("action") == "apply":
            try:
                moved = room_tools.apply_plan(round_)
            except ValidationError as exc:
                for m in exc.messages:
                    messages.error(request, m)
            else:
                messages.success(request, f"Rooms assigned: {moved} team(s) moved. Each faculty member's teams are in one room.")
        elif request.POST.get("action") == "move_group":
            faculty = get_object_or_404(EventFaculty, pk=request.POST.get("faculty"), event=event)
            room = get_object_or_404(Room, pk=request.POST.get("room"), event=event)
            try:
                group = room_tools.move_group(round_, faculty, room)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                messages.success(request, f"Moved {group.size} team(s) of {group.who()} to {room.name}.")
        return redirect(f"{reverse('hackathon_rooms_plan', args=[slug])}?round={round_.pk}")
    plan = room_tools.plan_rooms(round_) if round_ else None
    current = [
        (room, list(room.teams.order_by("name")))
        for room in Room.objects.filter(event=event).order_by("block", "floor", "name")
    ]
    from .services import room_rule_warnings

    return render(request, "hackathon/rooms_plan.html", {
        "event": event, "round": round_, "rounds": Round.objects.filter(event=event), "plan": plan, "current": current,
        "can_edit": can_edit, "conflicts": room_rule_warnings(event),
        "faculty": EventFaculty.objects.filter(event=event, assignments__round=round_).distinct().select_related("person") if round_ else [],
        "rooms": Room.objects.filter(event=event),
    })


# --- By floor --------------------------------------------------------------------------------------


def volunteer_rooms(user, event):
    """None for organisers (every room), or the ids of the rooms this volunteer looks after (maybe none)."""
    if oversees(user, event):
        return None
    row = volunteer_row(user, event)
    return list(row.rooms.values_list("pk", flat=True)) if row else []


def _may_mark(user, event, round_, team=None, faculty=None) -> bool:
    """Volunteers mark only their rooms: their teams, and faculty judging there. Never a team they judge."""
    rooms = volunteer_rooms(user, event)
    if rooms is None:
        return True
    if team is not None:
        judges_it = Assignment.objects.filter(team=team, faculty__person=user).exists()
        return team.room_id in rooms and not judges_it
    return Assignment.objects.filter(round=round_, faculty=faculty, team__room_id__in=rooms).exists()


def _floor_context(request, event):
    round_ = _round(request, event)
    block, floor = request.GET.get("block", ""), request.GET.get("floor", "")
    mine = volunteer_rooms(request.user, event)
    data, unroomed = room_tools.floors(event, round_, block, floor, room_ids=mine)
    all_rooms = Room.objects.filter(event=event) if mine is None else Room.objects.filter(pk__in=mine)
    if mine is not None:
        unroomed = []  # other teams are not theirs to see
    return {
        "mine": mine is not None, "has_rooms": mine is None or bool(mine),
        "event": event, "round": round_, "rounds": Round.objects.filter(event=event), "floors": data, "unroomed": unroomed,
        "block": block, "floor": floor,
        "blocks": sorted({r.block for r in all_rooms if r.block}), "floor_choices": sorted({r.floor for r in all_rooms if r.floor}),
        "can_team_att": False,  # student attendance is taken with attendance sessions (hackathon.attendance)
        "open_sessions": sessions_for_taker(request.user, event),
        "can_faculty_att": day_allowed(request.user, event, "faculty_attendance"),
        "can_progress": day_allowed(request.user, event, "progress"),
        "is_overseer": oversees(request.user, event),
        "can_floors": day_allowed(request.user, event, "floors"),
    }


@never_cache
@login_required
def by_floor(request, slug):
    event = day_event(request, slug, "floors")
    ctx = _floor_context(request, event)
    template = "hackathon/floors_print.html" if request.GET.get("print") else "hackathon/floors.html"
    return render(request, template, ctx)


@login_required
def by_floor_export(request, slug):
    event = day_event(request, slug, "floors")
    ctx = _floor_context(request, event)
    wb = Workbook()
    rows = []
    for (block, floor), rooms in ctx["floors"]:
        for r in rooms:
            faculty = ", ".join(str(f["faculty"]) for f in r["faculty"])
            for t in r["teams"] or [None]:
                row = [block, floor, r["room"].name, r["room"].capacity, faculty]
                if t is None:
                    rows.append(row + ["", "", "", "", "", "", ""])
                    continue
                att = t["attendance"]
                progress = ", ".join(f"{f}: {'done' if s else 'pending'}" for f, s in t["progress"]) if ctx["can_progress"] else ""
                rows.append(row + [t["team"].number, t["team"].name, ", ".join(t["members"]), t["team"].project_title, t["team"].category,
                                   ("Present" if att.present else "Absent") if att else "Not marked", progress])
    _sheet(wb, "By floor", ["Block", "Floor", "Room", "Max teams", "Faculty", "Team no", "Team", "Members", "Project", "Category",
                            f"Attendance ({ctx['round'] or 'no round'})", "Marking (done or pending, never marks)"], rows,
           text_columns=["Team no"])
    return _download(f"{event.slug}-by-floor.xlsx", wb)


# --- Attendance -----------------------------------------------------------------------------------


@login_required
@require_POST
def attendance_mark(request, slug):
    event = get_object_or_404(Event, slug=slug, kind=Event.Kind.HACKATHON)
    round_ = get_object_or_404(Round, pk=request.POST.get("round"), event=event)
    present = request.POST.get("present") == "1"
    if request.POST.get("team"):
        raise Http404("Student attendance is taken with attendance links now.")
    if not day_allowed(request.user, event, "faculty_attendance"):
        raise PermissionDenied
    faculty = get_object_or_404(EventFaculty, pk=request.POST.get("faculty"), event=event)
    if not _may_mark(request.user, event, round_, faculty=faculty):
        raise PermissionDenied  # not judging in one of their rooms
    room_tools.mark_attendance(round_, request.user, present, faculty=faculty)
    messages.success(request, f"{faculty}: {'present' if present else 'absent'}.")
    return redirect(request.POST.get("next") or f"{reverse('hackathon_floors', args=[slug])}?round={round_.pk}")


@login_required
@require_POST
def attendance_undo(request, slug, pk):
    event = get_object_or_404(Event, slug=slug, kind=Event.Kind.HACKATHON)
    mark = get_object_or_404(Attendance, pk=pk, round__event=event)
    if not day_allowed(request.user, event, "team_attendance" if mark.team_id else "faculty_attendance"):
        raise PermissionDenied
    if not _may_mark(request.user, event, mark.round, team=mark.team, faculty=mark.faculty):
        raise PermissionDenied
    try:
        room_tools.undo_attendance(mark, request.user, request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Mark undone.")
    return redirect(request.POST.get("next") or f"{reverse('hackathon_floors', args=[slug])}?round={mark.round_id}")


@login_required
def attendance_export(request, slug):
    event = get_object_or_404(Event, slug=slug, kind=Event.Kind.HACKATHON)
    if not (day_allowed(request.user, event, "team_attendance") or day_allowed(request.user, event, "faculty_attendance")
            or allowed(request.user, event, "can_download")):
        raise PermissionDenied
    wb = Workbook()
    marks = Attendance.objects.filter(round__event=event).select_related(
        "round", "team__room", "faculty__person", "marked_by", "undone_by").order_by("round__position", "marked_at")
    mine = volunteer_rooms(request.user, event)
    if mine is not None:  # a volunteer: only their rooms' teams, and faculty judging there
        judging_here = set(Assignment.objects.filter(round__event=event, team__room_id__in=mine).values_list("round_id", "faculty_id"))
        marks = [a for a in marks if (a.team_id and a.team.room_id in mine) or (a.faculty_id and (a.round_id, a.faculty_id) in judging_here)]
    regs = {}
    for t in Team.objects.filter(event=event, **({} if mine is None else {"room_id__in": mine})).prefetch_related("members__registration__person"):
        for m in t.members.all():
            regs[m.registration_id] = m.registration.person.display_name

    def row(a):
        who = a.team.name if a.team_id else str(a.faculty)
        room = a.team.room.name if a.team_id and a.team.room else ""
        members = ", ".join(regs.get(i, "?") for i in a.members_present)
        return [a.round.name, "Team" if a.team_id else "Faculty", a.team.number if a.team_id else "", who,
                a.team.category if a.team_id else "", room, "Present" if a.present else "Absent", members,
                a.marked_by.display_name, _local(a.marked_at),
                a.undone_by.display_name if a.undone_by else "", _local(a.undone_at), a.undo_reason]

    headers = ["Round", "Who", "Team no", "Name", "Category", "Room", "Mark", "Members ticked present", "Marked by", "Marked at", "Undone by", "Undone at", "Why undone"]
    _sheet(wb, "Attendance", headers, [row(a) for a in marks if not a.undone_at])
    _sheet(wb, "Undone and replaced", headers, [row(a) for a in marks if a.undone_at])
    return _download(f"{event.slug}-attendance.xlsx", wb)


# --- Volunteers' home ------------------------------------------------------------------------------


@never_cache
@login_required
def volunteer_today(request, slug):
    """A volunteer's start page: straight to their tools, floor filter first."""
    event = get_object_or_404(Event, slug=slug, kind=Event.Kind.HACKATHON)
    row = volunteer_row(request.user, event)
    if not row and not is_admin(request.user):
        raise PermissionDenied
    if row is None or oversees(request.user, event):
        return redirect("hackathon_floors", slug)  # organisers see every room
    ctx = _floor_context(request, event)
    ctx["today_only"] = True
    return render(request, "hackathon/floors_print.html" if request.GET.get("print") else "hackathon/floors.html", ctx)
