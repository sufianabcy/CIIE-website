from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from events.models import Event
from hackathon import services
from hackathon.models import Assignment, Criterion, Room, Round, Score, Team, TeamMember
from people.models import Person
from registrations.models import Registration
from registrations.services import RegistrationError, register
from tests.test_team_import import choose, upload, xlsx


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


def test_closed_registration_refuses_students_but_not_organisers(client, make_event, make_person, admin_user):
    talk = make_event(title="Closed Talk", registration_enabled=False)
    hack = make_event(title="Closed Hack", kind=Event.Kind.HACKATHON, registration_enabled=False, team_size_max=3)
    student = make_person()
    client.force_login(student)
    for event in (talk, hack):
        page = client.get(reverse("event_detail", args=[event.slug])).content.decode()
        assert "Participants are added by the organisers." in page and reverse("register", args=[event.slug]) not in page
    form = client.post(reverse("register", args=[talk.slug]), {"submit": "1"})
    assert b"Participants are added by the organisers." in form.content and not Registration.objects.filter(event=talk).exists()
    team = client.post(reverse("register", args=[hack.slug]), {"team-team_name": "Rocket", "team-m2_email": "x@example.com", "team-m2_name": "X"})
    assert b"Participants are added by the organisers." in team.content and not Team.objects.filter(event=hack).exists()
    with pytest.raises(RegistrationError):
        register(event=talk, person=student)
    # Organisers still add people: imports and the portal.
    client.force_login(admin_user)
    url = upload(client, hack, "students", xlsx([["Name", "KL ID"], ["Imported", "2300012345"]]))
    choose(client, url, {0: "name", 1: "student_id"}, action="import")
    assert Registration.objects.filter(event=hack, person__student_id="2300012345").exists()
    reg, _ = register(event=talk, person=student, source=Registration.Source.IMPORT)
    assert reg.is_confirmed


def test_registration_switch_toggles_from_the_pages(client, make_event, admin_user, make_person):
    hack = make_event(kind=Event.Kind.HACKATHON)
    client.force_login(admin_user)
    client.post(reverse("event_toggle_registration", args=[hack.slug]), {"open": "0", "next": "/manage/"})
    hack.refresh_from_db()
    assert not hack.registration_enabled
    assert b"Open registration" in client.get(reverse("hackathon_dashboard", args=[hack.slug])).content
    client.force_login(make_person())
    assert client.post(reverse("event_toggle_registration", args=[hack.slug]), {"open": "1"}).status_code == 403


def test_my_events_lists_registered_and_imported_events(client, make_event, make_person):
    student = make_person()
    now = timezone.now()
    soon = make_event(title="Soon Talk", venue="C Block", starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, hours=2))
    later_hack = make_event(title="Later Hack", kind=Event.Kind.HACKATHON, starts_at=now + timedelta(days=9), ends_at=now + timedelta(days=10))
    gone = make_event(title="Old Talk", starts_at=now - timedelta(days=9), ends_at=now - timedelta(days=9) + timedelta(hours=2))
    make_event(title="Not Mine")
    register(event=soon, person=student)
    register(event=later_hack, person=student, source=Registration.Source.IMPORT)  # added by the organisers
    Registration.objects.create(event=gone, person=student)
    client.force_login(student)
    page = client.get(reverse("my_events")).content.decode()
    assert page.index("Soon Talk") < page.index("Later Hack") < page.index("Old Talk") and "Not Mine" not in page
    assert "C Block" in page and reverse("my_hackathon", args=[later_hack.slug]) in page
    assert Registration.objects.get(event=soon, person=student).get_pass_url() in page
    listing = client.get(reverse("event_list")).content.decode()
    assert "All my events" in listing and "My events" in listing


def test_student_hackathon_page_shows_rounds_status_team_and_room_only(client, make_event, make_person):
    hack = make_event(kind=Event.Kind.HACKATHON)
    now = timezone.now()
    round1 = Round.objects.create(event=hack, name="Round 1", position=1, opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2))
    idea = Criterion.objects.create(round=round1, label="Idea", max_marks=10)
    room = Room.objects.create(event=hack, name="L502", capacity=4)
    mine = Team.objects.create(event=hack, name="Rocket", room=room, mentor_name="Prof. Mentor")
    theirs = Team.objects.create(event=hack, name="Comet", room=room)
    me = make_person(full_name="Me Student")
    mate = make_person(full_name="Mate Student")
    other = make_person(full_name="Other Team Student")
    for team, person in ((mine, me), (mine, mate), (theirs, other)):
        reg, _ = register(event=hack, person=person)
        TeamMember.objects.create(team=team, registration=reg)
    judge = services.add_faculty(hack, Person.objects.create_user(email="j@kluniversity.in", full_name="Dr. Judge"))[0]
    a = Assignment.objects.create(round=round1, faculty=judge, team=mine)
    b = Assignment.objects.create(round=round1, faculty=judge, team=theirs)
    hack.show_jury_to_students = True
    hack.save()
    client.force_login(me)
    url = reverse("my_hackathon", args=[hack.slug])
    page = client.get(url).content.decode()
    assert "Round 1" in page and "Pending" in page and "Rocket" in page and "Mate Student" in page and "L502" in page
    for hidden in ("Prof. Mentor", "Dr. Judge", "Comet", "Other Team Student"):
        assert hidden not in page, hidden
    for assignment, mark in ((a, "6.25"), (b, "9.75")):
        Score.objects.create(assignment=assignment, criterion=idea, marks=Decimal(mark))
        assignment.submitted_at = now
        assignment.save()
    page = client.get(url).content.decode()
    assert "Marking done" in page and "6.25" not in page and "9.75" not in page
    round1.release_marks_at = now - timedelta(minutes=1)
    round1.save()
    page = client.get(url).content.decode()
    assert "6.25" in page and "9.75" not in page
