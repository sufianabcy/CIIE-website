from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from registrations.models import Registration

from . import services
from .access import PERMISSIONS, allowed, is_admin, overseen_event, overseen_events, permissions_for, require
from .exports import TEMPLATES, credentials_file, template_file
from .forms import AllocateForm, CredentialsForm, MarksForm, MoveTeamForm, UploadForm
from .models import Assignment, EventFaculty, Room, Round, Supervisor, Team, TeamMember

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _download(filename, content):
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --- Admins, coordinators and supervisors ---------------------------------------


@never_cache
@login_required
def dashboard(request, slug):
    event = overseen_event(request, slug)
    rounds = list(Round.objects.filter(event=event).prefetch_related("criteria"))
    rooms = list(Room.objects.filter(event=event).annotate(team_count=Count("teams")))
    allocations = (
        Assignment.objects.filter(round__event=event)
        .select_related("faculty__person", "team__room", "round")
        .order_by("faculty__person__full_name")
    )
    grid = defaultdict(lambda: defaultdict(dict))  # round -> room -> {faculty: [done, total]}
    for a in allocations:
        cell = grid[a.round_id][a.team.room_id].setdefault(a.faculty, [0, 0])
        cell[1] += 1
        cell[0] += bool(a.submitted_at)
    round_rows = [
        (r, [(room, sorted(grid[r.pk][room.pk].items(), key=lambda kv: kv[0].person.display_name)) for room in rooms],
         sorted(grid[r.pk][None].items(), key=lambda kv: kv[0].person.display_name))
        for r in rounds
    ]
    confirmed = Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
    teams = (
        Team.objects.filter(event=event).select_related("room", "mentor")
        .annotate(size=Count("members", distinct=True)).order_by("room__name", "name")
    )
    default_expiry = timezone.localtime(max(event.ends_at, timezone.now()) + timedelta(days=2))
    return render(
        request,
        "hackathon/dashboard.html",
        {
            "event": event,
            "is_admin": is_admin(request.user),
            "can": permissions_for(request.user, event),
            "rounds": round_rows,
            "rooms": rooms,
            "teams": teams,
            "faculty": EventFaculty.objects.filter(event=event).select_related("person").annotate(
                total=Count("assignments"), done=Count("assignments", filter=Q(assignments__submitted_at__isnull=False))
            ),
            "supervisors": Supervisor.objects.filter(event=event).select_related("person"),
            "students": confirmed.count(),
            "without_team": confirmed.filter(team_membership__isnull=True).count(),
            "teams_without_room": teams.filter(room__isnull=True).count(),
            "allocated": allocations.count(),
            "submitted": allocations.filter(submitted_at__isnull=False).count(),
            "warnings": services.room_rule_warnings(event) + services.mentor_conflicts(event),
            "upload_form": UploadForm(),
            "allocate_form": AllocateForm(event=event),
            "pending_requests": _pending_requests(request.user, event),
            "credentials_form": CredentialsForm(initial={"expires_at": default_expiry.strftime("%Y-%m-%dT%H:%M")}),
        },
    )


def _pending_requests(user, event) -> int:
    from .models import FacultyRequest

    pending = FacultyRequest.objects.filter(status=FacultyRequest.Status.PENDING)
    if is_admin(user):
        return pending.count()
    return pending.filter(event=event).count() if allowed(user, event, "can_approve_requests") else 0


UPLOADS = {
    "students": ("Students", "student(s)"),
    "faculty": ("Faculty", "faculty"),
    "supervisors": ("Supervisors", "supervisor(s)"),
    "allocations": ("Allocations", "allocation(s)"),
}


@login_required
@require_POST
def upload(request, slug, kind):
    event = overseen_event(request, slug)
    if kind not in UPLOADS:
        raise Http404
    if kind == "supervisors" and not is_admin(request.user):
        raise PermissionDenied  # only admins add supervisors
    require(request.user, event, "can_import")
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors.get("file", ["Choose a file."])))
        return redirect("hackathon_dashboard", slug)
    upload_file = form.cleaned_data["file"]
    try:
        if kind == "students":
            report = services.import_students(event, upload_file, by=request.user)
        elif kind == "faculty":
            report = services.import_staff(event, upload_file, "faculty")
        elif kind == "supervisors":
            report = services.import_staff(event, upload_file, "supervisor")
        else:
            report = services.import_allocations(event, upload_file)
    except services.ImportFileError as exc:
        messages.error(request, str(exc))
        return redirect("hackathon_dashboard", slug)
    title, noun = UPLOADS[kind]
    return render(
        request, "hackathon/import_result.html",
        {"event": event, "title": title, "summary": report.summary(noun), "report": report},
    )


@login_required
def template(request, slug, kind):
    overseen_event(request, slug)
    if kind not in TEMPLATES:
        raise Http404
    return _download(*template_file(kind))


@login_required
@require_POST
def allocate(request, slug):
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_allocations")
    form = AllocateForm(request.POST, event=event)
    if not form.is_valid():
        messages.error(request, "Choose a round, a faculty member and a room.")
        return redirect("hackathon_dashboard", slug)
    round_, faculty, room = (form.cleaned_data[k] for k in ("round", "faculty", "room"))
    if request.POST.get("action") == "remove":
        removed, kept = services.unallocate_room(round_, faculty, room)
        messages.success(request, f"Removed {faculty} from {room.name} in {round_} ({removed} team(s)).")
        if kept:
            messages.warning(request, f"{kept} allocation(s) already have marks, so they were kept. An admin can remove them.")
        return redirect("hackathon_dashboard", slug)
    try:
        made, notes = services.allocate_room(round_, faculty, room)
    except (services.AllocationError, ValidationError) as exc:
        messages.error(request, " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return redirect("hackathon_dashboard", slug)
    messages.success(request, f"{faculty} judges {room.name} in {round_}: {made} new team(s).")
    for note in notes:
        messages.warning(request, note)
    return redirect("hackathon_dashboard", slug)


@login_required
@require_POST
def round_action(request, slug, pk, action):
    event = overseen_event(request, slug)
    round_ = get_object_or_404(Round, pk=pk, event=event)
    require(request.user, event, "can_publish" if action in ("publish", "unpublish") else "can_edit_rounds")
    if action == "open":
        services.open_round_now(round_)
        messages.success(request, f"{round_} is open for marking now.")
    elif action == "close":
        services.close_round_now(round_)
        messages.success(request, f"{round_} is closed. Faculty can no longer change marks.")
    elif action in ("publish", "unpublish"):
        round_.results_published_at = timezone.now() if action == "publish" else None
        round_.save(update_fields=["results_published_at"])
        messages.success(request, f"Results for {round_} {'published: each team sees its own marks' if action == 'publish' else 'hidden again'}.")
    else:
        raise Http404
    return redirect("hackathon_dashboard", slug)


@login_required
@require_POST
def move_team(request, slug, pk):
    event = overseen_event(request, slug)
    team = get_object_or_404(Team, pk=pk, event=event)
    require(request.user, event, "can_edit_teams")
    form = MoveTeamForm(request.POST, event=event)
    if form.is_valid():
        room = form.cleaned_data["room"]
        warnings = services.set_team_room(team, room)
        messages.success(request, f"{team.name} is now in {room.name if room else 'no room'}.")
        for w in warnings:
            messages.warning(request, w)
    return redirect("hackathon_dashboard", slug)


@login_required
@require_POST
def solo_teams(request, slug):
    """Registrations with no team (online sign-ups) become teams of one."""
    event = overseen_event(request, slug)
    require(request.user, event, "can_edit_teams")
    made = 0
    for registration in Registration.objects.filter(
        event=event, status=Registration.Status.CONFIRMED, team_membership__isnull=True
    ).select_related("person"):
        name = registration.person.display_name
        team = Team.objects.filter(event=event, name__iexact=name).first() or Team.objects.create(event=event, name=name)
        TeamMember.objects.create(team=team, registration=registration)
        made += 1
    messages.success(request, f"{made} student(s) put in teams of one.")
    return redirect("hackathon_dashboard", slug)


@login_required
@require_POST
def logins(request, slug):
    event = overseen_event(request, slug)
    # A new password for someone is a way into their account: admins, or supervisors given the switch
    # (and then only for this hackathon's plain faculty).
    require(request.user, event, "can_make_logins")
    form = CredentialsForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose who gets passwords and when they stop working.")
        return redirect("hackathon_dashboard", slug)
    if form.cleaned_data["expires_at"] <= timezone.now():
        messages.error(request, "Choose an expiry time in the future.")
        return redirect("hackathon_dashboard", slug)
    issued = services.issue_credentials(
        event, form.cleaned_data["expires_at"], form.cleaned_data["which"], faculty_only=not is_admin(request.user)
    )
    if not issued:
        messages.info(request, "Everyone already has a password. Choose \"Everyone\" to make new temporary ones.")
        return redirect("hackathon_dashboard", slug)
    return _download(*credentials_file(event, issued, request.user))


@never_cache
@login_required
def marks(request, slug):
    """Every mark from every faculty member, read-only (admins change marks in the admin)."""
    event = overseen_event(request, slug)
    require(request.user, event, "can_view_marks")
    rounds = []
    for round_ in Round.objects.filter(event=event).prefetch_related("criteria"):
        criteria = list(round_.criteria.all())
        rows = []
        for a in (
            Assignment.objects.filter(round=round_)
            .select_related("faculty__person", "team__room")
            .prefetch_related("scores")
            .order_by("team__room__name", "team__name", "faculty__person__full_name")
        ):
            given = {s.criterion_id: s.marks for s in a.scores.all()}
            rows.append((a, [given.get(c.pk) for c in criteria], sum(given.values(), Decimal("0")) if given else None))
        rounds.append((round_, criteria, rows))
    return render(
        request, "hackathon/marks.html",
        {"event": event, "rounds": rounds, "can": permissions_for(request.user, event)},
    )


# --- Faculty (and the landing page for supervisors) ----------------------------


@never_cache
@login_required
def staff_home(request):
    roles = (
        EventFaculty.objects.filter(person=request.user, is_active=True)
        .select_related("event").order_by("-event__starts_at")
    )
    return render(
        request, "hackathon/staff_home.html",
        {"roles": roles, "overseen": overseen_events(request.user).order_by("-starts_at")},
    )


@never_cache
@login_required
def judge_event(request, slug):
    role = get_object_or_404(EventFaculty, event__slug=slug, person=request.user, is_active=True)
    rounds = []
    for round_ in Round.objects.filter(event=role.event).prefetch_related("criteria"):
        mine = list(
            role.assignments.filter(round=round_)
            .select_related("team__room", "team__mentor")
            .prefetch_related("team__members__registration__person", "scores")
            .order_by("team__name")
        )
        if mine:
            rooms = sorted({a.team.room.name for a in mine if a.team.room})
            rounds.append((round_, rooms, mine, sum(1 for a in mine if a.submitted_at)))
    return render(request, "hackathon/judge_event.html", {"role": role, "event": role.event, "rounds": rounds})


@never_cache
@login_required
def mark(request, pk):
    # Only the faculty member's own allocation: anyone else's team is a 404.
    assignment = get_object_or_404(
        Assignment.objects.select_related("round__event", "team__room", "team__mentor", "faculty"),
        pk=pk, faculty__person=request.user, faculty__is_active=True,
    )
    round_ = assignment.round
    criteria = round_.criteria.all()
    scores = {s.criterion_id: s.marks for s in assignment.scores.all()}
    form = MarksForm(request.POST or None, criteria=criteria, scores=scores, assignment=assignment)
    is_open = round_.is_open()
    if request.method == "POST" and form.is_valid():
        submitting = "submit" in request.POST
        if not submitting or form.require_all():
            try:
                services.save_marks(assignment, form.marks(), form.cleaned_data["remarks"], submitting)
            except services.MarkingClosed as exc:
                messages.error(request, str(exc))
                return redirect("hackathon_mark", assignment.pk)
            if submitting:
                messages.success(request, f"Marks for {assignment.team.name} submitted.")
                return redirect("hackathon_judge_event", round_.event.slug)
            messages.success(request, "Saved. You can change these until you submit or the round closes.")
            return redirect("hackathon_mark", assignment.pk)
    members = [m.registration.person for m in assignment.team.members.select_related("registration__person")]
    return render(
        request, "hackathon/mark.html",
        {
            "assignment": assignment, "event": round_.event, "round": round_, "team": assignment.team,
            "members": members, "form": form, "is_open": is_open, "out_of": round_.out_of,
            "total": sum(scores.values()) if scores else None,
            "editable": is_open and not assignment.is_submitted,
        },
    )


# --- Students ---------------------------------------------------------------------


def team_marks(team, round_):
    """A team's marks for one round: per criterion, the average of submitted marks. Only for published rounds."""
    criteria = list(round_.criteria.all())
    submitted = list(
        Assignment.objects.filter(round=round_, team=team, submitted_at__isnull=False).prefetch_related("scores")
    )
    if not submitted:
        return None
    rows = []
    for c in criteria:
        given = [s.marks for a in submitted for s in a.scores.all() if s.criterion_id == c.pk]
        rows.append((c, (sum(given, Decimal("0")) / len(given)).quantize(Decimal("0.01")) if given else None))
    totals = [a.total() for a in submitted]
    return {"rows": rows, "total": (sum(totals, Decimal("0")) / len(totals)).quantize(Decimal("0.01")), "judges": len(submitted)}


@never_cache
@login_required
def my_hackathon(request, slug):
    """A student's own hackathon page. Never shows another team's data."""
    registration = get_object_or_404(
        Registration.objects.select_related("event", "person"),
        event__slug=slug, event__kind="hackathon", person=request.user, status=Registration.Status.CONFIRMED,
    )
    event = registration.event
    membership = TeamMember.objects.filter(registration=registration).select_related("team__room", "team__mentor").first()
    team = membership.team if membership else None
    members = (
        [m.registration.person for m in team.members.select_related("registration__person").order_by("registration__person__full_name")]
        if team else [request.user]
    )
    rounds = []
    for round_ in Round.objects.filter(event=event).prefetch_related("criteria"):
        jury = []
        if team and event.show_jury_to_students:
            jury = [
                a.faculty.person.display_name
                for a in Assignment.objects.filter(round=round_, team=team).select_related("faculty__person").order_by("faculty__person__full_name")
            ]
        marks = team_marks(team, round_) if team and round_.results_published_at else None
        rounds.append({"round": round_, "jury": jury, "marks": marks})
    return render(
        request, "hackathon/my_hackathon.html",
        {"event": event, "registration": registration, "person": request.user, "team": team, "members": members, "rounds": rounds},
    )


# --- Faculty self-registration ------------------------------------------------------


@never_cache
@login_required
def faculty_register(request):
    from .forms import FacultyRequestForm
    from .models import FacultyRequest

    person = request.user
    if person.is_faculty:
        messages.info(request, "You're already faculty on the portal.")
        return redirect("staff_home")
    pending = FacultyRequest.objects.filter(person=person, status=FacultyRequest.Status.PENDING).first()
    last = pending or FacultyRequest.objects.filter(person=person).first()
    form = None
    if not pending:
        form = FacultyRequestForm(request.POST or None, initial={"full_name": person.full_name, "phone": person.phone, "department": person.branch})
        if request.method == "POST" and form.is_valid():
            try:
                services.request_faculty(person, form.cleaned_data)
            except services.RequestError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Request sent. An admin will check it; you'll get an email either way.")
            return redirect("faculty_register")
    return render(request, "hackathon/faculty_register.html", {"form": form, "pending": pending, "last": last})


@never_cache
@login_required
def faculty_requests(request):
    from .forms import DecisionForm
    from .models import FacultyRequest

    user = request.user
    visible = FacultyRequest.objects.all()
    if not is_admin(user):
        events = [e.pk for e in overseen_events(user) if allowed(user, e, "can_approve_requests")]
        if not events:
            raise PermissionDenied
        visible = visible.filter(event__in=events)
    if request.method == "POST":
        req = get_object_or_404(visible, pk=request.POST.get("request"))
        if not is_admin(user) and not (req.event and allowed(user, req.event, "can_approve_requests")):
            raise PermissionDenied
        form = DecisionForm(request.POST)
        note = form.cleaned_data["note"] if form.is_valid() else ""
        try:
            if request.POST.get("decision") == "approve":
                services.approve_faculty_request(req, request.user, note, allow_duplicates=request.POST.get("anyway") == "1")
                messages.success(request, f"{req.full_name} is now faculty. They've been emailed.")
            elif request.POST.get("decision") == "reject":
                services.reject_faculty_request(req, request.user, note)
                messages.success(request, f"{req.full_name}'s request was rejected. They've been emailed.")
        except services.RequestError as exc:
            messages.error(request, str(exc))
        return redirect("faculty_requests")
    pending = list(visible.filter(status=FacultyRequest.Status.PENDING).select_related("person", "event"))
    rows = [(r, list(r.duplicates())) for r in pending]
    decided = visible.exclude(status=FacultyRequest.Status.PENDING).select_related("person", "event", "decided_by")[:30]
    return render(request, "hackathon/faculty_requests.html", {"rows": rows, "decided": decided, "decision_form": DecisionForm()})



# --- Admins and permitted supervisors: changing marks, jury switch, adding faculty ----


@never_cache
@login_required
def change_marks(request, slug, pk):
    """Change one allocation's marks, or reopen it, with a recorded reason."""
    from .forms import ChangeMarksForm

    event = overseen_event(request, slug)
    assignment = get_object_or_404(
        Assignment.objects.select_related("round", "faculty__person", "team__room"), pk=pk, round__event=event
    )
    can_change = allowed(request.user, event, "can_change_marks")
    can_reopen = allowed(request.user, event, "can_reopen_marks")
    if not (can_change or can_reopen):
        raise PermissionDenied
    criteria = list(assignment.round.criteria.all())
    scores = {s.criterion_id: s.marks for s in assignment.scores.all()}
    form = ChangeMarksForm(request.POST or None, criteria=criteria, scores=scores, assignment=assignment)
    if request.method == "POST":
        reopening = request.POST.get("action") == "reopen"
        require(request.user, event, "can_reopen_marks" if reopening else "can_change_marks")
        if form.is_valid():
            try:
                if reopening:
                    services.reopen_marks(assignment, request.user, form.cleaned_data["reason"])
                    messages.success(request, f"Reopened: {assignment.faculty} can change these marks again while the round is open.")
                else:
                    services.change_marks(assignment, request.user, form.marks(), form.cleaned_data["reason"], form.cleaned_data["remarks"])
                    messages.success(request, "Marks changed. The reason is recorded and appears in the marks Excel.")
                return redirect("hackathon_marks", slug)
            except ValidationError as exc:
                form.add_error(None, exc)
    return render(
        request, "hackathon/change_marks.html",
        {"event": event, "assignment": assignment, "form": form, "can_change": can_change, "can_reopen": can_reopen,
         "history": assignment.changes.select_related("changed_by")},
    )


@login_required
@require_POST
def jury_switch(request, slug):
    event = overseen_event(request, slug)
    require(request.user, event, "can_publish")
    event.show_jury_to_students = request.POST.get("show") == "1"
    event.save(update_fields=["show_jury_to_students"])
    messages.success(request, "Students now see their jury." if event.show_jury_to_students else "The jury is hidden from students.")
    return redirect("hackathon_dashboard", slug)


@login_required
@require_POST
def add_faculty(request, slug):
    from people.models import Person

    event = overseen_event(request, slug)
    require(request.user, event, "can_add_faculty")
    text = request.POST.get("who", "").strip()
    person = None
    if text:
        person = (
            Person.objects.filter(email__iexact=text).first()
            or Person.objects.filter(kl_email__iexact=text).first()
            or Person.objects.filter(employee_id__iexact=text).first()
            or Person.objects.filter(login_id__iexact=text).first()
        )
    if not person:
        messages.error(request, f"Nobody on the portal matches \"{text}\". Upload them in the faculty Excel, or ask them to register as faculty.")
    else:
        _, created = services.add_faculty(event, person)
        messages.success(request, f"{person.display_name} {'added to' if created else 'is already on'} this hackathon's faculty.")
    return redirect("hackathon_dashboard", slug)
