"""Emailing login details (temporary passwords) from a hackathon's Faculty and Students pages, and keeping count."""

import re

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from events.models import Event
from hackathon import services
from mailer.models import OutboxEmail
from mailer.services import LOGIN_BODY_REMOVED
from people.logins import login_states
from people.models import Person
from registrations.services import register

PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event):
    return make_event(title="Login Hack", kind=Event.Kind.HACKATHON)


def faculty(event, name, **kw):
    person = Person.objects.create_user(email=f"{name.lower()}@kluniversity.in", full_name=f"Dr. {name}", **kw)
    services.add_faculty(event, person)
    return person


def url(event, who):
    return reverse("hackathon_login_details", args=[event.slug, who])


def login_emails(person=None):
    qs = OutboxEmail.objects.filter(kind=OutboxEmail.Kind.LOGIN)
    return qs.filter(person=person) if person else qs


def sent_password(capture, client, *args, **kwargs):
    """Post, run the on-commit sends, and return the temporary passwords in the emails sent."""
    from django.core import mail

    before = len(mail.outbox)
    with capture(execute=True):
        response = client.post(*args, **kwargs)
    return response, [re.search(r"Temporary password: (\S+)", m.body).group(1) for m in mail.outbox[before:]]


def test_send_to_everyone_then_only_the_remaining(client, hack, admin_user, django_capture_on_commit_callbacks):
    a, b = faculty(hack, "A"), faculty(hack, "B")
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_faculty", args=[hack.slug])).content.decode()
    assert "Email login details" in page and "Login details" in page and "Not sent" in page
    confirm = client.get(url(hack, "faculty")).content.decode()
    assert "Nobody has been sent login details yet" in confirm and "Send to the 2 faculty" in confirm

    response, passwords = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "remaining"})
    assert response["Location"] == reverse("hackathon_manage_faculty", args=[hack.slug]) and len(passwords) == 2
    email = login_emails(a).get()
    assert email.status == OutboxEmail.Status.SENT and email.text_body == LOGIN_BODY_REMOVED  # the password isn't kept
    assert "login details" in email.subject

    # A third faculty member is added; the page now says who already has theirs.
    c = faculty(hack, "C")
    confirm = client.get(url(hack, "faculty")).content.decode()
    assert "2 of 3 already have their login details" in confirm and "Send to the 1 remaining" in confirm and "Send to all 3" in confirm
    _, passwords = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "remaining"})
    assert len(passwords) == 1 and login_emails(c).count() == 1 and login_emails(a).count() == 1

    # The emailed password works once, and must be changed straight away.
    client.logout()
    response = client.post(reverse("signin"), {"login": "c@kluniversity.in", "password": passwords[0]})
    assert response["Location"].startswith(reverse("password_change"))
    client.post(reverse("password_change"), {"new_password1": PASSWORD, "new_password2": PASSWORD})
    c.refresh_from_db()
    assert login_states([c])[c.pk].code == "own_password"


def test_send_to_all_again_replaces_passwords(client, hack, admin_user, django_capture_on_commit_callbacks):
    a = faculty(hack, "A")
    client.force_login(admin_user)
    _, first = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "remaining"})
    OutboxEmail.objects.update(created_at=OutboxEmail.objects.first().created_at.replace(year=2020))  # not a double-click
    _, second = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "all"})
    assert len(second) == 1 and first != second
    a.refresh_from_db()
    assert not a.check_password(first[0]) and a.check_password(second[0])
    state = login_states([a])[a.pk]
    assert state.code == "sent" and state.times_sent == 2
    assert "(2 times)" in client.get(reverse("hackathon_manage_faculty", args=[hack.slug])).content.decode()


def test_one_person_from_the_list_and_no_double_send(client, hack, admin_user, django_capture_on_commit_callbacks):
    a, b = faculty(hack, "A"), faculty(hack, "B")
    client.force_login(admin_user)
    _, passwords = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"person": a.pk})
    assert len(passwords) == 1 and login_emails(a).count() == 1 and not login_emails(b).exists()
    # A double-click moments later sends nothing more, so the emailed password stays the right one.
    _, again = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"person": a.pk}, follow=True)
    assert again == [] and login_emails(a).count() == 1
    a.refresh_from_db()
    assert a.check_password(passwords[0])
    page = client.get(reverse("hackathon_manage_faculty", args=[hack.slug])).content.decode()
    assert "Email again" in page and "already has login details" in page  # a confirm before resending


def test_remaining_skips_own_passwords_and_people_without_email(client, hack, admin_user, django_capture_on_commit_callbacks):
    own = faculty(hack, "Own")
    own.set_password(PASSWORD)
    own.save()
    no_email = Person.objects.create_user(email=None, full_name="Dr. Nomail", employee_id="KLF1")
    services.add_faculty(hack, no_email)
    fresh = faculty(hack, "Fresh")
    client.force_login(admin_user)
    confirm = client.get(url(hack, "faculty")).content.decode()
    assert "1 has set their own password" in confirm and "Send to the 1 remaining" in confirm
    _, passwords = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "remaining"})
    assert len(passwords) == 1 and login_emails(fresh).exists() and not login_emails(own).exists()
    own.refresh_from_db()
    assert own.check_password(PASSWORD)


def test_students_get_login_details_and_only_admins_send_them(client, hack, admin_user, make_person, django_capture_on_commit_callbacks):
    s1, s2 = make_person(email="s1@kluniversity.in"), make_person(email="s2@kluniversity.in")
    for s in (s1, s2):
        register(event=hack, person=s)
    supervisor = Person.objects.create_user(email="sup@kluniversity.in", full_name="Sup")
    services.add_supervisor(hack, supervisor, switches={"can_make_logins": True})
    client.force_login(supervisor)
    assert client.get(url(hack, "students")).status_code == 403
    assert "Email login details" not in client.get(reverse("hackathon_manage_students", args=[hack.slug])).content.decode()

    client.force_login(admin_user)
    _, passwords = sent_password(django_capture_on_commit_callbacks, client, url(hack, "students"), {"scope": "remaining"})
    assert len(passwords) == 2
    from django.core import mail

    assert "my_hackathon" not in mail.outbox[-1].body and f"/me/hackathon/{hack.slug}/" in mail.outbox[-1].body
    page = client.get(reverse("hackathon_manage_students", args=[hack.slug])).content.decode()
    assert page.count("Sent ") >= 2


def test_supervisors_send_only_to_plain_faculty_and_never_to_themselves(client, hack, admin_user, django_capture_on_commit_callbacks):
    plain = faculty(hack, "Plain")
    coordinator = faculty(hack, "Coord")
    coordinator.groups.add(Group.objects.get(name="Coordinator"))
    supervisor = Person.objects.create_user(email="sup@kluniversity.in", full_name="Sup")
    services.add_supervisor(hack, supervisor, switches={"can_make_logins": False})
    client.force_login(supervisor)
    assert client.get(url(hack, "faculty")).status_code == 403
    supervisor.supervisor_roles.update(can_make_logins=True)
    confirm = client.get(url(hack, "faculty")).content.decode()
    assert "bigger role" in confirm
    _, passwords = sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "all"})
    assert len(passwords) == 1 and login_emails(plain).exists() and not login_emails(coordinator).exists()
    assert client.post(url(hack, "faculty"), {"person": coordinator.pk}).status_code == 404

    # An admin who is also faculty never resets their own password from here.
    services.add_faculty(hack, admin_user)
    client.force_login(admin_user)
    assert "change your own password from your profile" in client.get(url(hack, "faculty")).content.decode()
    client.post(url(hack, "faculty"), {"scope": "all"})
    assert not login_emails(admin_user).exists()


def test_login_details_are_never_resent_as_copies_from_the_outbox(client, hack, admin_user, django_capture_on_commit_callbacks):
    faculty(hack, "A")
    client.force_login(admin_user)
    sent_password(django_capture_on_commit_callbacks, client, url(hack, "faculty"), {"scope": "remaining"})
    admin_user.is_superuser = True
    admin_user.save()
    email = login_emails().get()
    client.post(reverse("admin:mailer_outboxemail_changelist"), {"action": "send_again", "_selected_action": [email.pk]})
    assert login_emails().count() == 1
