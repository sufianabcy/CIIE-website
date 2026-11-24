"""Registering, cancelling and confirming. The database guards seats and duplicates."""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone

from events.models import Event
from mailer.models import OutboxEmail
from mailer.services import enqueue_email

from .models import Registration


class RegistrationError(Exception):
    """A registration that cannot happen, with a message fit for the person."""


class EventFull(RegistrationError):
    pass


def _take_seat(event: Event) -> None:
    if event.seats is None:
        Event.objects.filter(pk=event.pk).update(seats_taken=F("seats_taken") + 1)
        return
    # The conditional update is the seat lock: only one of two people
    # racing for the last seat gets a row back.
    taken = Event.objects.filter(pk=event.pk, seats_taken__lt=F("seats")).update(
        seats_taken=F("seats_taken") + 1
    )
    if not taken:
        raise EventFull("All seats are taken.")


def _release_seat(event: Event) -> None:
    Event.objects.filter(pk=event.pk, seats_taken__gt=0).update(seats_taken=F("seats_taken") - 1)


def register(
    *,
    event: Event,
    person,
    answers: dict | None = None,
    source: str = Registration.Source.ONLINE,
    created_by=None,
) -> tuple[Registration, bool]:
    """Register a person. Returns (registration, created). Safe to call twice."""
    answers = answers or {}
    takes_seat = source in Registration.SEAT_SOURCES
    for _attempt in range(3):
        try:
            with transaction.atomic():
                existing = (
                    Registration.objects.select_for_update().filter(event=event, person=person).first()
                )
                if existing and existing.is_confirmed:
                    return existing, False
                if takes_seat:
                    _take_seat(event)
                if existing:
                    existing.status = Registration.Status.CONFIRMED
                    existing.cancelled_at = None
                    existing.cancelled_by = None
                    existing.answers = answers
                    existing.form_version = event.form_version
                    existing.source = source
                    existing.save()
                    return existing, True
                registration = Registration.objects.create(
                    event=event,
                    person=person,
                    answers=answers,
                    form_version=event.form_version,
                    source=source,
                    created_by=created_by or person,
                )
                return registration, True
        except IntegrityError:
            # Either the same person registered twice at the same moment, or
            # (very rarely) two pass codes collided. Both roll back the seat.
            existing = Registration.objects.filter(
                event=event, person=person, status=Registration.Status.CONFIRMED
            ).first()
            if existing:
                return existing, False
    raise RegistrationError("Your registration could not be saved. Please try again.")


def cancel(registration: Registration, *, by) -> None:
    with transaction.atomic():
        registration = Registration.objects.select_for_update().get(pk=registration.pk)
        if not registration.is_confirmed:
            return
        if registration.checkins.filter(undone_at__isnull=True).exists():
            raise RegistrationError("You were already checked in, so this registration cannot be cancelled.")
        registration.status = Registration.Status.CANCELLED
        registration.cancelled_at = timezone.now()
        registration.cancelled_by = by
        registration.save(update_fields=["status", "cancelled_at", "cancelled_by"])
        if registration.takes_seat:
            _release_seat(registration.event)


def send_confirmation(registration: Registration) -> None:
    person = registration.person
    if not person.email:
        return
    context = {
        "registration": registration,
        "event": registration.event,
        "person": person,
        "pass_url": settings.SITE_URL + registration.get_pass_url(),
        "home_url": settings.SITE_URL + registration.get_home_url(),
        "my_url": settings.SITE_URL + "/me/",
        "club_name": settings.CLUB_NAME,
        "contact_email": settings.CLUB_CONTACT_EMAIL,
    }
    template = "hackathon_confirmation" if registration.event.is_hackathon else "registration_confirmation"
    enqueue_email(
        to=person.email,
        subject=f"[{settings.CLUB_NAME}] You're registered: {registration.event.title}",
        text=render_to_string(f"email/{template}.txt", context),
        html=render_to_string(f"email/{template}.html", context),
        kind=OutboxEmail.Kind.CONFIRMATION,
        person=person,
    )
