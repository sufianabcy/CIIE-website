"""What happens at the door. Each check-in is labelled, stamped and undoable."""

from dataclasses import dataclass, field

from allauth.account.models import EmailAddress
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from events.models import Event
from people.models import Person, is_kl_email, normalise_email
from registrations.models import Registration, normalise_pass_code
from registrations.services import register

from .models import CheckIn, ScanLog


@dataclass
class DoorResult:
    """What the door screen shows: a colour, a word and the holder's name."""

    outcome: str
    registration: Registration | None = None
    checkin: CheckIn | None = None
    matches: list = field(default_factory=list)
    message: str = ""

    @property
    def tone(self) -> str:
        return {
            ScanLog.Outcome.CHECKED_IN: "ok",
            ScanLog.Outcome.ALREADY: "warn",
            ScanLog.Outcome.SEARCH: "info",
        }.get(self.outcome, "stop")


def _log(event, raw, outcome, *, by, gate="", registration=None, device=""):
    ScanLog.objects.create(
        event=event,
        raw_input=(raw or "")[:200],
        outcome=outcome,
        registration=registration,
        gate=gate[:30],
        device=device[:200],
        scanned_by=by,
    )


def check_in(registration: Registration, *, method: str, by, gate: str = "", note: str = "", when=None) -> DoorResult:
    """Check one registration in, or say who already did."""
    if not registration.is_confirmed:
        return DoorResult(ScanLog.Outcome.CANCELLED, registration, message="This registration was cancelled.")
    try:
        with transaction.atomic():
            checkin = CheckIn.objects.create(
                registration=registration,
                event=registration.event,
                method=method,
                gate=gate[:30],
                checked_in_by=by,
                checked_in_at=when or timezone.now(),
                note=note[:200],
            )
    except IntegrityError:
        # The partial unique index says one active check-in per pass.
        existing = registration.active_checkin()
        return DoorResult(ScanLog.Outcome.ALREADY, registration, checkin=existing)
    return DoorResult(ScanLog.Outcome.CHECKED_IN, registration, checkin=checkin)


def scan(event: Event, raw: str, *, by, gate: str = "", device: str = "") -> DoorResult:
    """One box: a scanned or typed pass code, otherwise a search."""
    code = normalise_pass_code(raw)
    if code:
        registration = (
            Registration.objects.select_related("person", "event").filter(pass_code=code).first()
        )
        if registration is None:
            result = DoorResult(ScanLog.Outcome.NOT_FOUND, message="No pass has this code.")
        elif registration.event_id != event.pk:
            result = DoorResult(
                ScanLog.Outcome.WRONG_EVENT,
                registration,
                message=f"This pass is for {registration.event.title}.",
            )
        else:
            result = check_in(registration, method=CheckIn.Method.SCAN, by=by, gate=gate)
        _log(event, raw, result.outcome, by=by, gate=gate, registration=result.registration, device=device)
        return result

    matches = search(event, raw)
    if not matches:
        _log(event, raw, ScanLog.Outcome.NOT_FOUND, by=by, gate=gate, device=device)
        return DoorResult(ScanLog.Outcome.NOT_FOUND, message="Nobody registered matches that.")
    _log(event, raw, ScanLog.Outcome.SEARCH, by=by, gate=gate, device=device)
    return DoorResult(ScanLog.Outcome.SEARCH, matches=matches)


def search(event: Event, text: str, limit: int = 12) -> list[Registration]:
    """Find registrations by name, phone, email or KL ID."""
    text = (text or "").strip()
    if len(text) < 3:
        return []
    digits = "".join(ch for ch in text if ch.isdigit())
    q = (
        Q(person__full_name__icontains=text)
        | Q(person__email__icontains=text)
        | Q(person__kl_email__icontains=text)
        | Q(person__student_id__iexact=text)
    )
    if len(digits) >= 6:
        q |= Q(person__phone__contains=digits[-10:]) | Q(person__student_id__startswith=digits)
    return list(
        Registration.objects.filter(event=event)
        .filter(q)
        .select_related("person")
        .prefetch_related("checkins")
        .order_by("person__full_name")[:limit]
    )


def undo(checkin: CheckIn, *, by, reason: str) -> None:
    if not checkin.is_active:
        return
    checkin.undone_at = timezone.now()
    checkin.undone_by = by
    checkin.undo_reason = reason[:200]
    checkin.save(update_fields=["undone_at", "undone_by", "undo_reason"])


def find_or_create_person(*, name: str, email: str = "", kl_id: str = "", phone: str = "") -> Person:
    """The walk-in desk's three fields become (or find) one person record."""
    email = normalise_email(email)
    kl_id = (kl_id or "").strip()
    if kl_id and not email and settings.KL_STUDENT_EMAIL_FORMAT:
        email = normalise_email(settings.KL_STUDENT_EMAIL_FORMAT.format(id=kl_id))
    person = None
    if kl_id:
        person = Person.objects.filter(student_id=kl_id).first()
    if person is None and email:
        person = (
            Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).first()
            or Person.objects.filter(emailaddress__email__iexact=email).first()
        )
    if person:
        return person
    try:
        with transaction.atomic():
            person = Person.objects.create_user(
                email=email,
                full_name=name.strip(),
                phone=phone.strip(),
                student_id=kl_id,
                institution="KL University" if (kl_id or is_kl_email(email)) else "",
                source=Person.Source.WALK_IN,
            )
            if email:
                # Unverified until they sign in with a code sent to it; then it is theirs.
                EmailAddress.objects.create(user=person, email=email, primary=True, verified=False)
    except IntegrityError:
        # Another desk added them a moment ago: use that record.
        person = (Person.objects.filter(student_id=kl_id).first() if kl_id else None) or Person.objects.filter(
            Q(email__iexact=email) | Q(kl_email__iexact=email)).first()
        if person is None:
            raise
    return person


def walk_in(event: Event, *, name: str, email: str, kl_id: str, phone: str, kind: str, by, gate: str = "") -> DoorResult:
    """kind is CheckIn.Method.WALK_IN (never registered) or MANUAL_ADD (said they did)."""
    person = find_or_create_person(name=name, email=email, kl_id=kl_id, phone=phone)
    source = Registration.Source.WALK_IN if kind == CheckIn.Method.WALK_IN else Registration.Source.MANUAL
    registration = Registration.objects.filter(event=event, person=person).first()
    if registration is None or not registration.is_confirmed:
        registration, _ = register(event=event, person=person, source=source, created_by=by)
    result = check_in(registration, method=kind, by=by, gate=gate)
    _log(event, f"{kind}: {name}", result.outcome, by=by, gate=gate, registration=registration)
    return result
