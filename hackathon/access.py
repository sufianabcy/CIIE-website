"""Who may do what at one hackathon. Every permission check goes through allowed().

- Admin (Admin group or superuser): everything.
- Overseers: the event's supervisors and coordinators. They open the hackathon page; what
  else they may do is set per supervisor by an admin (the switches in PERMISSIONS).
  Coordinators get the defaults.
- Event faculty: only their own allocated teams, only their own marks.
- Students: only their own team (see the student page).
"""

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from events.models import Event

from .models import EventFaculty, Supervisor


# field name -> (label, one-line explanation, default for a new supervisor)
PERMISSIONS = {
    "can_view_marks": ("View all marks", "See every faculty member's marks on the marks page.", True),
    "can_edit_teams": ("Edit rooms, teams and students", "Add and edit rooms, teams and students' details, and move teams between rooms.", True),
    "can_edit_allocations": ("Edit allocations", "Allocate faculty to rooms for each round, and remove allocations.", True),
    "can_edit_rounds": ("Edit round times", "Change when rounds open and close, including Open now and Close now.", True),
    "can_download": ("Download Excel files", "Download this hackathon's Excel files.", True),
    "can_change_marks": ("Change marks", "Change any faculty member's marks, even after they submit. A reason is recorded.", False),
    "can_reopen_marks": ("Reopen marksheets", "Let a faculty member edit marks they already submitted.", False),
    "can_publish": ("Publish results", "Publish or hide round results, and switch \"show jury to students\".", False),
    "can_make_logins": ("Make faculty passwords", "Make or reset temporary passwords for this hackathon's faculty.", False),
    "can_import": ("Import Excel", "Upload the students, faculty and allocations Excel files.", False),
    "can_approve_requests": ("Approve faculty requests", "Approve or reject faculty requests that chose this hackathon.", False),
    "can_add_faculty": ("Add and edit faculty", "Add people to this hackathon's faculty list and edit their details.", False),
    "can_take_attendance": ("Take attendance", "Mark teams and faculty present or absent in each round.", True),
    "can_view_floors": ("See floor lists", "Open the By floor page, and print or export it.", True),
    "can_email_participants": ("Email participants", "Send sign-in invites to this hackathon's students.", False),
    "can_manage_fields": ("Manage fields and imports", "Define the student and faculty detail fields, and match Excel columns on import.", False),
}
DEFAULTS = {name: default for name, (_, _, default) in PERMISSIONS.items()}

# How the switches are grouped on the supervisor form (every switch is in exactly one group; a test checks).
PERMISSION_GROUPS = [
    ("Teams", ["can_edit_teams"]),
    ("Rooms and allocation", ["can_edit_allocations"]),
    ("Rounds and rubric", ["can_edit_rounds", "can_publish"]),
    ("Marks", ["can_view_marks", "can_change_marks", "can_reopen_marks", "can_download"]),
    ("People", ["can_add_faculty", "can_approve_requests", "can_make_logins", "can_email_participants", "can_import", "can_manage_fields"]),
    ("Day of the event", ["can_take_attendance", "can_view_floors"]),
]

# Presets fill the switches in; an admin can still change single switches afterwards. Switches not listed are off.
# Every supervisor can look at the hackathon's teams and rooms; the presets add what they may change.
PRESETS = {
    "floor": ("Floor supervisor", "Attendance and floor lists; they can look at teams but not change them.",
              ["can_take_attendance", "can_view_floors"]),
    "marks": ("Marks supervisor", "View and change marks (with a reason), reopen marksheets, download Excel.",
              ["can_view_marks", "can_change_marks", "can_reopen_marks", "can_download"]),
    "full": ("Full helper", "Everything except publishing results.", [name for name in PERMISSIONS if name != "can_publish"]),
}

# Volunteers: on-the-day help. There is deliberately no switch for marks: volunteers never see them.
VOLUNTEER_PERMISSIONS = {
    "can_team_attendance": ("Team attendance", "Mark teams (and their members) present or absent in each round.", True),
    "can_faculty_attendance": ("Faculty attendance", "Mark faculty present or absent in each round.", True),
    "can_view_progress": ("See marking progress", "See which teams each faculty member has submitted marks for (never the marks).", True),
    "can_view_floors": ("See floor lists", "Open the By floor page, and print or export it.", True),
}
# Day-of tools, checked by day_allowed(): what each means for supervisors (and coordinators/admins) and for volunteers.
DAY_TOOLS = {
    "team_attendance": ("can_take_attendance", "can_team_attendance"),
    "faculty_attendance": ("can_take_attendance", "can_faculty_attendance"),
    "progress": ("can_view_floors", "can_view_progress"),
    "floors": ("can_view_floors", "can_view_floors"),
}


def volunteer_row(user, event):
    if not user.is_authenticated:
        return None
    from .models import Volunteer

    return Volunteer.objects.filter(event=event, person=user, is_active=True).first()


def day_allowed(user, event, tool: str) -> bool:
    """Day-of tools (attendance, progress, floor lists) for overseers by their switch, or volunteers by theirs."""
    supervisor_perm, volunteer_perm = DAY_TOOLS[tool]
    if oversees(user, event) and allowed(user, event, supervisor_perm):
        return True
    row = volunteer_row(user, event)
    return bool(row and getattr(row, volunteer_perm))


def sees_marks(user, event) -> bool:
    """Marks, totals, remarks and rankings. Volunteers never; others by the "view all marks" switch."""
    return oversees(user, event) and allowed(user, event, "can_view_marks")


def allowed(user, event, perm: str) -> bool:
    """The one permission check for hackathon actions."""
    if perm not in PERMISSIONS:
        raise ValueError(f"Unknown hackathon permission {perm}")
    if not user.is_authenticated:
        return False
    if user.is_portal_admin:
        return True
    row = Supervisor.objects.filter(event=event, person=user, is_active=True).first()
    if row:
        return getattr(row, perm)
    if event.coordinators.filter(pk=user.pk).exists():
        return DEFAULTS[perm]
    return False


def require(user, event, perm: str) -> None:
    if not allowed(user, event, perm):
        raise PermissionDenied


def permissions_for(user, event) -> dict:
    return {name: allowed(user, event, name) for name in PERMISSIONS}


def events_allowing(user, perm: str):
    """Hackathons where this person has this permission."""
    return [e for e in overseen_events(user) if allowed(user, e, perm)]


def is_admin(user) -> bool:
    return bool(user.is_authenticated and user.is_portal_admin)


def oversees(user, event) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_portal_admin or event.coordinators.filter(pk=user.pk).exists():
        return True
    return Supervisor.objects.filter(event=event, person=user, is_active=True).exists()


def overseen_event(request, slug) -> Event:
    event = get_object_or_404(Event, slug=slug, kind=Event.Kind.HACKATHON)
    if not oversees(request.user, event):
        raise PermissionDenied
    return event


def faculty_role(user, event) -> EventFaculty | None:
    if not user.is_authenticated:
        return None
    return EventFaculty.objects.filter(event=event, person=user, is_active=True).first()


def overseen_events(user):
    if not user.is_authenticated:
        return Event.objects.none()
    qs = Event.objects.filter(kind=Event.Kind.HACKATHON)
    if user.is_portal_admin:
        return qs
    return (
        qs.filter(coordinators=user) | qs.filter(supervisors__person=user, supervisors__is_active=True)
    ).distinct()
