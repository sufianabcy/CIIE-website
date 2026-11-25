from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from hackathon.models import EventFaculty, FacultyRequest
from mailer.models import OutboxEmail
from people.models import Person

HOME = "/admin/"


@pytest.fixture
def portal_admin(make_person):
    person = make_person(full_name="Admin Only", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def superuser(make_person):
    return make_person(full_name="Root", is_staff=True, is_superuser=True)


def test_dashboard_counts_what_needs_attention(client, portal_admin, make_person, make_event):
    applicant = make_person()
    FacultyRequest.objects.create(person=applicant, full_name="A", employee_id="E1", department="CSE", designation="AP", phone="9000000000")
    OutboxEmail.objects.create(to_email="a@example.com", subject="x", text_body="x", status=OutboxEmail.Status.FAILED)
    OutboxEmail.objects.create(to_email="b@example.com", subject="x", text_body="x", created_at=timezone.now() - timedelta(hours=1))
    OutboxEmail.objects.create(to_email="c@example.com", subject="x", text_body="x")  # just queued: not stuck
    make_event(title="This week", starts_at=timezone.now() + timedelta(days=2), ends_at=timezone.now() + timedelta(days=2, hours=3))
    make_event(title="Later", starts_at=timezone.now() + timedelta(days=30), ends_at=timezone.now() + timedelta(days=30, hours=3))
    client.force_login(portal_admin)
    response = client.get(HOME)
    attention = {a["label"]: a["value"] for a in response.context["attention"]}
    assert attention == {"Faculty requests waiting": 1, "Emails failed or stuck": 2, "Last good backup": "never", "Events this week": 1}
    body = response.content.decode()
    assert "+ Create event" in body and "Create a hackathon" in body and "Give someone a role" in body and "SQL console" in body
    assert "This week" in body and "Later" in body


def test_empty_home_says_what_to_do_first(client, portal_admin):
    client.force_login(portal_admin)
    body = client.get(HOME).content.decode()
    assert "No events yet." in body
    empty_list = client.get(reverse("admin:events_event_changelist")).content.decode()
    assert "No events yet." in empty_list and "?kind=hackathon" in empty_list


def test_technical_tables_are_for_superusers_only(client, portal_admin, superuser):
    client.force_login(portal_admin)
    body = client.get(HOME).content.decode()
    assert "Change history" not in body and "Verified email addresses" not in body and "Advanced" not in body
    assert client.get("/admin/pghistory/events/").status_code == 403
    assert client.get("/admin/account/emailaddress/").status_code == 403
    client.force_login(superuser)
    body = client.get(HOME).content.decode()
    assert "Advanced" in body and "Verified email addresses" in body and "Change history" in body
    assert client.get("/admin/account/emailaddress/").status_code == 200


def test_coordinators_see_their_events_without_admin_counts(client, coordinator, event):
    client.force_login(coordinator)
    response = client.get(HOME)
    assert response.context["attention"] == [] and not response.context["can_add_event"]
    assert event.title.encode() in response.content


def test_creating_an_event_is_one_short_screen_then_the_details(client, portal_admin):
    client.force_login(portal_admin)
    add = client.get(reverse("admin:events_event_add") + "?kind=hackathon")
    body = add.content.decode()
    assert 'name="title"' in body and 'name="description"' not in body and 'name="slug"' not in body
    assert 'value="hackathon" selected' in body or 'value="hackathon" checked' in body or "selected>Hackathon" in body
    response = client.post(reverse("admin:events_event_add"), {
        "title": "Code Sprint", "kind": "hackathon", "status": "draft",
        "starts_at_0": "2026-12-20", "starts_at_1": "09:00", "ends_at_0": "2026-12-20", "ends_at_1": "18:00", "venue": "R&D",
    })
    event = Event.objects.get(title="Code Sprint")
    assert response["Location"] == reverse("admin:events_event_change", args=[event.pk])
    assert event.slug == "code-sprint" and event.is_hackathon
    change = client.get(response["Location"]).content.decode()
    assert "Open hackathon page" in change and reverse("hackathon_dashboard", args=[event.slug]) in change
    assert "Save and continue editing" not in change and "Save and add another" not in change
    assert "Door-team link" not in change and "show_jury_to_students" in change


def test_normal_event_form_has_no_hackathon_section(client, portal_admin, event):
    client.force_login(portal_admin)
    change = client.get(reverse("admin:events_event_change", args=[event.pk])).content.decode()
    assert "show_jury_to_students" not in change and "Run the door" in change and "Door-team link" in change


def test_role_choice_sets_the_groups(client, portal_admin, make_person, make_event):
    person = make_person(email="role@example.com")
    url = reverse("admin:people_person_change", args=[person.pk])
    client.force_login(portal_admin)
    page = client.get(url).content.decode()
    assert 'name="role"' in page and 'name="groups"' not in page  # the raw widget is for superusers only
    data = {"email": person.email, "full_name": person.full_name, "phone": person.phone, "institution": person.institution,
            "branch": "", "year_of_study": "", "kl_email": "", "student_id": "", "employee_id": "",
            "temp_password_expires_at_0": "", "temp_password_expires_at_1": "", "is_active": "on"}
    client.post(url, {**data, "role": "coordinator"})
    person.refresh_from_db()
    assert person.is_coordinator and person.is_staff and not person.is_portal_admin
    client.post(url, {**data, "role": "student"})
    person.refresh_from_db()
    assert not person.groups.exists() and not person.is_staff
    # Someone who judges at a hackathon keeps the Faculty role when made a coordinator.
    hack = make_event(kind=Event.Kind.HACKATHON)
    person.groups.add(Group.objects.get(name="Faculty"))
    EventFaculty.objects.create(event=hack, person=person)
    client.post(url, {**data, "role": "coordinator"})
    assert set(Person.objects.get(pk=person.pk).groups.values_list("name", flat=True)) == {"Faculty", "Coordinator"}
