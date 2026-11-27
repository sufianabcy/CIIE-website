"""The email queue: enqueue, send one, send whatever is due.

Free hosting usually has no background worker, so by default an email is
sent straight after the database commit (MAIL_SEND_INLINE). Anything that
fails or hits the cap stays queued for the worker, the /ops/tick/ endpoint
or the send_mail_queue command.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from .models import OutboxEmail

log = logging.getLogger(__name__)

PRIORITY_BY_KIND = {
    OutboxEmail.Kind.CODE: 1,
    OutboxEmail.Kind.LOGIN: 2,
    OutboxEmail.Kind.CONFIRMATION: 2,
    OutboxEmail.Kind.DOOR_LIST: 2,
}
MAX_ATTEMPTS = 6
STUCK_AFTER = timedelta(minutes=15)
CODE_BODY_REMOVED = "(The code in this email was removed after sending.)"
LOGIN_BODY_REMOVED = "(The temporary password in this email was removed after sending.)"
# Emails holding a secret: the body is not kept once sent, and they are never sent again as a copy.
SECRET_KINDS = {OutboxEmail.Kind.CODE: CODE_BODY_REMOVED, OutboxEmail.Kind.LOGIN: LOGIN_BODY_REMOVED}


def enqueue_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    kind: str = OutboxEmail.Kind.OTHER,
    person=None,
    dedupe_key: str | None = None,
    send_now: bool | None = None,
) -> OutboxEmail:
    """Queue an email. With a dedupe_key, the same email is only queued once."""
    values = {
        "to_email": to,
        "subject": subject[:255],
        "text_body": text,
        "html_body": html or "",
        "kind": kind,
        "priority": PRIORITY_BY_KIND.get(kind, 3),
        "person": person,
    }
    if dedupe_key:
        email, created = OutboxEmail.objects.get_or_create(dedupe_key=dedupe_key, defaults=values)
        if not created:
            return email
    else:
        email = OutboxEmail.objects.create(**values)

    inline = settings.MAIL_SEND_INLINE if send_now is None else send_now
    if inline:
        pk = email.pk
        transaction.on_commit(lambda: send_one(pk))
    return email


@dataclass
class Budget:
    codes: bool
    other: bool
    sent_last_day: int


def current_budget() -> Budget:
    """What the daily cap and the per-minute rate allow right now."""
    from ops.models import SiteSettings

    now = timezone.now()
    sent = OutboxEmail.objects.filter(status=OutboxEmail.Status.SENT)
    last_day = sent.filter(sent_at__gte=now - timedelta(days=1)).count()
    last_minute = sent.filter(sent_at__gte=now - timedelta(minutes=1)).count()
    under_rate = last_minute < settings.MAIL_RATE_PER_MINUTE
    codes = under_rate and last_day < settings.MAIL_DAILY_CAP
    other = (
        under_rate
        and last_day < settings.MAIL_DAILY_CAP - settings.MAIL_CODE_RESERVE
        and not SiteSettings.load().email_paused
    )
    return Budget(codes=codes, other=other, sent_last_day=last_day)


def _allowed(email: OutboxEmail, budget: Budget) -> bool:
    return budget.codes if email.kind == OutboxEmail.Kind.CODE else budget.other


def _claim(pk: int) -> OutboxEmail | None:
    """Mark one queued email as being sent, so no other process sends it too."""
    now = timezone.now()
    claimed = OutboxEmail.objects.filter(
        pk=pk, status=OutboxEmail.Status.QUEUED, next_attempt_at__lte=now
    ).update(status=OutboxEmail.Status.SENDING, claimed_at=now)
    if not claimed:
        return None
    return OutboxEmail.objects.get(pk=pk)


def from_address() -> str:
    from ops.models import SiteSettings

    name, address = parseaddr(settings.DEFAULT_FROM_EMAIL)
    sender_name = SiteSettings.load().sender_name.strip()
    return formataddr((sender_name or name, address))


def _deliver(email: OutboxEmail) -> bool:
    message = EmailMultiAlternatives(
        subject=email.subject,
        body=email.text_body,
        from_email=from_address(),
        to=[email.to_email],
        reply_to=[settings.CLUB_CONTACT_EMAIL] if settings.CLUB_CONTACT_EMAIL else None,
    )
    if email.html_body:
        message.attach_alternative(email.html_body, "text/html")
    email.attempts += 1
    try:
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - any provider error is recorded and retried
        log.warning("Email %s to %s failed: %s", email.pk, email.to_email, exc)
        email.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        if email.attempts >= MAX_ATTEMPTS:
            email.status = OutboxEmail.Status.FAILED
        else:
            email.status = OutboxEmail.Status.QUEUED
            email.next_attempt_at = timezone.now() + timedelta(minutes=2**email.attempts)
        email.claimed_at = None
        email.save(update_fields=["attempts", "last_error", "status", "next_attempt_at", "claimed_at"])
        return False

    email.status = OutboxEmail.Status.SENT
    email.sent_at = timezone.now()
    email.last_error = ""
    fields = ["attempts", "status", "sent_at", "last_error"]
    if email.kind in SECRET_KINDS:
        email.text_body = SECRET_KINDS[email.kind]
        email.html_body = ""
        fields += ["text_body", "html_body"]
    email.save(update_fields=fields)
    return True


def send_one(pk: int) -> bool:
    """Try to send one queued email now. Never raises."""
    try:
        email = OutboxEmail.objects.filter(pk=pk, status=OutboxEmail.Status.QUEUED).first()
        if email is None or not _allowed(email, current_budget()):
            return False
        email = _claim(pk)
        return bool(email) and _deliver(email)
    except Exception:  # noqa: BLE001 - a page must never fail because email did
        log.exception("Could not send email %s", pk)
        return False


def requeue_stuck() -> int:
    """Emails left in "sending" by a crashed process go back in the queue."""
    return OutboxEmail.objects.filter(
        status=OutboxEmail.Status.SENDING, claimed_at__lt=timezone.now() - STUCK_AFTER
    ).update(status=OutboxEmail.Status.QUEUED, claimed_at=None)


def send_pending(limit: int = 50) -> int:
    """Send due emails, codes first, within the cap. Returns how many were tried."""
    requeue_stuck()
    tried = 0
    while tried < limit:
        budget = current_budget()
        if not (budget.codes or budget.other):
            break
        due = OutboxEmail.objects.filter(
            status=OutboxEmail.Status.QUEUED, next_attempt_at__lte=timezone.now()
        ).order_by("priority", "created_at")
        if not budget.other:
            due = due.filter(kind=OutboxEmail.Kind.CODE)
        pk = due.values_list("pk", flat=True).first()
        if pk is None:
            break
        email = _claim(pk)
        if email is None:
            continue
        _deliver(email)
        tried += 1
    return tried
