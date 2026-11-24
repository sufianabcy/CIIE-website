import pytest
from django.urls import reverse

from checkin.models import CheckIn, ScanLog
from checkin.services import scan, undo, walk_in
from registrations.models import Registration
from registrations.services import register


@pytest.fixture
def registered(event, make_person):
    registration, _ = register(event=event, person=make_person(full_name="Asha Rao", student_id="2300031234"))
    return registration


def test_scan_checks_in_then_says_already(event, registered, coordinator):
    first = scan(event, registered.pass_code_display.lower(), by=coordinator, gate="Gate 1")
    assert first.outcome == ScanLog.Outcome.CHECKED_IN
    second = scan(event, registered.pass_code, by=coordinator, gate="Gate 2")
    assert second.outcome == ScanLog.Outcome.ALREADY
    assert second.checkin.gate == "Gate 1"
    assert ScanLog.objects.filter(event=event).count() == 2


def test_wrong_event_and_unknown_codes(event, make_event, make_person, coordinator):
    other = make_event(title="Other")
    elsewhere, _ = register(event=other, person=make_person())
    assert scan(event, elsewhere.pass_code, by=coordinator).outcome == ScanLog.Outcome.WRONG_EVENT
    assert scan(event, "ZZZZZZZZ", by=coordinator).outcome == ScanLog.Outcome.NOT_FOUND


def test_search_by_name_and_kl_id(event, registered, coordinator):
    assert scan(event, "asha", by=coordinator).matches == [registered]
    assert scan(event, "2300031234", by=coordinator).matches == [registered]
    assert scan(event, "nobody here", by=coordinator).outcome == ScanLog.Outcome.NOT_FOUND


def test_undo_is_recorded_and_allows_a_new_checkin(event, registered, coordinator):
    result = scan(event, registered.pass_code, by=coordinator)
    undo(result.checkin, by=coordinator, reason="Wrong person")
    result.checkin.refresh_from_db()
    assert result.checkin.undone_by == coordinator
    again = scan(event, registered.pass_code, by=coordinator)
    assert again.outcome == ScanLog.Outcome.CHECKED_IN
    assert CheckIn.objects.filter(registration=registered).count() == 2


def test_walk_in_creates_person_registration_and_labelled_checkin(event, coordinator):
    result = walk_in(event, name="Ravi Kumar", email="", kl_id="2300099999", phone="9876500000", kind=CheckIn.Method.WALK_IN, by=coordinator)
    assert result.outcome == ScanLog.Outcome.CHECKED_IN
    assert result.checkin.method == CheckIn.Method.WALK_IN
    registration = result.registration
    assert registration.source == Registration.Source.WALK_IN
    assert registration.person.email == "2300099999@kluniversity.in"
    event.refresh_from_db()
    assert event.seats_taken == 0


def test_door_screen_needs_door_rights(client, event, registered, make_person, coordinator):
    stranger = make_person()
    client.force_login(stranger)
    assert client.get(reverse("door", args=[event.slug])).status_code == 403
    client.force_login(coordinator)
    assert client.get(reverse("door", args=[event.slug])).status_code == 200
    response = client.post(reverse("door_scan", args=[event.slug]), {"q": registered.pass_code, "gate": "Gate 3"})
    assert b"Checked in" in response.content
    assert b"Asha Rao" in response.content


def test_door_team_link_gives_scan_rights(client, event, make_person):
    volunteer = make_person()
    client.force_login(volunteer)
    url = reverse("door_join", args=[event.slug, event.door_token])
    client.post(url)
    assert event.door_team.filter(pk=volunteer.pk).exists()
    assert client.get(reverse("door", args=[event.slug])).status_code == 200
    assert client.get(reverse("door_counts", args=[event.slug])).status_code == 403
