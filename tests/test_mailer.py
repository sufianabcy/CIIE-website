from django.core import mail

from mailer.models import OutboxEmail
from mailer.services import enqueue_email, send_pending
from ops.models import SiteSettings


def test_codes_go_first(db, settings):
    settings.MAIL_SEND_INLINE = False
    enqueue_email(to="a@example.com", subject="Reminder", text="r", kind="reminder")
    enqueue_email(to="b@example.com", subject="Code", text="c", kind="code")
    send_pending(limit=1)
    assert mail.outbox[0].subject == "Code"


def test_cap_keeps_room_for_codes(db, settings):
    settings.MAIL_SEND_INLINE = False
    settings.MAIL_DAILY_CAP = 3
    settings.MAIL_CODE_RESERVE = 1
    for i in range(4):
        enqueue_email(to=f"p{i}@example.com", subject=f"Confirm {i}", text="x", kind="confirmation")
    enqueue_email(to="c@example.com", subject="Code", text="c", kind="code")
    send_pending(limit=10)
    sent = OutboxEmail.objects.filter(status="sent")
    assert sent.filter(kind="code").count() == 1
    # Other mail stops once the day's total reaches cap minus reserve (3 - 1 = 2),
    # so one slot is still free for the next code.
    assert sent.filter(kind="confirmation").count() == 1
    assert OutboxEmail.objects.filter(status="queued").count() == 3


def test_pause_holds_everything_but_codes(db, settings):
    settings.MAIL_SEND_INLINE = False
    site = SiteSettings.load()
    site.email_paused = True
    site.save()
    enqueue_email(to="a@example.com", subject="Confirm", text="x", kind="confirmation")
    enqueue_email(to="b@example.com", subject="Code", text="c", kind="code")
    send_pending()
    assert [m.subject for m in mail.outbox] == ["Code"]


def test_failures_are_retried_later(db, settings, monkeypatch):
    settings.MAIL_SEND_INLINE = False
    from django.core.mail import EmailMultiAlternatives

    def boom(self, fail_silently=False):
        raise ConnectionError("provider down")

    monkeypatch.setattr(EmailMultiAlternatives, "send", boom)
    email = enqueue_email(to="a@example.com", subject="Confirm", text="x", kind="confirmation")
    send_pending()
    email.refresh_from_db()
    assert email.status == "queued" and email.attempts == 1 and "provider down" in email.last_error
    assert email.next_attempt_at > email.created_at


def test_tick_endpoint_needs_token(client, db, settings):
    settings.OPS_TICK_TOKEN = "secret-token"
    assert client.post("/ops/tick/").status_code == 403
    response = client.post("/ops/tick/", HTTP_X_TICK_TOKEN="secret-token")
    assert response.status_code == 200 and "emails_tried" in response.json()
