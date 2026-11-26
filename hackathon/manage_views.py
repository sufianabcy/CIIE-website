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
from django.db.models import Count
from django.forms import inlineformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.utils.text import slugify

from people.models import Person
from registrations.models import Registration
from registrations.services import RegistrationError, cancel, register

from . import fields as detail_fields
from . import services
from .access import PERMISSIONS, allowed, is_admin, overseen_event, permissions_for, require
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
    ("supervisors", "Supervisors"), ("volunteers", "Volunteers"), ("rounds", "Rounds and rubric"), ("allocations", "Allocations"), ("fields", "Fields"),
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


def _list(request, event, section, title, headers, rows, add_url=None, intro="", add_label="Add", import_kind=None, filters=None):
    rows = [{"cells": r[0], "edit": r[1] if len(r) > 1 else None, "delete": r[2] if len(r) > 2 else None} for r in rows]
    import_url = None
    if import_kind and allowed(request.user, event, "can_import"):
        import_url = reverse("hackathon_import_start", args=[event.slug]) + f"?kind={import_kind}"
    return render(request, "hackathon/manage_list.html", _ctx(
        request, event, section, title=title, headers=headers, rows=rows, add_url=add_url, intro=intro, add_label=add_label,
        import_url=import_url, filters=filters or [],
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
    regs = _student_regs(event)
    no_team = request.GET.get("no_team") == "1"
    if no_team:
        regs = regs.filter(team_membership__isnull=True)
    for r in regs:
        p = r.person
        team = r.team_membership.team if hasattr(r, "team_membership") else None
        vals = details.get(p.pk, {})
        rows.append((
            [p.display_name, p.email or "", p.student_id, team.name if team else "—", team.room.name if team and team.room else "—"]
            + [detail_fields.display(d, vals.get(d.key)) for d in defs],
            _url(event, "student_edit", r.pk) if can_edit else None,
        ))
    return _list(request, event, "students", "Students", ["Name", "Email", "KL ID", "Team", "Room"] + [d.label for d in defs], rows,
                 add_url=_url(event, "student_add") if can_edit else None, add_label="Add a student", import_kind="students",
                 filters=[("All students", "?", not no_team), ("No team yet", "?no_team=1", no_team)],
                 intro="Each student's team is shown here; change it on the student's page.")


class TeamChoiceForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False, empty_label="No team (a team of one is made)")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = Team.objects.filter(event=event)


def _set_team(event, registration, team):
    membership = TeamMember.objects.filter(registration=registration).first()
    if team is None:
        if membership:
            return
        name = registration.person.display_name
        team = Team.objects.filter(event=event, name__iexact=name).first() or Team.objects.create(event=event, name=name)
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
                    person = Person.objects.filter(email__iexact=email).first() or Person(email=email, source=Person.Source.IMPORT)
                    if not person.pk:
                        person.set_unusable_password()
                details.save_to(person)
                if registration is None:
                    registration, _ = register(event=event, person=person, source=Registration.Source.IMPORT, created_by=request.user)
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
        fields = ["name", "project_title", "project_description", "room", "mentor", "mentor_name"]
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


@never_cache
@login_required
def teams(request, slug):
    event = overseen_event(request, slug)
    can_edit = allowed(request.user, event, "can_edit_teams")
    rows = []
    for t in Team.objects.filter(event=event).select_related("room", "mentor").prefetch_related("members__registration__person"):
        members = ", ".join(("★ " if m.is_leader else "") + m.registration.person.display_name for m in t.members.all())
        rows.append(([t.name, members or "no members yet", t.project_title or "—", t.room.name if t.room else "—", t.mentor_display or "—"],
                     _url(event, "team_edit", t.pk) if can_edit else None))
    return _list(request, event, "teams", "Teams", ["Team", "Members (★ leader)", "Problem statement", "Room", "Mentor"], rows,
                 add_url=_url(event, "team_add") if can_edit else None, add_label="Add a team", import_kind="teams",
                 intro="Each team is made of registered students. Add or remove members on the team's page, or import a sheet with one row per team.")


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
            form.add_error("name", "Another team already has this name.")
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
    for f in EventFaculty.objects.filter(event=event).select_related("person").annotate(n=Count("assignments")):
        p, vals = f.person, details.get(f.person_id, {})
        rows.append((
            [p.display_name + ("" if f.is_active else " (off)"), p.email or "", p.employee_id, p.branch, f.n]
            + [detail_fields.display(d, vals.get(d.key)) for d in defs],
            _url(event, "faculty_edit", f.pk) if can_edit else None,
        ))
    return _list(request, event, "faculty", "Faculty", ["Name", "Email", "Employee ID", "Department", "Teams to judge"] + [d.label for d in defs],
                 rows, add_url=_url(event, "faculty_add") if can_edit else None, add_label="Add a faculty member", import_kind="faculty")


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


# --- Supervisors (admins only) -----------------------------------------------------------


class SupervisorForm(forms.ModelForm):
    class Meta:
        model = Supervisor
        fields = ["kind", "is_active", *PERMISSIONS]


class FindPersonForm(forms.Form):
    who = forms.CharField(label="Email, employee ID or KL ID of someone on the portal", max_length=254)
    name = forms.CharField(label="Or, for someone new: their name", max_length=150, required=False)

    def clean(self):
        cleaned = super().clean()
        who = (cleaned.get("who") or "").strip()
        person = (
            Person.objects.filter(email__iexact=who).first() or Person.objects.filter(kl_email__iexact=who).first()
            or Person.objects.filter(employee_id__iexact=who).first() or Person.objects.filter(student_id__iexact=who).first()
        ) if who else None
        if not person:
            if "@" in who and cleaned.get("name"):
                person = Person(email=who, full_name=cleaned["name"], source=Person.Source.IMPORT)
                person.set_unusable_password()
            else:
                raise ValidationError("Nobody matches. To add someone new, give their email and their name.")
        cleaned["person"] = person
        return cleaned


def _admin_only(request):
    if not is_admin(request.user):
        raise PermissionDenied


@never_cache
@login_required
def supervisors(request, slug):
    event = overseen_event(request, slug)
    admin = is_admin(request.user)
    rows = [
        ([s.person.display_name, s.get_kind_display(), "Yes" if s.is_active else "No", ", ".join(s.switch_labels) or "only look"],
         _url(event, "supervisor_edit", s.pk) if admin else None)
        for s in Supervisor.objects.filter(event=event).select_related("person")
    ]
    return _list(request, event, "supervisors", "Supervisors", ["Name", "Type", "Active", "Can"], rows,
                 add_url=_url(event, "supervisor_add") if admin else None, add_label="Add a supervisor",
                 intro="Only admins add supervisors or change what they can do.")


@never_cache
@login_required
@friendly_clash
def supervisor_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    _admin_only(request)
    row = get_object_or_404(Supervisor, pk=pk, event=event) if pk else None
    find = None if row else _style(FindPersonForm(request.POST or None, prefix="p"))
    form = _style(SupervisorForm(request.POST or None, instance=row, prefix="s"))
    for name, (_, help_text, _) in PERMISSIONS.items():
        form.fields[name].help_text = help_text
    if request.method == "POST" and form.is_valid() and (find is None or find.is_valid()):
        with transaction.atomic():
            if row is None:
                person = find.cleaned_data["person"]
                if not person.pk:
                    person.save()
                row, _ = services.add_supervisor(event, person, form.cleaned_data["kind"])
            for name in ["kind", "is_active", *PERMISSIONS]:
                setattr(row, name, form.cleaned_data[name])
            row.save()
            services.add_supervisor(event, row.person, row.kind)  # keeps the Supervisor (and Faculty) role in step
        messages.success(request, f"Saved {row.person.display_name}.")
        return redirect("hackathon_manage_supervisors", slug)
    forms_ = [find, form] if find else [form]
    return _form(request, event, "supervisors", f"Edit {row.person.display_name}" if row else "Add a supervisor", forms_,
                 back=_url(event, "supervisors"), delete_url=_url(event, "supervisor_remove", row.pk) if row else None,
                 delete_label="Remove as supervisor")


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
    return redirect("hackathon_manage_supervisors", slug)


# --- Rounds and rubric -----------------------------------------------------------------------


class RoundForm(forms.ModelForm):
    class Meta:
        model = Round
        fields = ["name", "position", "opens_at", "closes_at"]
        widgets = {
            "opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.event = event

    def clean(self):
        cleaned = super().clean()
        opens, closes = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens and closes and closes <= opens:
            self.add_error("closes_at", "Must be after it opens.")
        position = cleaned.get("position")
        if position and Round.objects.filter(event=self.instance.event, position=position).exclude(pk=self.instance.pk).exists():
            self.add_error("position", "Another round already has this number.")
        return cleaned


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
        fields = ["position", "label", "description", "max_marks"]

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
                 add_url=_url(event, "round_add") if can_edit else None, add_label="Add a round")


@never_cache
@login_required
def round_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_rounds")
    round_ = get_object_or_404(Round, pk=pk, event=event) if pk else None
    form = _style(RoundForm(request.POST or None, instance=round_, event=event,
                            initial=None if round_ else {"position": (event.rounds.count() + 1)}))
    formset = CriterionFormSet(request.POST or None, instance=round_ or Round(event=event), prefix="c")
    for f in formset.forms:
        _style(f)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                saved = form.save()
                formset.instance = saved
                formset.save()
        except IntegrityError:
            form.add_error("position", "Another round already has this number.")
        else:
            messages.success(request, f"Saved {saved.name}.")
            return redirect("hackathon_manage_rounds", slug)
    return _form(request, event, "rounds", f"Edit {round_.name}" if round_ else "Add a round", [form], back=_url(event, "rounds"),
                 delete_url=_url(event, "round_delete", round_.pk) if round_ else None, extra_template="hackathon/_criteria.html",
                 formset=formset)


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
    return _list(request, event, "allocations", "Allocations", ["Round", "Room", "Faculty", "Team", "How", "Marks"], rows,
                 intro="Allocate whole rooms on the hackathon page. Remove single allocations here.")


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


# --- Volunteers (admins only) ----------------------------------------------------------------


class VolunteerForm(forms.ModelForm):
    class Meta:
        model = Volunteer
        fields = ["is_active", *VOLUNTEER_PERMISSIONS]


@never_cache
@login_required
def volunteers(request, slug):
    event = overseen_event(request, slug)
    admin = is_admin(request.user)
    rows = [
        ([v.person.display_name, v.person.email or "", v.person.login_id or "", "Yes" if v.is_active else "No", ", ".join(v.switch_labels) or "nothing yet"],
         _url(event, "volunteer_edit", v.pk) if admin else None)
        for v in Volunteer.objects.filter(event=event).select_related("person")
    ]
    return _list(request, event, "volunteers", "Volunteers", ["Name", "Email", "Login ID", "Active", "Can"], rows,
                 add_url=_url(event, "volunteer_add") if admin else None, add_label="Add a volunteer",
                 intro="Volunteers take attendance and see which marks are submitted, never the marks. Only admins add them and choose what they can do. "
                       "Make their temporary passwords with the logins on the hackathon page.")


@never_cache
@login_required
@friendly_clash
def volunteer_edit(request, slug, pk=None):
    event = overseen_event(request, slug)
    _admin_only(request)
    row = get_object_or_404(Volunteer, pk=pk, event=event) if pk else None
    find = None if row else _style(FindPersonForm(request.POST or None, prefix="p"))
    form = _style(VolunteerForm(request.POST or None, instance=row, prefix="v"))
    for name, (_, help_text, _) in VOLUNTEER_PERMISSIONS.items():
        form.fields[name].help_text = help_text
    if request.method == "POST" and form.is_valid() and (find is None or find.is_valid()):
        with transaction.atomic():
            if row is None:
                person = find.cleaned_data["person"]
                if not person.pk:
                    person.save()
                row, _ = services.add_volunteer(event, person)
            for name in ["is_active", *VOLUNTEER_PERMISSIONS]:
                setattr(row, name, form.cleaned_data[name])
            row.save()
        messages.success(request, f"Saved {row.person.display_name}.")
        return redirect("hackathon_manage_volunteers", slug)
    return _form(request, event, "volunteers", f"Edit {row.person.display_name}" if row else "Add a volunteer",
                 [find, form] if find else [form], back=_url(event, "volunteers"),
                 delete_url=_url(event, "volunteer_remove", row.pk) if row else None, delete_label="Remove as volunteer")


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
    return redirect("hackathon_manage_volunteers", slug)
