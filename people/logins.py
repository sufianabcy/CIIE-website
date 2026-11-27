"""Emailing login details (an email and a temporary password) to faculty and students, and keeping count.

Each email is an OutboxEmail of kind LOGIN tied to the person, so "has this person been sent their login?"
is answered from the outbox: no extra table. The temporary password is never stored; the mailer blanks the
email's body once it is sent. People change it at their first sign-in, and later reset it themselves with
"Forgot password", so login details normally go to each person once.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from mailer.models import OutboxEmail
from mailer.services import enqueue_email

from .models import Person
from .staff import issue_temp_password

TEMP_PASSWORD_DAYS = 7
# A second click within this time does nothing, so a double-click never sends two different passwords.
JUST_SENT = timedelta(minutes=5)


@dataclass
class LoginState:
    """Where one person stands with their login details."""

    code: str  # no_email, own_password, sent, queued, failed, temp_only, never
    label: str
    times_sent: int = 0
    last_at: object = None

    @property
    def done(self) -> bool:
        """Already has a way to sign in: their own password, or login details sent or on the way."""
        return self.code in ("own_password", "sent", "queued")

    @property
    def tag(self) -> str:
        return {"own_password": "tag-green", "sent": "tag-green", "failed": "tag-red"}.get(self.code, "")


def login_states(people) -> dict[int, LoginState]:
    """{person id: LoginState} for these people, from their login emails in the outbox."""
    people = list(people)
    emails = {}
    for person_id, status, created, sent in (
        OutboxEmail.objects.filter(kind=OutboxEmail.Kind.LOGIN, person__in=people)
        .order_by("created_at").values_list("person_id", "status", "created_at", "sent_at")
    ):
        count = emails.get(person_id, (0,))[0]
        emails[person_id] = (count + 1, status, sent or created)
    states = {}
    for p in people:
        count, status, at = emails.get(p.pk, (0, "", None))
        if p.has_usable_password() and not p.must_change_password:
            state = LoginState("own_password", "Has set their own password", count, at)
        elif not p.email:
            state = LoginState("no_email", "No email", count, at)
        elif status == OutboxEmail.Status.SENT:
            state = LoginState("sent", f"Sent {date_format(timezone.localtime(at), 'j M')}", count, at)
        elif status in (OutboxEmail.Status.QUEUED, OutboxEmail.Status.SENDING):
            state = LoginState("queued", "Waiting to send", count, at)
        elif status == OutboxEmail.Status.FAILED:
            state = LoginState("failed", "Failed to send", count, at)
        elif p.has_usable_password():
            state = LoginState("temp_only", "Not emailed (Excel password)", count, at)
        else:
            state = LoginState("never", "Not sent", count, at)
        states[p.pk] = state
    return states


def _context(person, password, expires_at, event, role):
    signin = settings.SITE_URL + reverse("signin")
    if role in ("faculty", "helper"):
        home = settings.SITE_URL + reverse("staff_home")
    elif event is not None:
        home = settings.SITE_URL + reverse("my_hackathon", args=[event.slug]) if event.is_hackathon else signin
    else:
        home = signin
    return {
        "person": person, "login": person.email, "login_id": person.login_id, "password": password,
        "expires_at": expires_at, "event": event, "role": role, "signin_url": signin, "home_url": home,
        "reset_url": settings.SITE_URL + reverse("account_reset_password"),
        "club_name": settings.CLUB_NAME, "contact_email": settings.CLUB_CONTACT_EMAIL,
    }


def send_login_details(person: Person, *, event=None, role="student") -> bool:
    """Give this person a new temporary password and email it with their login. False if not sent.

    Not sent when they have no email or were sent one moments ago (a double-click).
    """
    with transaction.atomic():
        person = Person.objects.select_for_update().get(pk=person.pk)
        if not person.email or not person.is_active:
            return False
        recent = OutboxEmail.objects.filter(
            kind=OutboxEmail.Kind.LOGIN, person=person, created_at__gte=timezone.now() - JUST_SENT
        )
        if recent.exists():
            return False
        expires_at = timezone.now() + timedelta(days=TEMP_PASSWORD_DAYS)
        password = issue_temp_password(person, expires_at)
        ctx = _context(person, password, expires_at, event, role)
        where = f"{event.title}: " if event is not None else ""
        enqueue_email(
            to=person.email,
            subject=f"[{settings.CLUB_NAME}] {where}your login details",
            text=render_to_string("email/login_details.txt", ctx),
            html=render_to_string("email/login_details.html", ctx),
            kind=OutboxEmail.Kind.LOGIN,
            person=person,
        )
    return True
