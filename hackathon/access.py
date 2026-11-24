"""Who may do what at one hackathon.

- Admin (Admin group or superuser): everything, including changing marks after submission.
- Overseer: the event's supervisors and coordinators. See everything for that event and
  edit details (rooms, teams, allocations, round times), never marks.
- Event faculty: only their own allocated teams, only their own marks.
- Students: only their own team (see the student page).
"""

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from events.models import Event

from .models import EventFaculty, Supervisor


def is_admin(user) -> bool:
    return bool(user.is_authenticated and user.is_portal_admin)


def oversees(user, event) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_portal_admin or event.coordinators.filter(pk=user.pk).exists():
        return True
    return Supervisor.objects.filter(event=event, person=user, is_active=True).exists()


def can_change_marks(user) -> bool:
    """Only admins change marks (faculty change their own until they submit). Easy to widen here later."""
    return is_admin(user)


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
