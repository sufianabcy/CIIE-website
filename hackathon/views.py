from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from registrations.models import Registration

from . import imports, services
from .day_views import RandomForm
from .access import allowed, is_admin, overseen_event, overseen_events, permissions_for, require
from .exports import TEMPLATES, credentials_file, template_file
from .forms import AllocateForm, CredentialsForm, MarksForm, MoveTeamForm, UploadForm
from .models import Assignment, EventFaculty, Room, Round, RoundOutcome, Supervisor, Team, TeamMember

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
            "random_form": RandomForm(event=event),
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
    "volunteers": ("Volunteers", "volunteer(s)"),
    "helpers": ("Supervisors and volunteers", "person(s)"),
    "allocations": ("Allocations", "allocation(s)"),
    "rooms": ("Rooms", "room(s)"),
    "teams": ("Teams", "team(s)"),
}


@login_required
@require_POST
def upload(request, slug, kind):
    """Step 1: keep the uploaded file, then go to the column-matching step."""
    from .models import ImportUpload

    event = overseen_event(request, slug)
    if kind not in UPLOADS:
        raise Http404
    _import_allowed(request, event, kind)
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors.get("file", ["Choose a file."])))
        return redirect(request.META.get("HTTP_REFERER") or reverse_lazy_dashboard(slug))
    content = form.cleaned_data["file"].read()
    try:
        imports.sheets(content)
    except services.ImportFileError as exc:
        messages.error(request, str(exc))
        return redirect("hackathon_dashboard", slug)
    item = ImportUpload.objects.create(event=event, kind=kind, user=request.user, filename=form.cleaned_data["file"].name, content=content)
    return redirect("hackathon_import", slug, item.pk)


def reverse_lazy_dashboard(slug):
    from django.urls import reverse

    return reverse("hackathon_dashboard", args=[slug])


def _import_allowed(request, event, kind):
    if kind in ("supervisors", "volunteers", "helpers") and not is_admin(request.user):
        raise PermissionDenied  # only admins add supervisors and volunteers
    require(request.user, event, "can_import")


@never_cache
@login_required
def import_start(request, slug):
    """"Import from Excel": choose what the file holds, then upload it."""
    event = overseen_event(request, slug)
    require(request.user, event, "can_import")
    if request.method == "POST":
        if request.POST.get("kind") not in UPLOADS:
            raise Http404
        return upload(request, slug, request.POST["kind"])
    kinds = [(k, label) for k, label in imports.KINDS.items() if k in UPLOADS and (k != "helpers" or is_admin(request.user))]
    return render(request, "hackathon/import_start.html", {
        "event": event, "kinds": kinds, "chosen": request.GET.get("kind", "students"), "upload_form": UploadForm(),
    })


def _new_field(event, kind, header):
    """"+ New field": a short-text custom field named after the column's header."""
    from django.utils.text import slugify

    from .models import FieldDefinition

    applies = imports.NEW_FIELD_FOR[kind]
    label = header or "New field"
    existing = FieldDefinition.objects.filter(event=event, applies_to=applies, label__iexact=label).first()
    if existing:
        return existing
    base = slugify(label)[:40] or "field"
    key, n = base, 2
    while FieldDefinition.objects.filter(event=event, applies_to=applies, key=key).exists():
        key, n = f"{base}-{n}", n + 1
    last = FieldDefinition.objects.filter(event=event, applies_to=applies).order_by("-position").first()
    return FieldDefinition.objects.create(event=event, applies_to=applies, key=key, label=label, position=(last.position + 1 if last else 20))


@never_cache
@login_required
def import_step(request, slug, pk):
    """Steps 2 to 4: say what each Excel column is, check every row, then import the valid rows."""
    from openpyxl.utils import get_column_letter

    from .models import ImportUpload

    event = overseen_event(request, slug)
    item = get_object_or_404(ImportUpload, pk=pk, event=event)
    _import_allowed(request, event, item.kind)
    title, noun = UPLOADS[item.kind]
    if item.imported_at:
        return render(request, "hackathon/import_steps.html", {"event": event, "item": item, "title": title, "step": 4, "result": item.result})
    action = request.POST.get("action") if request.method == "POST" else None
    if action == "sheet":
        item.sheet_name = request.POST.get("sheet", "")
        raw = request.POST.get("header_row", "")
        item.header_row = int(raw) if raw.isdigit() else 0
        item.mapping = {}
        item.save(update_fields=["sheet_name", "header_row", "mapping"])
        return redirect("hackathon_import", slug, item.pk)
    headers, rows, numbers = imports.read_item(item)
    can_fields = allowed(request.user, event, "can_manage_fields") and item.kind in imports.NEW_FIELD_FOR
    targets_ = imports.targets(event, item.kind, request.user)
    mapping = item.mapping or imports.auto_match(headers, targets_, imports.saved_mapping(event, item.kind))
    mapping = {t.key: mapping.get(t.key) for t in targets_}
    twice = []
    if request.method == "POST" and any(k.startswith("col_") for k in request.POST):
        chosen = {}
        for i, header in enumerate(headers):
            value = request.POST.get(f"col_{i}", "")
            if value == "__new__" and can_fields:
                value = f"f:{_new_field(event, item.kind, header).key}"
            if value:
                chosen.setdefault(value, []).append(i)
        targets_ = imports.targets(event, item.kind, request.user)  # may include a field just added
        known = {t.key: t for t in targets_}
        mapping = {t.key: None for t in targets_}
        for key, cols in chosen.items():
            if key not in known:
                continue
            mapping[key] = cols[0]
            if len(cols) > 1:
                letters = " and ".join(get_column_letter(c + 1) for c in cols)
                twice.append(f"Columns {letters} are both used as \"{known[key].label}\". Use each field once.")
        item.mapping = mapping
        item.save(update_fields=["mapping"])
    elif request.method == "POST" and any(k.startswith("map_") for k in request.POST):  # field-first form, still accepted
        for t in targets_:
            raw = request.POST.get(f"map_{t.key}", "")
            mapping[t.key] = int(raw) if raw.isdigit() and int(raw) < len(headers) else None
        item.mapping = mapping
        item.save(update_fields=["mapping"])
    if request.method == "POST" and any(k.startswith("const_key_") for k in request.POST):
        known = {t.key for t in targets_}
        constants = {}
        for i in range(3):
            key, value = request.POST.get(f"const_key_{i}", ""), request.POST.get(f"const_val_{i}", "").strip()
            if key in known and value and mapping.get(key) is None:
                constants[key] = value
        item.constants = constants
        item.save(update_fields=["constants"])
    blank_choices = {key for key, _, _ in imports.BLANK_TEAM_CHOICES}
    if request.method == "POST" and request.POST.get("blank_teams") in blank_choices and item.kind == "students":
        item.blank_teams = request.POST["blank_teams"]
        item.save(update_fields=["blank_teams"])
    c_headers, c_rows, c_mapping = imports.with_constants(headers, rows, mapping, item.constants)
    blank_rows = imports.blank_team_rows(c_headers, c_rows, c_mapping, numbers) if item.kind == "students" else []
    c_rows = imports.fill_blank_teams(c_rows, c_mapping, item.blank_teams)
    errors, warnings = imports.sheet_checks(c_headers, c_rows, c_mapping, targets_, item.kind)
    errors = twice + errors
    problems, step, new_people = None, 2, 0
    if action in ("check", "import") and not errors:
        found, new_people = imports.row_checks(event, item.kind, c_headers, c_rows, c_mapping, targets_, want_new=True,
                                               blank_teams=item.blank_teams)
        problems = {numbers[line - 2]: imports.real_rows(msgs, numbers) for line, msgs in found.items() if 0 <= line - 2 < len(numbers)}
        step = 3
        if action == "import":
            valid = {line for line in range(2, len(rows) + 2) if line not in found}
            with transaction.atomic():
                report = imports.run_import(item, mapping, valid, request.user)
                emailed = 0
                if request.POST.get("email_new") == "1":
                    from registrations.models import Registration as Reg
                    from registrations.notify import notify_added

                    for registration in Reg.objects.filter(pk__in=set(report.added)).select_related("person", "event"):
                        emailed += notify_added(registration)
            skipped = {str(line): msgs for line, msgs in problems.items()}
            for message in imports.real_rows(report.problems, numbers):  # anything refused at the last moment
                line = message.split(":")[0].split(",")[0].replace("Row ", "")
                if line.isdigit():
                    skipped.setdefault(line, []).append(message)
            item.result = {"summary": report.summary(noun), "added": report.created, "updated": report.updated, "emailed": emailed,
                           "skipped": skipped, "warnings": imports.real_rows(report.warnings, numbers)}
            item.imported_at = timezone.now()
            item.save(update_fields=["result", "imported_at"])
            imports.remember(event, item.kind, headers, {k: v for k, v in mapping.items() if v is not None and v < len(headers)})
            return redirect("hackathon_import", slug, item.pk)
    return render(request, "hackathon/import_steps.html", {
        "event": event, "item": item, "title": title, "step": step, "targets": targets_,
        "columns": imports.columns_view(headers, rows, mapping), "sheets": imports.sheets(item.content),
        "unmapped": [t for t in targets_ if mapping.get(t.key) is None],
        "constant_rows": (list(item.constants.items()) + [("", "")] * 3)[:3],
        "errors": errors, "warnings": warnings, "problems": sorted((problems or {}).items()),
        "valid_count": len(rows) - len(problems or {}), "row_count": len(rows), "can_fields": can_fields, "new_people": new_people,
        "can_define_fields": allowed(request.user, event, "can_manage_fields"),
        "blank_rows": blank_rows, "blank_choices": imports.BLANK_TEAM_CHOICES,
    })


@login_required
def import_skipped(request, slug, pk):
    from .models import ImportUpload

    event = overseen_event(request, slug)
    item = get_object_or_404(ImportUpload, pk=pk, event=event, imported_at__isnull=False)
    _import_allowed(request, event, item.kind)
    return _download(f"{event.slug}-{item.kind}-skipped-rows.xlsx", imports.skipped_workbook(item))


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
        from .rooms import move_problems

        room = form.cleaned_data["room"]
        problems = move_problems(team, room) if room and room.pk != team.room_id else []
        if problems:
            for p in problems:
                messages.error(request, f"{team.name} not moved: {p}")
            return redirect("hackathon_dashboard", slug)
        services.set_team_room(team, room)
        messages.success(request, f"{team.name} is now in {room.name if room else 'no room'}.")
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
    from .models import Volunteer

    volunteering = Volunteer.objects.filter(person=request.user, is_active=True).select_related("event").order_by("-event__starts_at")
    return render(
        request, "hackathon/staff_home.html",
        {"roles": roles, "overseen": overseen_events(request.user).order_by("-starts_at"), "volunteering": volunteering},
    )


@never_cache
@login_required
def judge_event(request, slug):
    from . import rubric

    role = get_object_or_404(EventFaculty, event__slug=slug, person=request.user, is_active=True)
    rounds = []
    for round_ in Round.objects.filter(event=role.event).prefetch_related("criteria"):
        ids = rubric.eligible_team_ids(round_)
        mine = list(
            (role.assignments.filter(round=round_) if ids is None else role.assignments.filter(round=round_, team_id__in=ids))
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
    from . import rubric

    round_ = assignment.round
    ids = rubric.eligible_team_ids(round_)
    if ids is not None and assignment.team_id not in ids:
        raise Http404("This team did not advance to this round.")
    criteria = round_.criteria.all()
    scores = {s.criterion_id: s.marks for s in assignment.scores.all()}
    settings_row = rubric.settings_for(round_.event)
    final = not Round.objects.filter(event=round_.event, position__gt=round_.position).exists()
    extras = {
        "prompts": rubric.prompts(round_), "red_flags": settings_row.red_flags, "self_score_max": settings_row.self_score_max,
        "recommendations": settings_row.recommendations if final else [], "allow_override": round_.allow_override,
    }
    form = MarksForm(request.POST or None, criteria=criteria, scores=scores, assignment=assignment, extras=extras)
    is_open = round_.is_open()
    if request.method == "POST" and form.is_valid():
        submitting = "submit" in request.POST
        if not submitting or form.require_all():
            try:
                services.save_marks(assignment, form.marks(), form.cleaned_data["remarks"], submitting, form.extra_values())
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
            "bands": rubric.bands(round_.event),
            "saved_bands": {c.pk: rubric.band_for(round_.event, scores.get(c.pk), c.max_marks) for c in criteria},
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


# What a student sees about their own hackathon. Mentor and jury are hidden; set these to True to show them again.
SHOW_MENTOR_TO_STUDENTS = False
SHOW_JURY_TO_STUDENTS = False


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
        if team and SHOW_JURY_TO_STUDENTS and event.show_jury_to_students:
            jury = [
                a.faculty.person.display_name
                for a in Assignment.objects.filter(round=round_, team=team).select_related("faculty__person").order_by("faculty__person__full_name")
            ]
        released = round_.marks_released()
        marks = team_marks(team, round_) if team and released else None
        allocations = list(Assignment.objects.filter(round=round_, team=team).values_list("submitted_at", flat=True)) if team else []
        # Before release: only whether the marks are in, never numbers.
        status = ("submitted" if all(allocations) else "pending") if allocations else None
        decided = RoundOutcome.objects.filter(round=round_, team=team).first() if team else None
        rounds.append({"round": round_, "jury": jury, "marks": marks, "released": released, "status": status,
                       "outcome": decided, "next": Round.objects.filter(event=event, position__gt=round_.position).order_by("position").first()})
    from .fields import custom_values, display, ensure_fields

    values = custom_values(event, request.user)
    extra_details = [(d.label, display(d, values.get(d.key))) for d in ensure_fields(event, "student") if not d.builtin and values.get(d.key) not in (None, "")]
    return render(
        request, "hackathon/my_hackathon.html",
        {"event": event, "registration": registration, "person": request.user, "team": team, "members": members, "rounds": rounds,
         "extra_details": extra_details, "show_mentor": SHOW_MENTOR_TO_STUDENTS},
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
                with transaction.atomic():
                    services.request_faculty(person, form.cleaned_data)
            except services.RequestError as exc:
                messages.error(request, str(exc))
            except IntegrityError:
                messages.info(request, "Your request is already waiting for an admin.")
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


# --- Sign-in invites (any event) -----------------------------------------------------------------


def _invite_event(request, slug):
    from events.models import Event

    event = get_object_or_404(Event, slug=slug)
    if is_admin(request.user) or (event.is_hackathon and allowed(request.user, event, "can_email_participants")):
        return event
    raise PermissionDenied


def _invite_status(event):
    """{registration id: (attempts, latest status)} from the invite emails in the outbox."""
    from mailer.models import OutboxEmail

    status = {}
    for key, state in OutboxEmail.objects.filter(dedupe_key__startswith=f"invite-{event.pk}-").order_by("created_at").values_list("dedupe_key", "status"):
        _, _, reg_id, attempt = key.split("-")
        status[int(reg_id)] = (max(int(attempt), status.get(int(reg_id), (0, ""))[0]), state)
    return status


@never_cache
@login_required
def invites(request, slug):
    """Preview, then email every participant that they can sign in. Never sends twice without a confirm."""
    import math

    from django.conf import settings as conf

    from mailer.models import OutboxEmail
    from registrations.notify import send_invite

    from .models import Assignment as Alloc

    event = _invite_event(request, slug)
    regs = list(Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED)
                .select_related("person", "team_membership__team__room", "team_membership__team__mentor").order_by("person__full_name"))
    status = _invite_status(event)
    if request.method == "POST":
        action = request.POST.get("action")
        if not request.POST.get("confirm"):
            messages.error(request, "Tick the box to confirm before sending.")
            return redirect("hackathon_invites", slug)
        if action == "send_new":
            targets = [r for r in regs if r.pk not in status]
        elif action == "resend_failed":
            targets = [r for r in regs if status.get(r.pk, (0, ""))[1] == OutboxEmail.Status.FAILED]
        elif action == "resend_chosen":
            chosen = {int(x) for x in request.POST.getlist("chosen") if x.isdigit()}
            targets = [r for r in regs if r.pk in chosen]
        else:
            raise Http404
        sent = sum(send_invite(r, status.get(r.pk, (0, ""))[0] + 1) for r in targets)
        messages.success(request, f"{sent} invite(s) queued. They go out within the sending limits; sign-in codes go first.")
        return redirect("hackathon_invites", slug)
    teams = [getattr(r, "team_membership", None) for r in regs]
    counts = {"students": len(regs), "no_email": sum(1 for r in regs if not r.person.email)}
    if event.is_hackathon:
        counts.update(
            no_team=sum(1 for m in teams if m is None),
            no_room=sum(1 for m in teams if m is not None and m.team.room_id is None),
            no_mentor=sum(1 for m in teams if m is not None and not (m.team.mentor_id or m.team.mentor_name)),
        )
    states = [status[r.pk][1] for r in regs if r.pk in status]
    progress = {"queued": states.count(OutboxEmail.Status.QUEUED) + states.count("sending"), "sent": states.count(OutboxEmail.Status.SENT),
                "failed": states.count(OutboxEmail.Status.FAILED), "never": sum(1 for r in regs if r.pk not in status)}
    rate = max(1, conf.MAIL_RATE_PER_MINUTE)
    return render(request, "hackathon/invites.html", {
        "event": event, "regs": regs, "status": status, "counts": counts, "progress": progress,
        "allocated": event.is_hackathon and Alloc.objects.filter(round__event=event).exists(),
        "rounds_set": event.is_hackathon and event.rounds.exclude(opens_at=None).exists(),
        "minutes": math.ceil(max(progress["never"], 1) / rate), "rate": rate,
        "rows": [(r, status.get(r.pk)) for r in regs],
    })


# --- Advancing teams between rounds -------------------------------------------------------------


@never_cache
@login_required
def advance_teams(request, slug, pk):
    """Admins: every team's score for a round, the advancement rule, and which teams go on. Ticks are pre-filled."""
    from . import rubric
    from .models import RoundOutcome

    event = overseen_event(request, slug)
    if not is_admin(request.user):
        raise PermissionDenied
    round_ = get_object_or_404(Round, pk=pk, event=event)
    teams = list(rubric.eligible_teams(round_).filter(assignments__round=round_).distinct().order_by("name"))
    if request.method == "POST":
        if not request.POST.get("confirm"):
            messages.error(request, "Tick the box to confirm the teams that go on.")
            return redirect("hackathon_advance", slug, pk)
        chosen = {int(x) for x in request.POST.getlist("advance") if x.isdigit()}
        with transaction.atomic():
            for team in teams:
                RoundOutcome.objects.update_or_create(round=round_, team=team, defaults={
                    "advanced": team.pk in chosen, "decided_by": request.user, "decided_at": timezone.now()})
        messages.success(request, f"{len(chosen & {t.pk for t in teams})} of {len(teams)} teams go on from {round_.name}.")
        return redirect("hackathon_advance", slug, pk)
    rows = []
    for team in teams:
        score = rubric.team_score(round_, team)
        meets, why = rubric.meets_rule(round_, score)
        decided = rubric.outcome(round_, team)
        key_band = ""
        if score and round_.key_criterion_id:
            key_band = rubric.band_for(event, score["criteria"].get(round_.key_criterion_id), round_.key_criterion.max_marks)
        rows.append({"team": team, "score": score, "meets": meets, "why": why, "key_band": key_band,
                     "override": bool(score and score["override"]), "decided": decided,
                     "checked": decided.advanced if decided else meets})
    rows.sort(key=lambda r: -(r["score"]["total"] if r["score"] else -1))
    submitted = Assignment.objects.filter(round=round_, submitted_at__isnull=False).count()
    return render(request, "hackathon/advance.html", {
        "event": event, "round": round_, "rows": rows, "next_round": Round.objects.filter(event=event, position__gt=round_.position).first(),
        "submitted": submitted, "total": Assignment.objects.filter(round=round_).count(),
    })
