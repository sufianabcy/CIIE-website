from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from hackathon import services
from hackathon.models import Assignment, Criterion, Room, Round, Score, Team, TeamMember
from mailer.models import OutboxEmail
from mailer.services import current_budget
from people.models import Person
from registrations.models import Registration
from registrations.notify import notify_added
from registrations.services import register
from tests.test_team_import import choose, upload, xlsx


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event):
    return make_event(title="Mail Hack", kind=Event.Kind.HACKATHON, team_size_min=1, team_size_max=3)


# --- Profiles from KL ID numbers -------------------------------------------------------------


def test_students_are_found_or_created_by_kl_id(make_person):
    person, created, problem = services._upsert_person({"name": "New One", "student_id": "2300012345"})
    assert created and not problem
    assert (person.email, person.kl_email, person.kind, person.student_id) == ("2300012345@kluniversity.in", "2300012345@kluniversity.in", "student", "2300012345")
    # A profile with that KL email but no ID gets the ID attached.
    has_email = make_person(email="2300099999@kluniversity.in", full_name="Had Email")
    found, created, _ = services._upsert_person({"name": "Had Email", "student_id": "2300099999", "email": "personal@example.com"})
    assert found == has_email and not created
    found.refresh_from_db()
    assert found.student_id == "2300099999" and found.contact_email == "personal@example.com" and found.email == "2300099999@kluniversity.in"


def test_an_id_and_an_email_of_two_people_is_a_clash(make_person):
    make_person(email="2300011111@kluniversity.in", full_name="Owner Of Email")
    make_person(email="x@example.com", student_id="2300022222", full_name="Owner Of Id")
    person, _, problem = services._upsert_person({"name": "Mixed", "student_id": "2300022222", "email": "2300011111@kluniversity.in"})
    assert person is None and "belong to different people" in problem


def test_portal_add_by_kl_id_alone(client, hack, admin_user):
    client.force_login(admin_user)
    client.post(reverse("hackathon_manage_student_add", args=[hack.slug]), {"d-f_name": "Only Id", "d-f_student_id": "2300055555", "t-team": ""})
    person = Person.objects.get(student_id="2300055555")
    assert person.email == "2300055555@kluniversity.in" and Registration.objects.filter(event=hack, person=person).exists()


# --- "You've been added" ---------------------------------------------------------------------


def test_added_email_once_per_person_per_event_and_room_only_when_allocated(hack, make_person):
    reg, _ = register(event=hack, person=make_person(email="a@example.com", full_name="Asha"))
    team = Team.objects.create(event=hack, name="Rocket", project_title="Smart bins")
    TeamMember.objects.create(team=team, registration=reg, is_leader=True)
    assert notify_added(reg)
    first = OutboxEmail.objects.get(to_email="a@example.com")
    assert "You've been added to Mail Hack" in first.subject
    assert "Team: Rocket" in first.text_body and "Problem statement: Smart bins" in first.text_body and "Room:" not in first.text_body
    assert "sign in" in first.text_body.lower() and "a@example.com" in first.text_body
    reg.refresh_from_db()
    assert reg.added_email_at and not notify_added(reg)  # never twice
    make_event_with_room(hack, make_person)
    text = OutboxEmail.objects.get(to_email="b@example.com").text_body
    assert "Room: L502" in text and "Round 1" in text


def make_event_with_room(hack, make_person):
    room = Room.objects.create(event=hack, name="L502", capacity=4)
    Round.objects.create(event=hack, name="Round 1", position=1, opens_at=timezone.now() + timedelta(days=1))
    reg, _ = register(event=hack, person=make_person(email="b@example.com", full_name="Bo"))
    team = Team.objects.create(event=hack, name="Comet", room=room)
    TeamMember.objects.create(team=team, registration=reg, is_leader=True)
    notify_added(reg)
    return reg


def test_import_emails_people_new_to_the_event_only_when_ticked(client, hack, admin_user, make_person, django_capture_on_commit_callbacks):
    old = make_person(email="2300000001@kluniversity.in", student_id="2300000001", full_name="Already")
    register(event=hack, person=old)
    client.force_login(admin_user)
    sheet = [["Name", "KL ID"], ["Already", "2300000001"], ["New One", "2300000002"], ["New Two", "2300000003"]]
    url = upload(client, hack, "students", xlsx(sheet))
    preview = choose(client, url, {0: "name", 1: "student_id"}).content.decode()
    assert "Email the 2 people new to this event" in preview
    with django_capture_on_commit_callbacks(execute=True):
        done = client.post(url, {"action": "import", "email_new": "1", "col_0": "name", "col_1": "student_id"}, follow=True).content.decode()
    assert "2 people emailed" in done
    assert set(OutboxEmail.objects.filter(subject__contains="added").values_list("to_email", flat=True)) == {
        "2300000002@kluniversity.in", "2300000003@kluniversity.in"}
    # Unticked: nobody is emailed.
    url = upload(client, hack, "students", xlsx([["Name", "KL ID"], ["Quiet", "2300000009"]]))
    choose(client, url, {0: "name", 1: "student_id"})
    client.post(url, {"action": "import", "col_0": "name", "col_1": "student_id"})
    assert not OutboxEmail.objects.filter(to_email="2300000009@kluniversity.in").exists()


# --- Sign-in invites -------------------------------------------------------------------------


def test_invites_preview_permissions_and_no_double_send(client, hack, admin_user, make_person):
    for i in range(3):
        register(event=hack, person=make_person(email=f"s{i}@example.com"))
    url = reverse("hackathon_invites", args=[hack.slug])
    sup = services.add_supervisor(hack, Person.objects.create_user(email="sup@example.com", full_name="Sup"))[0]
    client.force_login(sup.person)
    assert client.get(url).status_code == 403
    sup.can_email_participants = True
    sup.save()
    page = client.get(url).content.decode()
    assert "3 students without a team." in page and "No faculty allocated yet." in page and "3 never invited" in page
    assert client.post(url, {"action": "send_new"}).status_code == 302
    assert not OutboxEmail.objects.filter(dedupe_key__startswith="invite-").exists()  # no confirm tick
    client.post(url, {"action": "send_new", "confirm": "1"})
    assert OutboxEmail.objects.filter(dedupe_key__startswith="invite-").count() == 3
    client.post(url, {"action": "send_new", "confirm": "1"})
    assert OutboxEmail.objects.filter(dedupe_key__startswith="invite-").count() == 3  # nobody twice
    first = Registration.objects.filter(event=hack).first()
    client.post(url, {"action": "resend_chosen", "confirm": "1", "chosen": [first.pk]})
    assert OutboxEmail.objects.filter(dedupe_key=f"invite-{hack.pk}-{first.pk}-2").exists()
    mail = OutboxEmail.objects.filter(dedupe_key__startswith="invite-").first()
    assert "/signin/" in mail.text_body and "KL email" in mail.text_body
    client.force_login(make_person())
    assert client.get(url).status_code == 403
    client.force_login(admin_user)
    normal = Event.objects.create(title="Talk", slug="talk", starts_at=hack.starts_at, ends_at=hack.ends_at)
    assert client.get(reverse("hackathon_invites", args=[normal.slug])).status_code == 200


# --- Marks for students ----------------------------------------------------------------------


def _marked(hack, make_person):
    round1 = Round.objects.create(event=hack, name="Round 1", position=1, opens_at=timezone.now() - timedelta(hours=1))
    idea = Criterion.objects.create(round=round1, label="Idea", max_marks=10)
    reg, _ = register(event=hack, person=make_person(email="team@example.com"))
    team = Team.objects.create(event=hack, name="Rocket")
    TeamMember.objects.create(team=team, registration=reg, is_leader=True)
    faculty = services.add_faculty(hack, Person.objects.create_user(email="f@kluniversity.in", full_name="Dr. F"))[0]
    a = Assignment.objects.create(round=round1, faculty=faculty, team=team)
    return round1, idea, reg, a


def test_students_see_submitted_or_pending_but_no_numbers_until_released(client, hack, make_person):
    round1, idea, reg, a = _marked(hack, make_person)
    client.force_login(reg.person)
    url = reverse("my_hackathon", args=[hack.slug])
    assert "Pending" in client.get(url).content.decode()
    Score.objects.create(assignment=a, criterion=idea, marks=Decimal("8.75"))
    a.submitted_at = timezone.now()
    a.save()
    page = client.get(url).content.decode()
    assert "Marks submitted" in page and "8.75" not in page
    round1.release_marks_at = timezone.now() + timedelta(hours=1)
    round1.save()
    assert "8.75" not in client.get(url).content.decode()
    round1.release_marks_at = timezone.now() - timedelta(minutes=1)
    round1.save()
    assert "8.75" in client.get(url).content.decode()


# --- Sending limits and the test command ------------------------------------------------------


@pytest.mark.django_db
@override_settings(MAIL_RATE_PER_MINUTE=2, MAIL_DAILY_CAP=1000, MAIL_CODE_RESERVE=10)
def test_per_minute_cap_holds_back_other_mail_but_codes_go_first():
    for i in range(2):
        OutboxEmail.objects.create(to_email=f"x{i}@example.com", subject="s", text_body="t", status=OutboxEmail.Status.SENT, sent_at=timezone.now())
    budget = current_budget()
    assert not budget.other and not budget.codes  # at the cap this minute
    OutboxEmail.objects.update(sent_at=timezone.now() - timedelta(minutes=2))
    budget = current_budget()
    assert budget.other and budget.codes


def test_send_test_email_command(capsys):
    from django.core import mail

    call_command("send_test_email", "--to", "someone@kluniversity.in")
    out = capsys.readouterr().out
    assert "Sent to someone@kluniversity.in" in out and "Transport:" in out and "PASSWORD" not in out
    assert mail.outbox[-1].to == ["someone@kluniversity.in"]
