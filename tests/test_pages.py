from io import BytesIO

from django.core import mail
from django.urls import reverse
from openpyxl import load_workbook

from events.exports import build_export
from events.models import Event, Question
from mailer.models import OutboxEmail
from people.models import Person
from registrations.models import Registration
from registrations.services import register


def test_public_pages(client, event, make_event):
    make_event(title="Secret draft", slug="secret", status=Event.Status.DRAFT)
    response = client.get(reverse("event_list"))
    assert event.title.encode() in response.content
    assert b"Secret draft" not in response.content
    assert client.get(reverse("event_detail", args=[event.slug])).status_code == 200
    assert client.get(reverse("event_detail", args=["secret"])).status_code == 404


def test_register_needs_profile_then_works_and_queues_confirmation(client, event, make_person, django_capture_on_commit_callbacks):
    Question.objects.create(event=event, key="team", label="Team name", required=True)
    person = make_person(profile_completed_at=None)
    client.force_login(person)
    url = reverse("register", args=[event.slug])
    assert reverse("profile") in client.get(url)["Location"]

    person.profile_completed_at = person.date_joined
    person.save()
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"q_team": "Rockets"})
    registration = Registration.objects.get(event=event, person=person)
    assert response["Location"] == registration.get_pass_url()
    assert registration.answers == {"team": "Rockets"}
    assert OutboxEmail.objects.filter(kind="confirmation", status="sent").count() == 1
    assert registration.pass_code_display in mail.outbox[0].body

    page = client.get(registration.get_pass_url())
    assert person.full_name.encode() in page.content
    # A full 21-module QR code with a quiet zone, not a Micro QR that door scanners can't read.
    assert b'viewBox="0 0 232 232"' in page.content


def test_pass_is_private(client, event, make_person):
    registration, _ = register(event=event, person=make_person())
    client.force_login(make_person())
    assert client.get(registration.get_pass_url()).status_code == 404


def test_kl_only_event_needs_kl_verification(client, make_event, make_person):
    event = make_event(kl_only=True)
    person = make_person()
    client.force_login(person)
    response = client.post(reverse("register", args=[event.slug]), {})
    assert b"KL students only" in response.content
    assert not Registration.objects.filter(event=event).exists()


def test_signin_new_email_creates_account_and_sends_code(client, db, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("signin"), {"email": "New.Person@Example.com"})
    assert response.status_code == 302
    assert Person.objects.filter(email="new.person@example.com").exists()
    code_mail = OutboxEmail.objects.get(kind="code")
    assert code_mail.to_email == "new.person@example.com"
    assert code_mail.status == "sent"
    assert "removed after sending" in code_mail.text_body  # codes are not kept


def test_signin_with_code_marks_kl_students(client, make_person, django_capture_on_commit_callbacks, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    person = make_person(email="2300031234@kluniversity.in")
    from allauth.account.models import EmailAddress

    EmailAddress.objects.create(user=person, email=person.email, primary=True, verified=False)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("signin"), {"email": person.email})
    assert response.status_code == 302
    body = mail.outbox[-1].body
    code = next(line.strip() for line in body.splitlines() if line.strip() and len(line.strip()) <= 9 and line.strip().replace("-", "").isalnum() and line.strip().upper() == line.strip())
    client.post(reverse("account_confirm_login_code"), {"code": code})
    person.refresh_from_db()
    assert person.is_kl_verified
    assert person.student_id == "2300031234"


def test_exports_keep_phone_as_text(event, make_person, coordinator):
    register(event=event, person=make_person(phone="09876543210"))
    for kind in ("registrations", "attendance", "results", "attendance-handoff"):
        name, content = build_export(event, kind, coordinator)
        assert name.startswith(event.slug) and name.endswith(".xlsx")
        wb = load_workbook(BytesIO(content))
        assert "About this file" in wb.sheetnames
    wb = load_workbook(BytesIO(build_export(event, "registrations", coordinator)[1]))
    ws = wb["Registrations"]
    headers = [c.value for c in ws[1]]
    phone = ws.cell(row=2, column=headers.index("Phone") + 1)
    assert phone.value == "09876543210" and phone.number_format == "@"


def test_coordinator_pages(client, event, coordinator):
    client.force_login(coordinator)
    for name in ("door_counts", "door_list", "event_report"):
        assert client.get(reverse(name, args=[event.slug])).status_code == 200
    assert client.get(reverse("manage")).status_code == 200
    assert client.get(reverse("event_export", args=[event.slug, "attendance"])).status_code == 200
