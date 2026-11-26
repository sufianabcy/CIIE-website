"""Emails to participants that aren't sign-in codes: "you've been added" and sign-in invites.

All go through the mailer outbox, so the daily and per-minute caps apply and sign-in codes go first.
"""

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from mailer.models import OutboxEmail
from mailer.services import enqueue_email


def participant_context(registration) -> dict:
    """What a participant may be told about their place in an event. The room and round times only once a room is set."""
    event = registration.event
    ctx = {
        "event": event, "person": registration.person, "registration": registration,
        "club_name": settings.CLUB_NAME, "contact_email": settings.CLUB_CONTACT_EMAIL,
        "signin_url": settings.SITE_URL + reverse("signin"),
        "home_url": settings.SITE_URL + registration.get_home_url(),
        "team": None, "members": [], "room": None, "rounds": [],
    }
    if event.is_hackathon:
        membership = getattr(registration, "team_membership", None)
        team = membership.team if membership else None
        if team:
            ctx["team"] = team
            ctx["members"] = [m.registration.person.display_name for m in team.members.select_related("registration__person")]
            if team.room_id:
                ctx["room"] = team.room
                ctx["rounds"] = list(event.rounds.exclude(opens_at=None))
    return ctx


def notify_added(registration) -> bool:
    """Queue "you've been added to <event>" the first time this person is added to it. True if queued."""
    person = registration.person
    if registration.added_email_at or not person.email:
        return False
    ctx = participant_context(registration)
    enqueue_email(
        to=person.email,
        subject=f"[{settings.CLUB_NAME}] You've been added to {registration.event.title}",
        text=render_to_string("email/added_to_event.txt", ctx),
        html=render_to_string("email/added_to_event.html", ctx),
        kind=OutboxEmail.Kind.CONFIRMATION,
        person=person,
        dedupe_key=f"added-{registration.event_id}-{person.pk}",
    )
    type(registration).objects.filter(pk=registration.pk).update(added_email_at=timezone.now())
    registration.added_email_at = timezone.now()
    return True


def invite_key(registration, attempt: int) -> str:
    return f"invite-{registration.event_id}-{registration.pk}-{attempt}"


def send_invite(registration, attempt: int = 1) -> bool:
    """Queue the "you can sign in now" email. attempt > 1 is a confirmed resend."""
    person = registration.person
    if not person.email:
        return False
    ctx = participant_context(registration)
    enqueue_email(
        to=person.email,
        subject=f"[{settings.CLUB_NAME}] {registration.event.title}: sign in to see your team and marks",
        text=render_to_string("email/signin_invite.txt", ctx),
        html=render_to_string("email/signin_invite.html", ctx),
        kind=OutboxEmail.Kind.OTHER,
        person=person,
        dedupe_key=invite_key(registration, attempt),
    )
    return True
