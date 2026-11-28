"""Attendance sessions: an admin makes a link for a classroom that works for a set number of minutes; the
attendance-takers of that classroom sign in, open it and tick who is there. When the link expires the session is
final: only admins change it (latecomers, corrections), and takers ask for changes with a request.
"""

from collections import defaultdict
from datetime import timedelta
from io import BytesIO

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from openpyxl import Workbook

from events.exports import _about, _local, _sheet
from registrations.models import Registration

from .access import allowed, is_admin, overseen_event, oversees, volunteer_row
from .models import AttendanceRequest, AttendanceSession, Room, SessionMark, TeamMember, Volunteer, team_number_order


class AttendanceError(Exception):
    """An attendance change that can't be made, with the reason to show."""


# --- Rules -------------------------------------------------------------------------------------------


def room_students(room) -> list[tuple]:
    """[(team, [registrations])] of the confirmed students in this classroom's teams, by team number."""
    members = (TeamMember.objects.filter(team__room=room, registration__status=Registration.Status.CONFIRMED)
               .select_related("team", "registration__person"))
    by_team = defaultdict(list)
    for m in members:
        by_team[m.team].append(m.registration)
    return [(team, sorted(regs, key=lambda r: r.person.display_name.lower()))
            for team, regs in sorted(by_team.items(), key=lambda kv: team_number_order(kv[0]))]


def can_take(user, session) -> bool:
    """Admins; supervisors with the attendance switch; active volunteers of this hackathon with the attendance switch.
    A volunteer who has rooms only takes attendance in those; one with no rooms yet, in any classroom whose link
    they are given (each link shows only its own classroom)."""
    if not user.is_authenticated:
        return False
    if is_admin(user):
        return True
    event = session.event
    if oversees(user, event) and allowed(user, event, "can_take_attendance"):
        return True
    row = volunteer_row(user, event)
    if not (row and row.is_active and row.can_team_attendance):
        return False
    rooms = set(row.rooms.values_list("pk", flat=True))
    return not rooms or session.room_id in rooms


def sessions_for_taker(user, event, only_open=True):
    """The sessions this person can open at this hackathon (open ones by default)."""
    sessions = AttendanceSession.objects.filter(event=event).select_related("room")
    if only_open:
        now = timezone.now()
        sessions = sessions.filter(opens_at__lte=now, expires_at__gt=now)
    return [s for s in sessions if can_take(user, s)]


@transaction.atomic
def create_sessions(event, rooms, name, opens_at, minutes, by) -> list[AttendanceSession]:
    if not rooms:
        raise AttendanceError("Choose at least one classroom.")
    if not 1 <= minutes <= 24 * 60:
        raise AttendanceError("A link can be valid for 1 minute to 24 hours.")
    return [AttendanceSession.objects.create(event=event, room=room, name=name, opens_at=opens_at,
                                             expires_at=opens_at + timedelta(minutes=minutes), created_by=by)
            for room in rooms]


def _room_registration(session, registration_id):
    """The student, if they are a confirmed student in a team in this session's classroom (so no proxies from elsewhere)."""
    member = (TeamMember.objects.filter(registration_id=registration_id, team__room=session.room,
                                        registration__status=Registration.Status.CONFIRMED, team__event=session.event)
              .select_related("registration__person", "team").first())
    return member


@transaction.atomic
def take(session, user, present_ids) -> tuple[int, int]:
    """The taker's ticks for the whole classroom: ticked students present, the rest absent. Only while the link is open."""
    session = AttendanceSession.objects.select_for_update().get(pk=session.pk)
    if not session.is_open():
        raise AttendanceError("This link has expired, so the attendance is final. Ask an admin for any change."
                              if session.is_final() else "This link isn't open yet.")
    wanted = {int(x) for x in present_ids}
    students = [r for _, regs in room_students(session.room) for r in regs]
    known = {r.pk for r in students}
    stray = wanted - known
    if stray:
        raise AttendanceError("Some ticked students aren't in this classroom. Reload the page and try again.")
    teams = {m.registration_id: m.team_id for m in TeamMember.objects.filter(registration_id__in=known)}
    now, present = timezone.now(), 0
    for reg in students:
        is_present = reg.pk in wanted
        present += is_present
        SessionMark.objects.update_or_create(session=session, registration=reg, defaults={
            "present": is_present, "team_id": teams.get(reg.pk), "source": SessionMark.Source.LINK,
            "marked_by": user, "marked_at": now})
    return present, len(students)


def admin_mark(session, registration, present, by) -> SessionMark:
    """An admin sets one student's attendance at any time: a latecomer once the link has expired, else a correction."""
    member = _room_registration(session, registration.pk)
    if member is None:
        raise AttendanceError(f"{registration.person.display_name} isn't a student in {session.room.name}.")
    source = SessionMark.Source.LATE if present and session.is_final() else SessionMark.Source.CORRECTION
    mark, _ = SessionMark.objects.update_or_create(session=session, registration=registration, defaults={
        "present": present, "team": member.team, "source": source, "marked_by": by, "marked_at": timezone.now()})
    return mark


def raise_request(session, user, registration_id, kind, want_present, note) -> AttendanceRequest:
    if not can_take(user, session):
        raise PermissionDenied
    member = _room_registration(session, registration_id)
    if member is None:
        raise AttendanceError("Choose a student from this classroom.")
    if kind not in AttendanceRequest.Kind.values:
        raise AttendanceError("Choose what happened.")
    if AttendanceRequest.objects.filter(session=session, registration=member.registration, status=AttendanceRequest.Status.PENDING).exists():
        raise AttendanceError(f"There is already a request for {member.registration.person.display_name} waiting for an admin.")
    return AttendanceRequest.objects.create(session=session, registration=member.registration, kind=kind,
                                            want_present=want_present, note=(note or "").strip()[:500], requested_by=user)


@transaction.atomic
def decide(req, by, approve, note=""):
    if req.status != AttendanceRequest.Status.PENDING:
        raise AttendanceError("This request was already decided.")
    if approve and req.kind in (AttendanceRequest.Kind.LATE, AttendanceRequest.Kind.WRONG):
        admin_mark(req.session, req.registration, req.want_present, by)
    req.status = AttendanceRequest.Status.APPROVED if approve else AttendanceRequest.Status.REJECTED
    req.decided_by, req.decided_at, req.decision_note = by, timezone.now(), (note or "").strip()[:500]
    req.save()
    return req


def status_of(mark) -> str:
    return "Not marked" if mark is None else ("Present" if mark.present else "Absent")


# --- Excel -------------------------------------------------------------------------------------------


def workbook(event, user) -> bytes:
    """Session-wise, student-wise and classroom-wise attendance, every mark with who and when, and the requests."""
    sessions = list(AttendanceSession.objects.filter(event=event).select_related("room", "created_by").order_by("opens_at", "room__name"))
    marks = {(m.session_id, m.registration_id): m for m in
             SessionMark.objects.filter(session__event=event).select_related("marked_by", "registration__person", "team")}
    roster = {s.pk: [(team, reg) for team, regs in room_students(s.room) for reg in regs] for s in sessions}
    wb = Workbook()

    rows = []
    for s in sessions:
        statuses = [status_of(marks.get((s.pk, reg.pk))) for _, reg in roster[s.pk]]
        rows.append([s.name, s.room.name, _local(s.opens_at), _local(s.expires_at), s.state.capitalize(), len(statuses),
                     statuses.count("Present"), statuses.count("Absent"), statuses.count("Not marked"), s.created_by.display_name])
    _sheet(wb, "By session", ["Session", "Classroom", "Opens", "Expires", "State", "Students", "Present", "Absent", "Not marked",
                              "Made by"], rows)

    students = {}
    for s in sessions:
        for team, reg in roster[s.pk]:
            students.setdefault(reg.pk, (team, reg))
    for (sid, rid), m in marks.items():
        if rid not in students:
            students[rid] = (m.team, m.registration)  # moved away since: still listed
    labels = [f"{s.name} · {s.room.name}" for s in sessions]
    rows = []
    for team, reg in sorted(students.values(), key=lambda tr: (team_number_order(tr[0]) if tr[0] else (True,), tr[1].person.display_name.lower())):
        cells = [status_of(marks.get((s.pk, reg.pk))) if (s.pk, reg.pk) in marks or any(r.pk == reg.pk for _, r in roster[s.pk]) else ""
                 for s in sessions]
        held = [c for c in cells if c]
        present = cells.count("Present")
        rows.append([reg.person.display_name, reg.person.student_id, team.number if team else "", team.name if team else "",
                     team.room.name if team and team.room else ""] + cells
                    + [present, len(held), round(present * 100 / len(held)) if held else None])
    _sheet(wb, "By student", ["Name", "KL ID", "Team no", "Team", "Classroom"] + labels + ["Present", "Sessions", "Present %"], rows,
           text_columns=["KL ID", "Team no"])

    by_room = defaultdict(lambda: [0, 0, 0])
    for s in sessions:
        for _, reg in roster[s.pk]:
            status = status_of(marks.get((s.pk, reg.pk)))
            cell = by_room[(s.room.name, s.name, _local(s.opens_at))]
            cell[0] += 1
            cell[1] += status == "Present"
            cell[2] += status == "Absent"
    rows = [[room, name, opens, n, p, a, n - p - a, round(p * 100 / n) if n else None]
            for (room, name, opens), (n, p, a) in sorted(by_room.items(), key=lambda kv: (kv[0][0], kv[0][2] or 0))]
    _sheet(wb, "By classroom", ["Classroom", "Session", "Opens", "Students", "Present", "Absent", "Not marked", "Present %"], rows)

    by_id = {s.pk: s for s in sessions}
    rows = [[by_id[m.session_id].name, by_id[m.session_id].room.name, m.registration.person.display_name, m.registration.person.student_id,
             m.team.number if m.team else "", m.team.name if m.team else "", "Present" if m.present else "Absent",
             m.get_source_display(), m.marked_by.display_name, _local(m.marked_at)] for m in marks.values()]
    rows.sort(key=lambda r: (str(r[0]), str(r[1]), str(r[2])))
    _sheet(wb, "Every mark", ["Session", "Classroom", "Name", "KL ID", "Team no", "Team", "Mark", "How", "Marked by", "Marked at"],
           rows, text_columns=["KL ID", "Team no"])

    reqs = AttendanceRequest.objects.filter(session__event=event).select_related(
        "session__room", "registration__person", "requested_by", "decided_by")
    _sheet(wb, "Requests", ["Session", "Classroom", "Student", "KL ID", "What happened", "Asked for", "Note", "Asked by", "Asked at",
                            "Decision", "Decided by", "Decided at", "Admin note"],
           [[r.session.name, r.session.room.name, r.registration.person.display_name, r.registration.person.student_id,
             r.get_kind_display(), "Present" if r.want_present else "Absent", r.note, r.requested_by.display_name, _local(r.requested_at),
             r.get_status_display(), r.decided_by.display_name if r.decided_by else "", _local(r.decided_at), r.decision_note]
            for r in reqs], text_columns=["KL ID"])
    _about(wb, event, "Attendance (sessions)", user)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- Pages -------------------------------------------------------------------------------------------


class SessionForm(forms.Form):
    name = forms.CharField(label="Session name", max_length=100, initial="Attendance",
                           help_text="For example Morning check or Round 2 start.")
    rooms = forms.ModelMultipleChoiceField(label="Classrooms", queryset=Room.objects.none(), widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
                                           help_text="One link is made for each classroom you tick.")
    opens_at = forms.DateTimeField(label="Opens", required=False, help_text="Empty: now.",
                                   widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"))
    minutes = forms.IntegerField(label="Link valid for (minutes)", min_value=1, max_value=24 * 60, initial=60,
                                 widget=forms.NumberInput(attrs={"class": "form-control", "style": "max-width:8rem"}))

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rooms"].queryset = Room.objects.filter(event=event)
        self.fields["name"].widget.attrs["class"] = "form-control"


def _admin_event(request, slug):
    event = overseen_event(request, slug)
    if not is_admin(request.user):
        raise PermissionDenied
    return event


def _ctx(request, event, **extra):
    from .manage_views import _ctx as manage_ctx

    return manage_ctx(request, event, "attendance", **extra)


@never_cache
@login_required
def sessions_page(request, slug):
    """Admins: make links, see every session, decide requests, download the Excel."""
    event = _admin_event(request, slug)
    form = SessionForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            made = create_sessions(event, list(d["rooms"]), d["name"].strip(), d["opens_at"] or timezone.now(), d["minutes"], request.user)
        except AttendanceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"{len(made)} attendance link{'s' if len(made) != 1 else ''} made. Copy each link for its "
                                      "classroom's attendance-takers; they also see open links on their page.")
            return redirect("hackathon_manage_attendance", slug)
    sessions = list(AttendanceSession.objects.filter(event=event).select_related("room").order_by("-opens_at", "room__name"))
    counts = defaultdict(lambda: [0, 0])
    for m in SessionMark.objects.filter(session__event=event).values("session_id", "present"):
        counts[m["session_id"]][0] += 1
        counts[m["session_id"]][1] += m["present"]
    room_size = {}
    for m in TeamMember.objects.filter(team__event=event, team__room__isnull=False,
                                       registration__status=Registration.Status.CONFIRMED).values("team__room_id"):
        room_size[m["team__room_id"]] = room_size.get(m["team__room_id"], 0) + 1
    rows = [{"session": s, "link": request.build_absolute_uri(reverse("attendance_take", args=[s.token])),
             "marked": counts[s.pk][0], "present": counts[s.pk][1], "students": room_size.get(s.room_id, 0)} for s in sessions]
    pending = AttendanceRequest.objects.filter(session__event=event, status=AttendanceRequest.Status.PENDING).select_related(
        "session__room", "registration__person", "requested_by")
    takers = Volunteer.objects.filter(event=event).select_related("person").prefetch_related("rooms")
    return render(request, "hackathon/attendance_sessions.html", _ctx(
        request, event, title="Attendance", form=form, rows=rows, pending=pending, takers=takers,
        rooms=list(Room.objects.filter(event=event))))


@never_cache
@login_required
def session_detail(request, slug, pk):
    """Admins: one session's classroom list. Add latecomers or correct a mark at any time; end the link early."""
    event = _admin_event(request, slug)
    session = get_object_or_404(AttendanceSession.objects.select_related("room"), pk=pk, event=event)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "end":
            if not session.is_final():
                session.expires_at = timezone.now()
                session.opens_at = min(session.opens_at, session.expires_at - timedelta(seconds=1))
                session.save(update_fields=["expires_at", "opens_at"])
                messages.success(request, "The link has ended. This attendance is final.")
        elif action in ("present", "absent"):
            reg = get_object_or_404(Registration, pk=request.POST.get("registration"), event=event)
            try:
                mark = admin_mark(session, reg, action == "present", request.user)
            except AttendanceError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"{reg.person.display_name}: {'present' if mark.present else 'absent'} "
                                          f"({mark.get_source_display().lower()}).")
        nxt = request.POST.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else reverse("hackathon_attendance_session", args=[slug, pk]))
    marks = {m.registration_id: m for m in session.marks.select_related("marked_by")}
    groups = [(team, [(reg, marks.get(reg.pk)) for reg in regs]) for team, regs in room_students(session.room)]
    return render(request, "hackathon/attendance_session.html", _ctx(
        request, event, title=str(session), session=session, groups=groups,
        link=request.build_absolute_uri(reverse("attendance_take", args=[session.token])),
        requests=session.requests.select_related("registration__person", "requested_by", "decided_by")))


@login_required
@require_POST
def request_decide(request, slug, pk):
    event = _admin_event(request, slug)
    req = get_object_or_404(AttendanceRequest, pk=pk, session__event=event)
    try:
        decide(req, request.user, request.POST.get("decision") == "approve", request.POST.get("note", ""))
    except AttendanceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Request for {req.registration.person.display_name} {req.get_status_display().lower()}.")
    return redirect(request.POST.get("next") or reverse("hackathon_manage_attendance", args=[slug]))


@login_required
def export(request, slug):
    event = overseen_event(request, slug)
    if not (is_admin(request.user) or allowed(request.user, event, "can_download")):
        raise PermissionDenied
    stamp = timezone.localtime().strftime("%Y-%m-%d-%H%M")
    response = HttpResponse(workbook(event, request.user), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{event.slug}-attendance-sessions-{stamp}.xlsx"'
    return response


@never_cache
@login_required
def take_page(request, token):
    """The link: a signed-in attendance-taker of this classroom ticks who is there, while it is open."""
    session = get_object_or_404(AttendanceSession.objects.select_related("room", "event"), token=token)
    if not can_take(request.user, session):
        raise PermissionDenied
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "request":
                req = raise_request(session, request.user, request.POST.get("registration") or 0, request.POST.get("kind", ""),
                                    request.POST.get("want_present") == "1", request.POST.get("note", ""))
                messages.success(request, f"Request sent for {req.registration.person.display_name}. An admin will decide.")
            else:
                present, total = take(session, request.user, [x for x in request.POST.getlist("present") if x.isdigit()])
                messages.success(request, f"Saved: {present} of {total} present. You can change it until the link expires.")
        except AttendanceError as exc:
            messages.error(request, str(exc))
        return redirect("attendance_take", token)
    marks = {m.registration_id: m for m in session.marks.all()}
    groups = [(team, [(reg, marks.get(reg.pk)) for reg in regs]) for team, regs in room_students(session.room)]
    return render(request, "hackathon/attendance_take.html", {
        "session": session, "event": session.event, "groups": groups, "is_open": session.is_open(), "is_final": session.is_final(),
        "students": [reg for _, regs in groups for reg, _ in regs], "kinds": AttendanceRequest.Kind.choices,
        "my_requests": session.requests.filter(requested_by=request.user).select_related("registration__person"),
        "marked": bool(marks),
    })


# --- Attendance-taker accounts made by the portal -------------------------------------------------------


class TakerForm(forms.Form):
    names = forms.CharField(label="Names (one per line)", widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
                            error_messages={"required": "Type at least one name."},
                            help_text="One account is made for each name, with a login ID and a password made by the portal.")
    rooms = forms.ModelMultipleChoiceField(label="Classrooms they take attendance in", queryset=Room.objects.none(), required=False,
                                           widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}), help_text="They see and mark only these classrooms.")
    days = forms.IntegerField(label="Password valid for (days)", min_value=1, max_value=60, initial=7,
                              widget=forms.NumberInput(attrs={"class": "form-control", "style": "max-width:8rem"}))

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rooms"].queryset = Room.objects.filter(event=event)

    def clean_names(self):
        names = [" ".join(line.split()) for line in self.cleaned_data["names"].splitlines() if line.strip()]
        if not names:
            raise forms.ValidationError("Type at least one name.")
        if len(names) > 100:
            raise forms.ValidationError("At most 100 at a time.")
        if any(len(n) > 150 for n in names):
            raise forms.ValidationError("A name is longer than 150 characters.")
        return names


TAKER_SWITCHES = {"can_team_attendance": True, "can_faculty_attendance": False, "can_view_progress": False, "can_view_floors": True}


def make_takers(event, names, rooms, days) -> list[tuple]:
    """New accounts for attendance-takers: [(person, password)]. The password is shown once and never stored."""
    from people.models import Person
    from people.staff import issue_temp_password

    from .services import add_volunteer, set_volunteer_rooms

    out = []
    with transaction.atomic():
        for name in names:
            person = Person(full_name=name, email=None, source=Person.Source.IMPORT, profile_completed_at=timezone.now())
            person.set_unusable_password()
            person.save()
            row, _ = add_volunteer(event, person, switches=TAKER_SWITCHES)
            set_volunteer_rooms(row, rooms)
            out.append((person, issue_temp_password(person, timezone.now() + timedelta(days=days))))
    return out


@never_cache
@login_required
def takers_page(request, slug):
    """Admins: make attendance-taker accounts (login ID and password made here), or a new password for one."""
    from people.staff import issue_temp_password

    event = _admin_event(request, slug)
    form = TakerForm(request.POST or None, event=event)
    made = []
    if request.method == "POST" and request.POST.get("action") == "reset":
        row = get_object_or_404(Volunteer, pk=request.POST.get("volunteer"), event=event)
        if row.person.is_staff or row.person.is_superuser or row.person.pk == request.user.pk:
            raise PermissionDenied
        made = [(row.person, issue_temp_password(row.person, timezone.now() + timedelta(days=7)))]
    elif request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        made = make_takers(event, d["names"], list(d["rooms"]), d["days"])
    from django.conf import settings

    return render(request, "hackathon/attendance_takers.html", _ctx(
        request, event, title="Attendance-taker accounts", form=form, made=made,
        signin_url=settings.SITE_URL + reverse("signin"),
        takers=Volunteer.objects.filter(event=event).select_related("person").prefetch_related("rooms")))
