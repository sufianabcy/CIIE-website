"""Portal pages for editing a hackathon: students, teams, rooms, faculty, supervisors, rounds and rubric,
allocations and detail fields. Everything for a hackathon is edited here, not in the Django admin.

Anyone who oversees the hackathon can open the lists; each change needs the matching switch
(hackathon.access.allowed). Supervisors are managed by admins only.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.forms import inlineformset_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.utils.text import slugify

from people.logins import TEMP_PASSWORD_DAYS, login_states, send_login_details
from people.models import Person
from registrations.models import Registration
from registrations.services import RegistrationError, cancel, register

from . import fields as detail_fields
from . import services
from .access import PERMISSION_GROUPS, PERMISSIONS, PRESETS, allowed, is_admin, overseen_event, permissions_for, require
from . import rooms as room_tools
from .access import VOLUNTEER_PERMISSIONS
from .models import Assignment, Criterion, EventFaculty, FieldDefinition, Room, Round, Supervisor, Team, TeamMember, Volunteer

CLASH = "Not saved: someone else already uses that email, KL ID or employee ID (or this was just saved twice). Check and try again."

def friendly_clash(view):
    """Safety net for saves that race (a double-clicked Save): a database clash becomes a message, never a 500."""
    from functools import wraps

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except IntegrityError:
            messages.error(request, CLASH)
            return redirect(request.get_full_path())

    return wrapper


SECTIONS = [
    ("students", "Students"), ("teams", "Teams"), ("rooms", "Rooms"), ("faculty", "Faculty"),
    ("helpers", "Supervisors & volunteers"), ("rounds", "Rounds and rubric"), ("allocations", "Allocations"), ("fields", "Fields"),
]


def _ctx(request, event, section, **extra):
    return {"event": event, "section": section, "sections": SECTIONS, "can": permissions_for(request.user, event),
            "is_admin": is_admin(request.user), **extra}


def _url(event, name, *args):
    return reverse(f"hackathon_manage_{name}", args=[event.slug, *args])


def _style(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput,)):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.Select):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")
    return form


def _list(request, event, section, title, headers, rows, add_url=None, intro="", add_label="Add", import_kind=None, filters=None,
          extra_url=None, extra_label="", second_add_url=None, second_add_label="", bulk=None, **extra):
    """rows: (cells, edit url, delete url, send button, id for the ticked-rows actions). bulk: {"url", "actions":
    [(value, label, confirm text with N for the count)], "teams" or "rooms" for the actions that need one}."""
    rows = [{"cells": r[0], "edit": r[1] if len(r) > 1 else None, "delete": r[2] if len(r) > 2 else None,
             "send": r[3] if len(r) > 3 else None, "id": r[4] if len(r) > 4 else None} for r in rows]
    if bulk is not None and not bulk.get("actions"):
        bulk = None
    import_url = None
    if import_kind and allowed(request.user, event, "can_import"):
        import_url = reverse("hackathon_import_start", args=[event.slug]) + f"?kind={import_kind}"
    return render(request, "hackathon/manage_list.html", _ctx(
        request, event, section, title=title, headers=headers, rows=rows, add_url=add_url, intro=intro, add_label=add_label,
        import_url=import_url, filters=filters or [], extra_url=extra_url, extra_label=extra_label,
        second_add_url=second_add_url, second_add_label=second_add_label, bulk=bulk, **extra,
    ))


def _form(request, event, section, title, forms_, back, delete_url=None, extra_template=None, **extra):
    return render(request, "hackathon/manage_form.html", _ctx(
        request, event, section, title=title, forms=forms_, back=back, delete_url=delete_url, extra_template=extra_template, **extra,
    ))


# --- Students ---------------------------------------------------------------------------


def _student_regs(event):
    return (
        Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
        .select_related("person", "team_membership__team__room").order_by("person__full_name")
    )


@never_cache
@login_required
def students(request, slug):
    event = overseen_event(request, slug)
    defs = [d for d in detail_fields.ensure_fields(event, "student") if not d.builtin]
    can_edit = allowed(request.user, event, "can_edit_teams")
    details = {d.person_id: d.values for d in event.person_details.all()}
    rows = []
    regs = list(_student_regs(event))
    no_team = request.GET.get("no_team") == "1"
    if no_team:
        regs = [r for r in regs if not hasattr(r, "team_membership")]
    can_send = can_send_logins(request.user, event, "students")
    states = login_states([r.person for r in regs]) if can_send else {}
    for r in regs:
        p = r.person
        team = r.team_membership.team if hasattr(r, "team_membership") else None
        vals = details.get(p.pk, {})
        rows.append((
            [p.display_name, p.email or "", p.student_id, team.name if team else "—", team.room.name if team and team.room else "—"]
            + [detail_fields.display(d, vals.get(d.key)) for d in defs] + ([_state_cell(states[p.pk])] if can_send else []),
            _url(event, "student_edit", r.pk) if can_edit else None,
            None,
            _send_button(request, event, "students", p, states.get(p.pk)) if can_send else None,
            r.pk,
        ))
    return _list(request, event, "students", "Students",
                 ["Name", "Email", "KL ID", "Team", "Room"] + [d.label for d in defs] + (["Login details"] if can_send else []), rows,
                 add_url=_url(event, "student_add") if can_edit else None, add_label="Add a student", import_kind="students",
                 filters=[("All students", "?", not no_team), ("Not in a team", "?no_team=1", no_team)],
                 intro="Each student's team is shown here; change it on the student's page.",
                 extra_url=reverse("hackathon_login_details", args=[event.slug, "students"]) if can_send else None,
                 extra_label="Email login details",
                 bulk={"url": _url(event, "students_bulk"), "teams": _teams_by_number(event) if can_edit else None, "rooms": None,
                       "actions": ([("team", "Put in a team", ""),
                                    ("remove", "Remove from this hackathon", "Remove the N ticked students from this hackathon?")]
                                   if can_edit else [])
                                  + ([("email", "Email login details", "Email new login details to the N ticked students?")] if can_send else [])})


class TeamChoiceForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False, empty_label="Not in a team")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = Team.objects.filter(event=event)


def _set_team(event, registration, team):
    """Put the student in this team, or (team None) take them out of any team. A team is never made up for them."""
    membership = TeamMember.objects.filter(registration=registration).first()
    if team is None:
        if membership:
            membership.delete()
        return
    if membership and membership.team_id != team.pk:
        membership.team = team
        membership.save(update_fields=["team"])
    elif not membership:
        TeamMember.objects.create(team=team, registration=registration)


@never_cache
@login_required
def student_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    registration = get_object_or_404(_student_regs(event), pk=pk) if pk else None
    person = registration.person if registration else None
    details = detail_fields.DetailsForm(request.POST or None, event=event, applies_to="student", person=person,
                                        email_editable=is_admin(request.user) or person is None, prefix="d")
    current = registration.team_membership.team if registration and hasattr(registration, "team_membership") else None
    team_form = _style(TeamChoiceForm(request.POST or None, event=event, initial={"team": current}, prefix="t"))
    if request.method == "POST" and details.is_valid() and team_form.is_valid():
        try:
            with transaction.atomic():
                if person is None:
                    email = details.cleaned_data.get("f_email")
                    kl_id = services.clean_student_id(details.cleaned_data.get("f_student_id"))
                    by_id = Person.objects.filter(student_id__iexact=kl_id).first() if kl_id else None
                    by_email = Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).first() if email else None
                    if by_id and by_email and by_id != by_email:
                        raise ValidationError(f"The KL ID belongs to {by_id.display_name} but the email to {by_email.display_name}.")
                    person = by_id or by_email or Person(email=email, source=Person.Source.IMPORT, kind=Person.Kind.STUDENT)
                    if not person.pk:
                        person.set_unusable_password()
                    elif email and person.email and email != person.email.lower():
                        # Keep the sign-in email; the other address becomes a contact email.
                        person.contact_email = person.contact_email or email
                        details.cleaned_data["f_email"] = person.email
                details.save_to(person)
                if registration is None:
                    registration, _ = register(event=event, person=person, source=Registration.Source.IMPORT, created_by=request.user)
                    from registrations.notify import notify_added

                    new_reg = registration
                    transaction.on_commit(lambda: notify_added(new_reg))
                _set_team(event, registration, team_form.cleaned_data["team"])
        except ValidationError as exc:
            details.add_error(None, exc)
        except RegistrationError as exc:
            details.add_error(None, str(exc))
        except IntegrityError:
            details.add_error(None, CLASH)
        else:
            messages.success(request, f"Saved {person.display_name}.")
            return redirect("hackathon_manage_students", slug)
    return _form(request, event, "students", f"Edit {person.display_name}" if person else "Add a student",
                 [details, team_form], back=_url(event, "students"),
                 delete_url=_url(event, "student_remove", registration.pk) if registration else None,
                 delete_label="Remove from this hackathon")


@login_required
@require_POST
def student_remove(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    registration = get_object_or_404(_student_regs(event), pk=pk)
    TeamMember.objects.filter(registration=registration).delete()
    try:
        cancel(registration, by=request.user)
    except RegistrationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{registration.person.display_name} is no longer registered.")
    return redirect("hackathon_manage_students", slug)


# --- Teams --------------------------------------------------------------------------------


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ["number", "name", "project_title", "project_description", "category", "room", "mentor", "mentor_name"]
        widgets = {"project_description": forms.Textarea(attrs={"rows": 3})}
        labels = {"mentor": "Mentor (a faculty member of this hackathon)", "mentor_name": "Or a mentor without an account"}

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.event = event
        self.fields["room"].queryset = Room.objects.filter(event=event)
        self.fields["mentor"].queryset = Person.objects.filter(faculty_roles__event=event).distinct()
        self.fields["mentor"].help_text = "They can never be allocated to judge this team."

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Team.objects.filter(event=self.instance.event, name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("Another team already has this name.")
        return name

    def clean_number(self):
        from .models import clean_team_number

        number = clean_team_number(self.cleaned_data.get("number"))
        if number and Team.objects.filter(event=self.instance.event, number=number).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Another team already has this number.")
        return number


@never_cache
@login_required
def teams(request, slug):
    event = overseen_event(request, slug)
    can_edit = allowed(request.user, event, "can_edit_teams")
    low, high = services.size_limits(event)
    rows = []
    from .models import team_number_order

    team_list = sorted(Team.objects.filter(event=event).select_related("room", "mentor").prefetch_related("members__registration__person"),
                       key=team_number_order)
    for t in team_list:
        members = ", ".join(("★ " if m.is_leader else "") + m.registration.person.display_name for m in t.members.all())
        rows.append({"team": t, "members": members or "no members yet", "size": len(t.members.all()),
                     "edit": _url(event, "team_edit", t.pk) if can_edit else None})
    return render(request, "hackathon/teams.html", _ctx(
        request, event, "teams", title="Teams", rows=rows, can_edit=can_edit, low=low, high=high,
        free=list(services.not_in_a_team(event)), teams_for_choice=[(r["team"], r["size"]) for r in rows],
        locked_reason=services.teams_locked_reason(event),
        bulk={"url": _url(event, "teams_bulk"), "teams": None, "rooms": list(Room.objects.filter(event=event)),
              "actions": [("room", "Move to a room", ""),
                          ("delete", "Delete", "Delete the N ticked teams? Their students go to Not in a team.")]} if can_edit else None,
        add_url=_url(event, "team_add") if can_edit else None,
        import_url=(reverse("hackathon_import_start", args=[event.slug]) + "?kind=teams") if allowed(request.user, event, "can_import") else None,
    ))


@never_cache
@login_required
def add_to_team(request, slug):
    """Students who are not in a team: into an existing team or a new one (ticked on the Teams tab, or one at a time)."""
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    source = request.POST if request.method == "POST" else request.GET
    chosen = {int(x) for x in source.getlist("r") if str(x).isdigit()}
    regs = [r for r in services.not_in_a_team(event) if r.pk in chosen]
    back = request.POST.get("back") or ""
    if request.method == "POST":
        team = Team.objects.filter(event=event, pk=request.POST.get("team")).first() if request.POST.get("team") else None
        try:
            team = services.add_to_team(event, regs, team=team, new_name=request.POST.get("new_name", ""))
        except services.TeamError as exc:
            messages.error(request, str(exc))
            if back == "one" and regs:
                return redirect(reverse("hackathon_add_to_team", args=[slug]) + "?" + "&".join(f"r={r.pk}" for r in regs))
            return redirect(reverse("hackathon_manage_teams", args=[slug]) + "#not-in-a-team")
        names = ", ".join(r.person.display_name for r in regs)
        messages.success(request, f"Added {names} to {team.name}.")
        return redirect(reverse("hackathon_manage_teams", args=[slug]) + "#not-in-a-team")
    if not regs:
        messages.info(request, "Those students are already in teams.")
        return redirect("hackathon_manage_teams", slug)
    low, high = services.size_limits(event)
    teams_ = Team.objects.filter(event=event).annotate(size=Count("members"))
    return render(request, "hackathon/add_to_team.html", _ctx(
        request, event, "teams", regs=regs, teams_for_choice=[(t, t.size) for t in teams_], low=low, high=high,
    ))


@login_required
@require_POST
def teams_lock(request, slug):
    """Admins: whether students can form their own teams. Off locks teams for students; it also locks by itself
    once faculty are allocated."""
    event = overseen_event(request, slug)
    if not is_admin(request.user):
        raise PermissionDenied
    event.students_form_teams = request.POST.get("open") == "1"
    event.save(update_fields=["students_form_teams"])
    messages.success(request, "Students can form their own teams again." if event.students_form_teams
                     else "Teams are locked: students can no longer create teams or change their members.")
    return redirect("hackathon_manage_teams", slug)


@never_cache
@login_required
def one_person_teams(request, slug):
    """Admins: move one-person teams named after their member (made up by older imports) to Not in a team.
    Shows what would happen first; teams with allocations, marks or attendance are kept and listed."""
    event = overseen_event(request, slug)
    if not is_admin(request.user):
        raise PermissionDenied
    if request.method == "POST":
        if not request.POST.get("confirm"):
            messages.error(request, "Tick the box to confirm.")
            return redirect("hackathon_one_person_teams", slug)
        moved, kept = services.move_one_person_teams(event)
        messages.success(request, f"{moved} student{'s' if moved != 1 else ''} moved to Not in a team; their made-up teams were deleted."
                         + (f" {len(kept)} team{'s' if len(kept) != 1 else ''} kept (allocations, marks or attendance)." if kept else ""))
        return redirect(reverse("hackathon_manage_teams", args=[slug]) + "#not-in-a-team")
    move, kept = services.one_person_teams_named_after_member(event)
    return render(request, "hackathon/one_person_teams.html", _ctx(request, event, "teams", move=move, kept=kept))


@never_cache
@login_required
def team_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    team = get_object_or_404(Team, pk=pk, event=event) if pk else None
    form = _style(TeamForm(request.POST or None, instance=team, event=event))
    old_room = team.room if team else None
    if request.method == "POST" and request.POST.get("member_action"):
        return _team_members(request, event, team)
    if request.method == "POST" and form.is_valid():
        new = form.save(commit=False)
        problems = room_tools.move_problems(Team.objects.get(pk=team.pk), new.room) if team and new.room and new.room != old_room else []
        if problems:
            for p in problems:
                form.add_error("room", p)
            return _form(request, event, "teams", f"Edit {team.name}", [form], back=_url(event, "teams"),
                         delete_url=_url(event, "team_delete", team.pk), extra_template="hackathon/_team_members.html",
                         team=team, members=list(team.members.select_related("registration__person")),
                         free=_student_regs(event).filter(team_membership__isnull=True))
        try:
            with transaction.atomic():
                new.save()
        except IntegrityError:
            form.add_error("name", "Another team already has this name or number.")
            return _form(request, event, "teams", f"Edit {team.name}" if team else "Add a team", [form], back=_url(event, "teams"))
        if new.mentor_id and new.assignments.filter(faculty__person_id=new.mentor_id).exists():
            messages.warning(request, f"{new.mentor} mentors this team but is allocated to judge it. Remove that allocation.")
        messages.success(request, f"Saved {new.name}.")
        return redirect("hackathon_manage_team_edit", slug, new.pk)
    members, free = [], []
    if team:
        members = list(team.members.select_related("registration__person"))
        free = _student_regs(event).filter(team_membership__isnull=True)
    return _form(request, event, "teams", f"Edit {team.name}" if team else "Add a team", [form], back=_url(event, "teams"),
                 delete_url=_url(event, "team_delete", team.pk) if team else None, extra_template="hackathon/_team_members.html",
                 team=team, members=members, free=free)


def _team_members(request, event, team):
    if request.POST["member_action"] == "add":
        registration = get_object_or_404(_student_regs(event), pk=request.POST.get("registration"))
        _set_team(event, registration, team)
        messages.success(request, f"{registration.person.display_name} added.")
    elif request.POST["member_action"] == "leader":
        member = get_object_or_404(TeamMember, team=team, pk=request.POST.get("member"))
        with transaction.atomic():
            team.members.update(is_leader=False)
            member.is_leader = True
            member.save(update_fields=["is_leader"])
        messages.success(request, f"{member.registration.person.display_name} is now the team leader.")
    else:
        TeamMember.objects.filter(team=team, pk=request.POST.get("member")).delete()
        messages.success(request, "Member removed from the team.")
    return redirect("hackathon_manage_team_edit", event.slug, team.pk)


@login_required
@require_POST
def team_delete(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    team = get_object_or_404(Team, pk=pk, event=event)
    if team.assignments.exists():
        messages.error(request, f"{team.name} has faculty allocated. Remove those allocations first.")
        return redirect("hackathon_manage_team_edit", slug, pk)
    team.members.all().delete()
    team.delete()
    messages.success(request, "Team deleted. Its members are now without a team.")
    return redirect("hackathon_manage_teams", slug)


# --- Rooms ---------------------------------------------------------------------------------


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "capacity"]

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.event = event

    def clean_name(self):
        from .models import clean_room_code, parse_room_code

        name = clean_room_code(self.cleaned_data["name"])
        if not parse_room_code(name):
            raise ValidationError("Use a room number like L502: the block letters, then the floor, then the two-digit room.")
        if Room.objects.filter(event=self.instance.event, name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise ValidationError(f"There is already a room {name} in this hackathon.")
        return name


@never_cache
@login_required
def rooms(request, slug):
    event = overseen_event(request, slug)
    can_edit = allowed(request.user, event, "can_edit_teams")
    rows = [
        ([r.name, f"{r.n} / {r.capacity}" if r.capacity else f"{r.n} / no max set"], _url(event, "room_edit", r.pk) if can_edit else None)
        for r in Room.objects.filter(event=event).annotate(n=Count("teams"))
    ]
    return _list(request, event, "rooms", "Rooms", ["Room", "Teams / max capacity"], rows,
                 add_url=_url(event, "room_add") if can_edit else None, add_label="Add a room",
                 intro="Give each room's number (like L502: block L, floor 5, room 02) and how many teams fit. "
                       "Rooms can also come from Excel, with a room number column and a capacity column.")


@never_cache
@login_required
def room_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    room = get_object_or_404(Room, pk=pk, event=event) if pk else None
    form = _style(RoomForm(request.POST or None, instance=room, event=event))
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            form.add_error("name", "There is already a room with this number in this hackathon.")
        else:
            messages.success(request, "Room saved.")
            return redirect("hackathon_manage_rooms", slug)
    return _form(request, event, "rooms", f"Edit {room.name}" if room else "Add a room", [form], back=_url(event, "rooms"),
                 delete_url=_url(event, "room_delete", room.pk) if room else None)


@login_required
@require_POST
def room_delete(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    room = get_object_or_404(Room, pk=pk, event=event)
    if Assignment.objects.filter(team__room=room).exists():
        messages.error(request, f"Faculty are allocated to teams in {room.name}. Move the teams or remove the allocations first.")
        return redirect("hackathon_manage_room_edit", slug, pk)
    room.delete()
    messages.success(request, "Room deleted. Its teams now have no room.")
    return redirect("hackathon_manage_rooms", slug)


# --- Faculty -----------------------------------------------------------------------------


@never_cache
@login_required
def faculty(request, slug):
    event = overseen_event(request, slug)
    defs = [d for d in detail_fields.ensure_fields(event, "faculty") if not d.builtin]
    can_edit = allowed(request.user, event, "can_add_faculty")
    details = {d.person_id: d.values for d in event.person_details.all()}
    rows = []
    roles = list(EventFaculty.objects.filter(event=event).select_related("person").annotate(n=Count("assignments")))
    can_send = can_send_logins(request.user, event, "faculty")
    states = login_states([f.person for f in roles]) if can_send else {}
    sendable = {p.pk for p in login_people(request.user, event, "faculty")[0]} if can_send else set()
    for f in roles:
        p, vals = f.person, details.get(f.person_id, {})
        rows.append((
            [p.display_name + ("" if f.is_active else " (off)"), p.email or "", p.employee_id, p.branch, f.n]
            + [detail_fields.display(d, vals.get(d.key)) for d in defs] + ([_state_cell(states[p.pk])] if can_send else []),
            _url(event, "faculty_edit", f.pk) if can_edit else None,
            None,
            _send_button(request, event, "faculty", p, states.get(p.pk)) if p.pk in sendable else None,
            f.pk,
        ))
    return _list(request, event, "faculty", "Faculty",
                 ["Name", "Email", "Employee ID", "Department", "Teams to judge"] + [d.label for d in defs] + (["Login details"] if can_send else []),
                 rows, add_url=_url(event, "faculty_add") if can_edit else None, add_label="Add a faculty member", import_kind="faculty",
                 extra_url=reverse("hackathon_login_details", args=[event.slug, "faculty"]) if can_send else None,
                 extra_label="Email login details",
                 bulk={"url": _url(event, "faculty_bulk"), "teams": None, "rooms": None,
                       "actions": ([("remove", "Remove from this hackathon", "Remove the N ticked faculty? Anyone with allocations is switched off instead.")]
                                   if can_edit else [])
                                  + ([("email", "Email login details", "Email new login details to the N ticked faculty?")] if can_send else [])})


# --- Login details by email (temporary passwords) -------------------------------------------------


def can_send_logins(user, event, who) -> bool:
    """Faculty: admins, or supervisors given "Make faculty passwords". Students: admins only."""
    if who == "faculty":
        return allowed(user, event, "can_make_logins")
    return who in ("students", "helpers") and is_admin(user)


def login_people(user, event, who):
    """(people this user may send login details to, [(person, why not)])."""
    if who == "faculty":
        people = [f.person for f in EventFaculty.objects.filter(event=event, is_active=True).select_related("person")
                  .order_by("person__full_name")]
    elif who == "helpers":
        found = {}
        for model in (Supervisor, Volunteer):
            for row in model.objects.filter(event=event, is_active=True).select_related("person"):
                found.setdefault(row.person.pk, row.person)
        people = sorted(found.values(), key=lambda p: p.display_name.lower())
    else:
        people = [r.person for r in _student_regs(event)]
    allowed_, skipped = [], []
    for p in people:
        if p.pk == user.pk:
            skipped.append((p, "that's you; change your own password from your profile"))
        elif not is_admin(user) and services._privileged(p):
            skipped.append((p, "has a bigger role, so only an admin can reset their login"))
        else:
            allowed_.append(p)
    return allowed_, skipped


def _state_cell(state):
    times = f" ({state.times_sent} times)" if state.times_sent > 1 else ""
    return state.label + times


def _send_button(request, event, who, person, state):
    if state is None or not person.email:
        return None
    confirm = ""
    if state.done:
        confirm = (f"{person.display_name} already has login details ({state.label.lower()}). "
                   "Email a new temporary password? The one they have now stops working.")
    return {"url": reverse("hackathon_login_details", args=[event.slug, who]), "person": person.pk,
            "label": "Email again" if state.done or state.times_sent else "Email login", "confirm": confirm}


@never_cache
@login_required
def login_details(request, slug, who):
    """Email login details: to everyone not sent yet, to everyone, or (from the list) to one person."""
    event = overseen_event(request, slug)
    if who not in ("faculty", "students", "helpers"):
        raise Http404
    if not can_send_logins(request.user, event, who):
        raise PermissionDenied
    people, skipped = login_people(request.user, event, who)
    states = login_states(people)
    noun = {"faculty": "faculty member(s)", "students": "student(s)", "helpers": "supervisor(s) and volunteer(s)"}[who]
    if request.method == "POST":
        if request.POST.get("person"):
            targets = [p for p in people if str(p.pk) == request.POST["person"]]
            if not targets:
                raise Http404
        elif request.POST.get("scope") == "remaining":
            targets = [p for p in people if p.email and not states[p.pk].done]
        elif request.POST.get("scope") == "all":
            targets = [p for p in people if p.email]
        else:
            raise Http404
        role = {"faculty": "faculty", "students": "student", "helpers": "helper"}[who]
        sent = sum(send_login_details(p, event=event, role=role) for p in targets)
        if sent:
            messages.success(request, f"Login details emailed to {sent} {noun}. Each has a new temporary password, "
                                      "valid for a week; they choose their own at first sign-in.")
        if len(targets) - sent:
            messages.info(request, f"{len(targets) - sent} not sent again: their login details went out a moment ago.")
        if not targets:
            messages.info(request, "Nobody to send to.")
        return redirect("hackathon_manage_" + who, slug)
    emailable = [p for p in people if p.email]
    remaining = [p for p in emailable if not states[p.pk].done]
    rows = [(p, states[p.pk]) for p in people]
    return render(request, "hackathon/login_details.html", _ctx(
        request, event, who, who=who, noun=noun, rows=rows, skipped=skipped, emailable=emailable, remaining=remaining,
        already=[(p, states[p.pk]) for p in emailable if states[p.pk].done],
        own_password=sum(1 for p in emailable if states[p.pk].code == "own_password"),
        emailed_before=sum(1 for p in emailable if states[p.pk].code in ("sent", "queued")),
        temp_only=sum(1 for p in remaining if states[p.pk].code == "temp_only"),
        failed=sum(1 for p in remaining if states[p.pk].code == "failed"),
        no_email=[p for p in people if not p.email], days=TEMP_PASSWORD_DAYS,
    ))


class FacultyActiveForm(forms.Form):
    is_active = forms.BooleanField(label="Can judge at this hackathon", required=False, initial=True)


@never_cache
@login_required
def faculty_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    require(request.user, event, "can_add_faculty")
    role = get_object_or_404(EventFaculty, pk=pk, event=event) if pk else None
    person = role.person if role else None
    details = detail_fields.DetailsForm(request.POST or None, event=event, applies_to="faculty", person=person,
                                        email_editable=is_admin(request.user) or person is None, prefix="d")
    active = _style(FacultyActiveForm(request.POST or None, initial={"is_active": role.is_active if role else True}, prefix="a"))
    if request.method == "POST" and details.is_valid() and active.is_valid():
        try:
            with transaction.atomic():
                if person is None:
                    email = details.cleaned_data.get("f_email")
                    person = Person.objects.filter(email__iexact=email).first() or Person(email=email, source=Person.Source.IMPORT)
                    if not person.pk:
                        person.set_unusable_password()
                details.save_to(person)
                role, _ = services.add_faculty(event, person)
                role.is_active = active.cleaned_data["is_active"]
                role.save(update_fields=["is_active"])
        except ValidationError as exc:
            details.add_error(None, exc)
        except IntegrityError:
            details.add_error(None, CLASH)
        else:
            messages.success(request, f"Saved {person.display_name}.")
            return redirect("hackathon_manage_faculty", slug)
    return _form(request, event, "faculty", f"Edit {person.display_name}" if person else "Add a faculty member",
                 [details, active], back=_url(event, "faculty"),
                 delete_url=_url(event, "faculty_remove", role.pk) if role else None, delete_label="Remove from this hackathon")


@login_required
@require_POST
def faculty_remove(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_add_faculty")
    role = get_object_or_404(EventFaculty, pk=pk, event=event)
    if role.assignments.exists():
        role.is_active = False
        role.save(update_fields=["is_active"])
        messages.warning(request, f"{role} has allocations, so they were switched off instead of removed.")
    else:
        role.delete()
        messages.success(request, "Removed from this hackathon's faculty.")
    return redirect("hackathon_manage_faculty", slug)


# --- Supervisors and volunteers: one tab, one Add form (admins only) --------------------------------------


ROLE_LABELS = {"supervisor": "Supervisor", "volunteer": "Volunteer"}


class HelperForm(forms.Form):
    """Who they are. The same person is reused if they're already on the portal (as faculty or a student)."""

    name = forms.CharField(max_length=150)
    role = forms.ChoiceField(choices=list(ROLE_LABELS.items()), widget=forms.RadioSelect(attrs={"class": "form-check-input"}))
    employee_id = forms.CharField(label="Employee ID", max_length=50, required=False,
                                  help_text="For faculty and staff. Someone new with an employee ID is added as Faculty.")
    student_id = forms.CharField(label="KL ID", max_length=20, required=False,
                                 help_text="For students. Someone new with a KL ID is added as a Student.")
    email = forms.EmailField(required=False, help_text="Optional. Needed to email them their login details.")
    phone = forms.CharField(max_length=20, required=False)

    def clean(self):
        cleaned = super().clean()
        has_emp, has_kl = bool((cleaned.get("employee_id") or "").strip()), bool((cleaned.get("student_id") or "").strip())
        if not (has_emp or has_kl):
            raise ValidationError("Give their employee ID or their KL ID.")
        if has_emp and has_kl:
            raise ValidationError("Give an employee ID or a KL ID, not both.")
        return cleaned


class SupervisorSwitchesForm(forms.ModelForm):
    class Meta:
        model = Supervisor
        fields = ["kind", "is_active", *PERMISSIONS]
        labels = {"kind": "Type", "is_active": "Active at this hackathon"}

    def __init__(self, *args, adding=False, **kwargs):
        super().__init__(*args, **kwargs)
        if adding:
            del self.fields["kind"]  # from the person: an employee ID makes them Faculty, a KL ID a Student
        for name, (label, help_text, _) in PERMISSIONS.items():
            self.fields[name].label = label[0].upper() + label[1:]
            self.fields[name].help_text = help_text

    def groups(self):
        return [(title, [self[name] for name in names]) for title, names in PERMISSION_GROUPS]


class VolunteerSettingsForm(forms.ModelForm):
    class Meta:
        model = Volunteer
        fields = ["is_active", "rooms", *VOLUNTEER_PERMISSIONS]
        labels = {"is_active": "Active at this hackathon", "rooms": "Rooms they look after"}
        widgets = {"rooms": forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"})}

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rooms"].queryset = Room.objects.filter(event=event).order_by("block", "floor", "name")
        self.fields["rooms"].help_text = "They see only these rooms: their teams and faculty, and take attendance there."
        for name, (label, help_text, _) in VOLUNTEER_PERMISSIONS.items():
            self.fields[name].label = label[0].upper() + label[1:]
            self.fields[name].help_text = help_text


def _admin_only(request):
    if not is_admin(request.user):
        raise PermissionDenied


def _with_preset(request, prefix):
    """A preset button: the posted form with that preset's switches, to look at before saving (nothing is saved)."""
    key = request.POST.get("preset")
    if request.method != "POST" or key not in PRESETS:
        return None, None
    data = request.POST.copy()
    on = set(PRESETS[key][2])
    for name in PERMISSIONS:
        field = f"{prefix}-{name}"
        if name in on:
            data[field] = "on"
        else:
            data.pop(field, None)
    return data, PRESETS[key][0]


@never_cache
@login_required
def helpers(request, slug):
    """Supervisors and volunteers, with a Role column and a filter. Faculty (judges) are on the Faculty tab."""
    event = overseen_event(request, slug)
    admin = is_admin(request.user)
    role = request.GET.get("role", "")
    supervisors_ = list(Supervisor.objects.filter(event=event).select_related("person"))
    volunteers_ = list(Volunteer.objects.filter(event=event).select_related("person").prefetch_related("rooms"))
    people = [s.person for s in supervisors_] + [v.person for v in volunteers_]
    can_send = admin
    states = login_states(people) if can_send else {}
    rows = []
    for kind, row in [("supervisor", s) for s in supervisors_] + [("volunteer", v) for v in volunteers_]:
        if role and role != kind:
            continue
        p = row.person
        rooms = ", ".join(r.name for r in row.rooms.all()) if kind == "volunteer" else ""
        can = ", ".join(row.switch_labels) or ("only look" if kind == "supervisor" else "nothing yet")
        edit = _url(event, f"{kind}_edit", row.pk) if admin else None
        rows.append((
            [p.display_name, ROLE_LABELS[kind], p.employee_id or p.student_id or "—", p.email or "—",
             rooms or ("—" if kind == "volunteer" else ""), "Yes" if row.is_active else "No", can]
            + ([_state_cell(states[p.pk])] if can_send else []),
            edit, None,
            _send_button(request, event, "helpers", p, states.get(p.pk)) if can_send and p.pk != request.user.pk else None,
        ))
    rows.sort(key=lambda r: (r[0][0].lower(), r[0][1]))
    return _list(request, event, "helpers", "Supervisors & volunteers",
                 ["Name", "Role", "Employee ID / KL ID", "Email", "Rooms", "Active", "Can"] + (["Login details"] if can_send else []), rows,
                 add_url=_url(event, "supervisor_add") if admin else None, add_label="Add a supervisor",
                 extra_url=reverse("hackathon_login_details", args=[event.slug, "helpers"]) if can_send else None,
                 extra_label="Email login details",
                 second_add_url=_url(event, "volunteer_add") if admin else None, second_add_label="Add a volunteer",
                 import_kind="helpers" if admin else None,
                 filters=[("Everyone", "?", not role), ("Supervisors", "?role=supervisor", role == "supervisor"),
                          ("Volunteers", "?role=volunteer", role == "volunteer")],
                 intro="Supervisors oversee this hackathon with the switches an admin gives them. Volunteers look after their rooms "
                       "on the day: attendance, and whether marks are submitted, never the marks. Only admins add them or change "
                       "what they can do. Faculty who judge are on the Faculty tab.")


@never_cache
@login_required
@friendly_clash
def helper_add(request, slug, role="supervisor"):
    """Add a supervisor or a volunteer: name, role, an employee ID or KL ID, optional email and phone, then their
    switches (supervisors) or rooms (volunteers). Someone already on the portal gets the role; no second account."""
    event = overseen_event(request, slug)
    _admin_only(request)
    data, preset = _with_preset(request, "s")
    posted = data or (request.POST if request.method == "POST" else None)
    person_form = _style(HelperForm(posted, prefix="p", initial={"role": request.GET.get("role", role)}))
    sup_form = _style(SupervisorSwitchesForm(posted, prefix="s", adding=True))
    vol_form = _style(VolunteerSettingsForm(posted, prefix="v", event=event))
    if preset:
        messages.info(request, f"Filled in the {preset} switches. Change any of them, then save.")
    elif request.method == "POST" and person_form.is_valid():
        d = person_form.cleaned_data
        settings_form = sup_form if d["role"] == "supervisor" else vol_form
        if settings_form.is_valid():
            switches = {n: settings_form.cleaned_data[n] for n in (PERMISSIONS if d["role"] == "supervisor" else VOLUNTEER_PERMISSIONS)}
            try:
                with transaction.atomic():
                    row, created = services.add_helper(
                        event, d["role"], d["name"], d["employee_id"], d["student_id"], d["email"], d["phone"],
                        rooms=list(settings_form.cleaned_data["rooms"]) if d["role"] == "volunteer" else None, switches=switches)
                    row.is_active = settings_form.cleaned_data["is_active"]
                    row.save(update_fields=["is_active"])
            except services.HelperError as exc:
                person_form.add_error(None, str(exc)[0].upper() + str(exc)[1:] + ".")
            else:
                how = "added" if created else "already on the portal: the role was added to their account"
                messages.success(request, f"{row.person.display_name} ({ROLE_LABELS[d['role']]}): {how}.")
                return redirect(reverse("hackathon_manage_helpers", args=[slug]))
    return render(request, "hackathon/helper_form.html", _ctx(
        request, event, "helpers", title="Add a supervisor or volunteer", person_form=person_form, sup_form=sup_form,
        vol_form=vol_form, presets=[(k, label, help_text) for k, (label, help_text, _) in PRESETS.items()],
        role=(posted.get("p-role") if posted else None) or request.GET.get("role", role), back=_url(event, "helpers"),
    ))


@never_cache
@login_required
def supervisors(request, slug):
    return redirect(reverse("hackathon_manage_helpers", args=[slug]) + "?role=supervisor")


@never_cache
@login_required
@friendly_clash
def supervisor_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    _admin_only(request)
    if pk is None:
        return helper_add(request, slug, role="supervisor")
    row = get_object_or_404(Supervisor, pk=pk, event=event)
    data, preset = _with_preset(request, "s")
    form = _style(SupervisorSwitchesForm(data or request.POST or None, instance=row, prefix="s"))
    if preset:
        messages.info(request, f"Filled in the {preset} switches. Change any of them, then save.")
    elif request.method == "POST" and form.is_valid():
        with transaction.atomic():
            row = form.save()
            services.add_supervisor(event, row.person, row.kind)  # keeps the Supervisor (and Faculty) role in step
        messages.success(request, f"Saved {row.person.display_name}.")
        return redirect("hackathon_manage_helpers", slug)
    return render(request, "hackathon/helper_form.html", _ctx(
        request, event, "helpers", title=f"Edit {row.person.display_name} (Supervisor)", sup_form=form, row=row, role="supervisor",
        presets=[(k, label, help_text) for k, (label, help_text, _) in PRESETS.items()], back=_url(event, "helpers"),
        delete_url=_url(event, "supervisor_remove", row.pk), delete_label="Remove as supervisor",
    ))


@login_required
@require_POST
def supervisor_remove(request, slug, pk):
    event = overseen_event(request, slug)
    _admin_only(request)
    row = get_object_or_404(Supervisor, pk=pk, event=event)
    person = row.person
    row.delete()
    if not person.supervisor_roles.exists():
        person.groups.remove(*person.groups.filter(name="Supervisor"))
    messages.success(request, f"{person.display_name} is no longer a supervisor here.")
    return redirect("hackathon_manage_helpers", slug)


# --- Rounds and rubric -----------------------------------------------------------------------


RUBRIC_FIELDS = ["focus", "suggested_questions", "total_marks", "weight", "min_total", "key_criterion", "key_min_band",
                 "allow_override", "prompts_text"]


class RoundForm(forms.ModelForm):
    prompts_text = forms.CharField(label="Comment prompts (one per line)", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    key_min_band = forms.ChoiceField(label="Key criterion at least", required=False)

    class Meta:
        model = Round
        fields = ["name", "position", "opens_at", "closes_at", "release_marks_at", "focus", "suggested_questions",
                  "total_marks", "weight", "min_total", "key_criterion", "key_min_band", "allow_override"]
        widgets = {
            "opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "release_marks_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "focus": forms.Textarea(attrs={"rows": 2}),
            "suggested_questions": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, event, **kwargs):
        from . import rubric

        super().__init__(*args, **kwargs)
        self.instance.event = event
        self.fields["key_criterion"].queryset = self.instance.criteria.all() if self.instance.pk else Criterion.objects.none()
        self.fields["key_min_band"].choices = [("", "—")] + [(b["name"], b["name"]) for b in rubric.bands(event)]
        self.fields["prompts_text"].initial = "\n".join(rubric.prompts(self.instance))
        self.fields["total_marks"].required = False

    def clean_total_marks(self):
        return self.cleaned_data.get("total_marks") or 100

    def clean(self):
        cleaned = super().clean()
        opens, closes = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens and closes and closes <= opens:
            self.add_error("closes_at", "Must be after it opens.")
        position = cleaned.get("position")
        if position and Round.objects.filter(event=self.instance.event, position=position).exclude(pk=self.instance.pk).exists():
            self.add_error("position", "Another round already has this number.")
        if cleaned.get("key_criterion") and not cleaned.get("key_min_band"):
            self.add_error("key_min_band", "Choose the band the key criterion must reach.")
        return cleaned

    def save(self, commit=True):
        round_ = super().save(commit=False)
        if "prompts_text" in self.cleaned_data and not self.fields["prompts_text"].disabled:
            round_.comment_prompts = [p.strip() for p in self.cleaned_data["prompts_text"].splitlines() if p.strip()]
        if commit:
            round_.save()
        return round_


class BaseCriterionFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        for form in self.deleted_forms:
            if form.instance.pk and form.instance.scores.exists():
                raise ValidationError(f"Faculty already gave marks for \"{form.instance.label}\", so it can't be deleted.")
        for form in self.forms:
            if form.instance.pk and form.cleaned_data and not form.cleaned_data.get("DELETE"):
                top = form.instance.scores.order_by("-marks").values_list("marks", flat=True).first()
                if top is not None and form.cleaned_data.get("max_marks") is not None and form.cleaned_data["max_marks"] < top:
                    form.add_error("max_marks", f"A faculty member already gave {top.normalize():f}.")


class CriterionForm(forms.ModelForm):
    class Meta:
        model = Criterion
        fields = ["position", "label", "tag", "max_marks", "strong_text", "weak_text", "description"]
        widgets = {"strong_text": forms.Textarea(attrs={"rows": 2}), "weak_text": forms.Textarea(attrs={"rows": 2})}
        labels = {"description": "Guidance"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # An empty extra row must count as unchanged, so the order has no starting value.
        self.fields["position"].required = False
        self.fields["position"].initial = None if not self.instance.pk else self.instance.position

    def clean_position(self):
        return self.cleaned_data.get("position") or 0


CriterionFormSet = inlineformset_factory(
    Round, Criterion, form=CriterionForm, formset=BaseCriterionFormSet, extra=2, can_delete=True,
)


@never_cache
@login_required
def rounds(request, slug):
    event = overseen_event(request, slug)
    can_edit = allowed(request.user, event, "can_edit_rounds")
    rows = [
        ([r.name, r.opens_at and r.opens_at.strftime("%d %b, %H:%M") or "—", r.closes_at and r.closes_at.strftime("%d %b, %H:%M") or "—",
          r.state, ", ".join(f"{c.label} /{c.max_marks.normalize():f}" for c in r.criteria.all()) or "no rubric yet"],
         _url(event, "round_edit", r.pk) if can_edit else None)
        for r in Round.objects.filter(event=event).prefetch_related("criteria")
    ]
    return _list(request, event, "rounds", "Rounds and rubric", ["Round", "Opens", "Closes", "State", "Rubric"], rows,
                 add_url=_url(event, "round_add") if can_edit else None, add_label="Add a round",
                 intro="Each round's criteria, descriptors and advancement rule are on its page. Bands, red flags, weights and recommendations: "
                       + ("see Rubric settings." if not allowed(request.user, event, "can_manage_fields") else "use the Rubric settings button."),
                 extra_url=reverse("hackathon_rubric_settings", args=[event.slug]) if allowed(request.user, event, "can_manage_fields") else None,
                 extra_label="Rubric settings")


@never_cache
@login_required
def round_edit(request, slug, pk=None):
    """A round: its times, and (with the fields switch) its rubric. The rubric locks once marks exist."""
    from . import rubric

    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_rounds")
    round_ = get_object_or_404(Round, pk=pk, event=event) if pk else None
    if request.method == "POST" and request.POST.get("action") == "unlock" and round_:
        if not is_admin(request.user):
            raise PermissionDenied
        if request.POST.get("confirm") != "1":
            messages.error(request, "Tick the box to confirm unlocking a rubric that already has marks.")
        else:
            round_.rubric_unlocked = True
            round_.save(update_fields=["rubric_unlocked"])
            messages.warning(request, f"The rubric for {round_.name} is unlocked. Changing it changes how existing marks read.")
        return redirect("hackathon_manage_round_edit", slug, pk)
    can_rubric = allowed(request.user, event, "can_manage_fields")
    locked = bool(round_ and rubric.is_locked(round_))
    form = _style(RoundForm(request.POST or None, instance=round_, event=event,
                            initial=None if round_ else {"position": (event.rounds.count() + 1)}))
    if not allowed(request.user, event, "can_publish"):
        form.fields["release_marks_at"].disabled = True
        form.fields["release_marks_at"].help_text = "Only people who can publish results set this."
    if not can_rubric or locked:
        for name in RUBRIC_FIELDS:
            form.fields[name].disabled = True
    formset = CriterionFormSet(request.POST if request.method == "POST" and can_rubric and not locked else None,
                               instance=round_ or Round(event=event), prefix="c")
    for f in formset.forms:
        _style(f)
        if not can_rubric or locked:
            for field in f.fields.values():
                field.disabled = True
    formset_ok = not (request.method == "POST" and can_rubric and not locked) or formset.is_valid()
    if request.method == "POST" and form.is_valid() and formset_ok:
        try:
            with transaction.atomic():
                saved = form.save()
                if can_rubric and not locked:
                    formset.instance = saved
                    formset.save()
        except IntegrityError:
            form.add_error("position", "Another round already has this number.")
        else:
            messages.success(request, f"Saved {saved.name}.")
            for problem in (rubric.total_problem(saved), rubric.weights_problem(event)):
                if problem:
                    messages.warning(request, problem)
            return redirect("hackathon_manage_rounds", slug)
    return _form(request, event, "rounds", f"Edit {round_.name}" if round_ else "Add a round", [form], back=_url(event, "rounds"),
                 delete_url=_url(event, "round_delete", round_.pk) if round_ else None, extra_template="hackathon/_criteria.html",
                 formset=formset, locked=locked, can_rubric=can_rubric, round_obj=round_,
                 round_total=round_.total_marks if round_ else 100)


@login_required
@require_POST
def round_delete(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_rounds")
    round_ = get_object_or_404(Round, pk=pk, event=event)
    if round_.assignments.exists():
        messages.error(request, "Faculty are allocated in this round. Remove the allocations first.")
        return redirect("hackathon_manage_round_edit", slug, pk)
    round_.delete()
    messages.success(request, "Round deleted.")
    return redirect("hackathon_manage_rounds", slug)


# --- Allocations -------------------------------------------------------------------------------


@never_cache
@login_required
def allocations(request, slug):
    event = overseen_event(request, slug)
    can_edit = allowed(request.user, event, "can_edit_allocations")
    rows = [
        ([a.round.name, a.team.room.name if a.team.room else "—", a.faculty.person.display_name, a.team.name, a.get_source_display(),
          "Submitted" if a.submitted_at else ("Has marks" if a.n else "No marks yet")],
         None, _url(event, "allocation_delete", a.pk) if can_edit else None)
        for a in Assignment.objects.filter(round__event=event).select_related("round", "team__room", "faculty__person")
        .annotate(n=Count("scores")).order_by("round__position", "team__room__name", "faculty__person__full_name", "team__name")
    ]
    extra = {}
    if can_edit:
        from .models import team_number_order

        by_room = {}
        for team in sorted(Team.objects.filter(event=event).select_related("room"), key=team_number_order):
            by_room.setdefault(team.room, []).append(team)
        extra = {"top_template": "hackathon/_allocate_teams.html", "alloc_rounds": list(Round.objects.filter(event=event)),
                 "alloc_faculty": list(EventFaculty.objects.filter(event=event, is_active=True).select_related("person")),
                 "alloc_rooms": sorted(by_room.items(), key=lambda kv: (kv[0] is None, kv[0].name if kv[0] else ""))}
    return _list(request, event, "allocations", "Allocations", ["Round", "Room", "Faculty", "Team", "How", "Marks"], rows,
                 intro="Pick a jury panel and their teams below, or allocate whole rooms on the hackathon page. Remove single allocations here.",
                 **extra)


@login_required
@require_POST
def allocate_teams(request, slug):
    """A jury panel: every ticked faculty member judges every ticked team (all in one room) in the chosen round."""
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_allocations")
    round_ = Round.objects.filter(event=event, pk=request.POST.get("round")).first() if request.POST.get("round", "").isdigit() else None
    faculty_ids = [int(x) for x in request.POST.getlist("faculty") if x.isdigit()]
    team_ids = [int(x) for x in request.POST.getlist("teams") if x.isdigit()]
    faculty_list = list(EventFaculty.objects.filter(event=event, is_active=True, pk__in=faculty_ids).select_related("person"))
    teams = list(Team.objects.filter(event=event, pk__in=team_ids).select_related("room"))
    every_round = request.POST.get("rounds") == "all"
    if round_ is None and not every_round:
        messages.error(request, "Choose a round.")
        return redirect("hackathon_manage_allocations", slug)
    rounds = list(Round.objects.filter(event=event)) if every_round else [round_]
    made, notes = 0, []
    try:
        with transaction.atomic():  # every round or none
            for r in rounds:
                n, round_notes = services.allocate_teams(r, faculty_list, teams)
                made += n
                notes += [f"{r}: {note}" for note in round_notes] if every_round else round_notes
    except (services.AllocationError, ValidationError) as exc:
        text = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, (f"Nothing was allocated. {text}" if every_round else text))
        return redirect("hackathon_manage_allocations", slug)
    where = "every round (the same jury and room)" if every_round else str(round_)
    messages.success(request, f"{made} allocation{'s' if made != 1 else ''} made in {where}: "
                              f"{len(faculty_list)} faculty × {len(teams)} team{'s' if len(teams) != 1 else ''}.")
    for note in notes:
        messages.warning(request, note)
    return redirect("hackathon_manage_allocations", slug)


@login_required
@require_POST
def allocation_delete(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_allocations")
    a = get_object_or_404(Assignment, pk=pk, round__event=event)
    if a.scores.exists() and not allowed(request.user, event, "can_change_marks"):
        messages.error(request, "This allocation has marks. Only someone allowed to change marks can remove it.")
    else:
        a.delete()
        messages.success(request, "Allocation removed.")
    return redirect("hackathon_manage_allocations", slug)


# --- Detail fields --------------------------------------------------------------------------------


class FieldForm(forms.ModelForm):
    choices_text = forms.CharField(label="Choices (one per line)", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    class Meta:
        model = FieldDefinition
        fields = ["applies_to", "label", "kind", "required", "position"]

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.event = event
        self.fields["choices_text"].initial = "\n".join(self.instance.choices or [])
        if self.instance.builtin:
            for name in ("applies_to", "kind"):
                self.fields[name].disabled = True
            self.fields["choices_text"].disabled = True
            if self.instance.always_required:
                self.fields["required"].disabled = True
                self.fields["required"].help_text = "Name and email are always required."

    def clean(self):
        cleaned = super().clean()
        choices = [c.strip() for c in (cleaned.get("choices_text") or "").splitlines() if c.strip()]
        if cleaned.get("kind") == FieldDefinition.Kind.CHOICE and not self.instance.builtin and len(choices) < 2:
            self.add_error("choices_text", "Give at least two choices.")
        cleaned["choices"] = choices
        return cleaned

    def save(self, commit=True):
        defn = super().save(commit=False)
        if not defn.builtin:
            defn.choices = self.cleaned_data["choices"]
            if not defn.key:
                base = slugify(defn.label)[:40] or "field"
                key, n = base, 2
                while FieldDefinition.objects.filter(event=defn.event, applies_to=defn.applies_to, key=key).exists():
                    key, n = f"{base}-{n}", n + 1
                defn.key = key
        if defn.always_required:
            defn.required = True
        if commit:
            defn.save()
        return defn


@never_cache
@login_required
def fields_list(request, slug):
    event = overseen_event(request, slug)
    can_edit = allowed(request.user, event, "can_manage_fields")
    rows = []
    for applies in ("student", "faculty"):
        for d in detail_fields.ensure_fields(event, applies):
            rows.append((
                [d.get_applies_to_display(), d.label, d.get_kind_display() + (f": {', '.join(d.choices)}" if d.choices else ""),
                 "Required" if (d.required or d.always_required) else "Optional", "Built-in" if d.builtin else "Custom"],
                _url(event, "field_edit", d.pk) if can_edit else None,
            ))
    return _list(request, event, "fields", "Student and faculty fields", ["For", "Field", "Type", "", "Kind"], rows,
                 add_url=_url(event, "field_add") if can_edit else None, add_label="Add a field",
                 intro="These fields appear on the student and faculty pages, in the Excel import's column matching, and in exports. Built-in fields are stored on the person's account.")


@never_cache
@login_required
def field_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    require(request.user, event, "can_manage_fields")
    defn = get_object_or_404(FieldDefinition, pk=pk, event=event) if pk else None
    form = _style(FieldForm(request.POST or None, instance=defn, event=event))
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            form.add_error("label", "Another field already has this name.")
        else:
            messages.success(request, "Field saved.")
            return redirect("hackathon_manage_fields", slug)
    return _form(request, event, "fields", f"Edit {defn.label}" if defn else "Add a field", [form], back=_url(event, "fields"),
                 delete_url=_url(event, "field_delete", defn.pk) if defn and not defn.builtin else None)


@login_required
@require_POST
def field_delete(request, slug, pk):
    event = overseen_event(request, slug)
    require(request.user, event, "can_manage_fields")
    defn = get_object_or_404(FieldDefinition, pk=pk, event=event, builtin="")
    defn.delete()
    messages.success(request, "Field deleted. Values already saved for it stay in the database.")
    return redirect("hackathon_manage_fields", slug)


# --- Volunteers (the tab and Add form are above) -----------------------------------------------------


@never_cache
@login_required
def volunteers(request, slug):
    return redirect(reverse("hackathon_manage_helpers", args=[slug]) + "?role=volunteer")


@never_cache
@login_required
@friendly_clash
def volunteer_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    _admin_only(request)
    if pk is None:
        return helper_add(request, slug, role="volunteer")
    row = get_object_or_404(Volunteer, pk=pk, event=event)
    form = _style(VolunteerSettingsForm(request.POST or None, instance=row, prefix="v", event=event))
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                saved = form.save(commit=False)
                saved.save()
                services.set_volunteer_rooms(saved, list(form.cleaned_data["rooms"]))
        except services.HelperError as exc:
            form.add_error("rooms", str(exc))
        else:
            messages.success(request, f"Saved {row.person.display_name}.")
            return redirect("hackathon_manage_helpers", slug)
    return render(request, "hackathon/helper_form.html", _ctx(
        request, event, "helpers", title=f"Edit {row.person.display_name} (Volunteer)", vol_form=form, row=row, role="volunteer",
        back=_url(event, "helpers"), delete_url=_url(event, "volunteer_remove", row.pk), delete_label="Remove as volunteer",
    ))


@login_required
@require_POST
def volunteer_remove(request, slug, pk):
    event = overseen_event(request, slug)
    _admin_only(request)
    row = get_object_or_404(Volunteer, pk=pk, event=event)
    person = row.person
    row.delete()
    if not person.volunteer_roles.exists():
        person.groups.remove(*person.groups.filter(name="Volunteer"))
    messages.success(request, f"{person.display_name} is no longer a volunteer here.")
    return redirect("hackathon_manage_helpers", slug)


# --- Rubric settings (per hackathon) ----------------------------------------------------------


class RubricSettingsForm(forms.Form):
    guidance = forms.CharField(label="Guidance for faculty (shown at the top of every marksheet)", required=False,
                               widget=forms.Textarea(attrs={"rows": 3}))
    bands_text = forms.CharField(label="Scoring bands (one per line: name and the lowest %, highest first)", widget=forms.Textarea(attrs={"rows": 4}))
    red_flags_text = forms.CharField(label="Red flags faculty can tick (one per line)", required=False, widget=forms.Textarea(attrs={"rows": 4}))
    self_score_max = forms.IntegerField(label="Student self-score out of", min_value=1, max_value=1000, required=False,
                                        help_text="Leave empty if faculty don't record a self-score.")
    recommendations_text = forms.CharField(label="Recommendations for the final round (one per line)", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, rounds, **kwargs):
        super().__init__(*args, **kwargs)
        self.rounds = rounds
        for r in rounds:
            self.fields[f"w_{r.pk}"] = forms.DecimalField(label=f"Weight of {r.name}", min_value=0, max_value=1, decimal_places=2, max_digits=4)

    def clean_bands_text(self):
        import re

        bands = []
        for line in self.cleaned_data["bands_text"].splitlines():
            if not line.strip():
                continue
            m = re.match(r"^\s*(.+?)\s+(\d+(?:\.\d+)?)\s*%?\s*$", line)
            if not m:
                raise ValidationError(f"\"{line.strip()}\" should look like \"Strong 85\".")
            bands.append({"name": m.group(1), "min": float(m.group(2))})
        bands.sort(key=lambda b: -b["min"])
        if not bands or bands[-1]["min"] != 0:
            raise ValidationError("The lowest band must start at 0%.")
        return bands

    def clean(self):
        from decimal import Decimal

        cleaned = super().clean()
        weights = [cleaned.get(f"w_{r.pk}") for r in self.rounds]
        if self.rounds and None not in weights and abs(sum(weights, Decimal(0)) - 1) >= Decimal("0.005"):
            raise ValidationError(f"The round weights add up to {sum(weights, Decimal(0)).normalize():f}; they must add up to 1.")
        return cleaned


@never_cache
@login_required
def rubric_settings(request, slug):
    from . import rubric

    event = overseen_event(request, slug)
    require(request.user, event, "can_manage_fields")
    settings_row = rubric.settings_for(event)
    rounds = list(Round.objects.filter(event=event))
    if request.method == "POST" and request.POST.get("action") == "copy":
        from events.models import Event

        source = get_object_or_404(Event, pk=request.POST.get("source"), kind=Event.Kind.HACKATHON)
        made = rubric.copy_rubric(source, event)
        messages.success(request, f"Copied {made} round(s) and the rubric settings from {source.title}. Rounds that already have marks were left alone.")
        return redirect("hackathon_rubric_settings", slug)
    initial = {
        "bands_text": "\n".join(f"{b['name']} {b['min']:g}" for b in rubric.bands(event)),
        "red_flags_text": "\n".join(settings_row.red_flags), "self_score_max": settings_row.self_score_max,
        "guidance": settings_row.guidance,
        "recommendations_text": "\n".join(settings_row.recommendations),
        **{f"w_{r.pk}": rubric.weight(r) for r in rounds},
    }
    form = _style(RubricSettingsForm(request.POST or None, rounds=rounds, initial=initial))
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        # Keep each band's meaning from the rubric file, and the top ends too while no band's start changed.
        old = {b["name"].lower(): b for b in settings_row.bands or []}
        same_starts = {(n, float(b.get("min", -1))) for n, b in old.items()} == {(b["name"].lower(), b["min"]) for b in d["bands_text"]}
        for b in d["bands_text"]:
            before = old.get(b["name"].lower(), {})
            if before.get("meaning"):
                b["meaning"] = before["meaning"]
            if same_starts and before.get("max") is not None:
                b["max"] = before["max"]
        settings_row.bands = d["bands_text"]
        settings_row.guidance = d["guidance"].strip()
        settings_row.red_flags = [x.strip() for x in d["red_flags_text"].splitlines() if x.strip()]
        settings_row.self_score_max = d["self_score_max"]
        settings_row.recommendations = [x.strip() for x in d["recommendations_text"].splitlines() if x.strip()]
        settings_row.save()
        for r in rounds:
            r.weight = d[f"w_{r.pk}"]
            r.save(update_fields=["weight"])
        messages.success(request, "Rubric settings saved.")
        return redirect("hackathon_rubric_settings", slug)
    from events.models import Event

    others = Event.objects.filter(kind=Event.Kind.HACKATHON).exclude(pk=event.pk).order_by("-starts_at")
    return _form(request, event, "rounds", "Rubric settings", [form], back=_url(event, "rounds"),
                 extra_template="hackathon/_rubric_copy.html", others=others)


@login_required
def rubric_download(request, slug):
    """This hackathon's rubric as a .json file, in the format "Import rubric from a file" reads."""
    from django.http import HttpResponse

    from . import rubric_file

    event = overseen_event(request, slug)
    require(request.user, event, "can_manage_fields")
    response = HttpResponse(rubric_file.export_text(event), content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{event.slug}-rubric.json"'
    return response


@never_cache
@login_required
def rubric_import(request, slug):
    """Import a rubric file: choose the file, see what it holds, then confirm. All or nothing."""
    from . import rubric_file

    event = overseen_event(request, slug)
    require(request.user, event, "can_manage_fields")
    ctx = {"title": "Import rubric from a file"}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "import":
            raw = request.POST.get("content", "")
        else:
            upload = request.FILES.get("file")
            if upload is None:
                ctx["problems"] = ["Choose a rubric file (.json)."]
                return render(request, "hackathon/rubric_import.html", _ctx(request, event, "rounds", **ctx))
            if upload.size > 1024 * 1024:
                ctx["problems"] = ["That file is over 1 MB; a rubric file is much smaller."]
                return render(request, "hackathon/rubric_import.html", _ctx(request, event, "rounds", **ctx))
            raw = upload.read()
        try:
            spec = rubric_file.parse(raw)
        except rubric_file.RubricFileError as exc:
            ctx["problems"] = exc.problems
            return render(request, "hackathon/rubric_import.html", _ctx(request, event, "rounds", **ctx))
        if action == "import":
            if request.POST.get("confirm") != "1":
                messages.error(request, "Tick the box to confirm the import.")
            else:
                try:
                    rubric_file.apply(event, spec)
                except rubric_file.RubricFileError as exc:
                    ctx["problems"] = exc.problems
                except IntegrityError:
                    ctx["problems"] = ["Nothing was imported: the rounds changed while you were importing. Try again."]
                else:
                    messages.success(request, f"Rubric imported: {len(spec['rounds'])} round(s), {len(spec['bands'])} bands.")
                    return redirect("hackathon_rubric_settings", slug)
        ctx.update(spec=spec, plan=rubric_file.plan(event, spec),
                   content=raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
    return render(request, "hackathon/rubric_import.html", _ctx(request, event, "rounds", **ctx))


# --- Ticked rows on the Students, Teams and Faculty lists ---------------------------------------------


def _teams_by_number(event):
    from .models import team_number_order

    return sorted(Team.objects.filter(event=event), key=team_number_order)


def _ticked(request, queryset):
    ids = [int(x) for x in request.POST.getlist("ids") if x.isdigit()]
    return list(queryset.filter(pk__in=ids))


def _email_logins(request, event, who, people):
    """Send login details to ticked people this user may send to; the rest are counted, never sent."""
    if not can_send_logins(request.user, event, who):
        raise PermissionDenied
    sendable = {p.pk for p in login_people(request.user, event, who)[0]}
    role = {"faculty": "faculty", "students": "student"}[who]
    sent = sum(send_login_details(p, event=event, role=role) for p in people if p.pk in sendable)
    not_sent = len(people) - sent
    if sent:
        messages.success(request, f"Login details emailed to {sent}. Each has a new temporary password, valid for a week.")
    if not_sent:
        messages.info(request, f"{not_sent} not sent: no email, sent a moment ago, or not someone you can reset.")


@login_required
@require_POST
def students_bulk(request, slug):
    from . import bulk

    event = overseen_event(request, slug)
    regs = _ticked(request, _student_regs(event))
    action = request.POST.get("do")
    if not regs:
        messages.error(request, "Tick at least one student.")
    elif action == "email":
        _email_logins(request, event, "students", [r.person for r in regs])
    elif action == "remove":
        require(request.user, event, "can_edit_teams")
        removed, notes = bulk.remove_students(event, regs, request.user)
        messages.success(request, f"{removed} student{'s' if removed != 1 else ''} removed from this hackathon.")
        for note in notes:
            messages.warning(request, note)
    elif action == "team":
        require(request.user, event, "can_edit_teams")
        team = Team.objects.filter(event=event, pk=request.POST.get("team")).first() if request.POST.get("team", "").isdigit() else None
        try:
            team, notes = bulk.put_in_team(event, regs, team=team, new_name=request.POST.get("new_name", ""))
        except services.TeamError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{len(regs)} student{'s' if len(regs) != 1 else ''} now in {team.name}.")
            for note in notes:
                messages.warning(request, note)
    else:
        raise Http404
    return redirect("hackathon_manage_students", slug)


@login_required
@require_POST
def teams_bulk(request, slug):
    from . import bulk

    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    teams = _ticked(request, Team.objects.filter(event=event))
    action = request.POST.get("do")
    if not teams:
        messages.error(request, "Tick at least one team.")
    elif action == "delete":
        deleted, kept = bulk.delete_teams(teams)
        messages.success(request, f"{deleted} team{'s' if deleted != 1 else ''} deleted. Their students are under Not in a team.")
        for note in kept:
            messages.warning(request, note)
    elif action == "room":
        room = Room.objects.filter(event=event, pk=request.POST.get("room")).first() if request.POST.get("room", "").isdigit() else None
        if room is None:
            messages.error(request, "Choose a room.")
        else:
            problems = bulk.move_teams_problems(teams, room)
            if problems:
                for p in problems:
                    messages.error(request, p)
            else:
                moved = bulk.move_teams(teams, room)
                messages.success(request, f"{moved} team{'s' if moved != 1 else ''} moved to {room.name}.")
    else:
        raise Http404
    return redirect("hackathon_manage_teams", slug)


@login_required
@require_POST
def faculty_bulk(request, slug):
    from . import bulk

    event = overseen_event(request, slug)
    roles = _ticked(request, EventFaculty.objects.filter(event=event).select_related("person"))
    action = request.POST.get("do")
    if not roles:
        messages.error(request, "Tick at least one faculty member.")
    elif action == "email":
        _email_logins(request, event, "faculty", [r.person for r in roles if r.is_active])
    elif action == "remove":
        require(request.user, event, "can_add_faculty")
        removed, off = bulk.remove_faculty(roles)
        messages.success(request, f"{removed} removed from this hackathon's faculty.")
        if off:
            messages.warning(request, f"{off} {'has' if off == 1 else 'have'} allocations, so they were switched off instead of removed.")
    else:
        raise Http404
    return redirect("hackathon_manage_faculty", slug)
