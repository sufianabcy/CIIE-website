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
    "can_manage_fields": ("Manage fields and imports", "Define the student and faculty detail fields, and match Excel columns on import.", False),
}
DEFAULTS = {name: default for name, (_, _, default) in PERMISSIONS.items()}


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
