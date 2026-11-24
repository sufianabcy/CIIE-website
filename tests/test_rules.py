"""The database rules that broke in Excel: duplicates, overbooking, double check-ins."""

import threading

import pytest
from django.db import IntegrityError, connection, transaction

from checkin.models import CheckIn, ScanLog
from events.models import Event
from registrations.models import Registration
from registrations.services import EventFull, cancel, register
from results.models import Result


def test_one_registration_per_person_per_event(event, make_person):
    person = make_person()
    Registration.objects.create(event=event, person=person)
    with pytest.raises(IntegrityError), transaction.atomic():
        Registration.objects.create(event=event, person=person)


def test_register_twice_returns_the_same_registration(event, make_person):
    person = make_person()
    first, created = register(event=event, person=person)
    again, created_again = register(event=event, person=person)
    assert created and not created_again
    assert first.pk == again.pk
    event.refresh_from_db()
    assert event.seats_taken == 1


def test_seat_limit_is_enforced(make_event, make_person):
    event = make_event(seats=1)
    register(event=event, person=make_person())
    with pytest.raises(EventFull):
        register(event=event, person=make_person())
    event.refresh_from_db()
    assert event.seats_taken == 1


def test_database_refuses_overbooking(make_event):
    event = make_event(seats=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        Event.objects.filter(pk=event.pk).update(seats_taken=3)


def test_cancel_frees_the_seat_and_reregistering_takes_it_again(make_event, make_person):
    event = make_event(seats=1)
    person = make_person()
    registration, _ = register(event=event, person=person)
    cancel(registration, by=person)
    event.refresh_from_db()
    assert event.seats_taken == 0
    again, created = register(event=event, person=person)
    assert created and again.pk == registration.pk and again.is_confirmed
    event.refresh_from_db()
    assert event.seats_taken == 1


def test_walk_ins_do_not_take_online_seats(make_event, make_person):
    event = make_event(seats=1)
    register(event=event, person=make_person(), source=Registration.Source.WALK_IN)
    event.refresh_from_db()
    assert event.seats_taken == 0


@pytest.mark.django_db(transaction=True)
def test_last_seat_race_has_one_winner(make_event, make_person):
    event = make_event(seats=1)
    people = [make_person() for _ in range(6)]
    outcomes = []
    barrier = threading.Barrier(len(people))

    def attempt(person):
        barrier.wait()
        try:
            register(event=event, person=person)
            outcomes.append("ok")
        except EventFull:
            outcomes.append("full")
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(p,)) for p in people]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    event.refresh_from_db()
    assert outcomes.count("ok") == 1
    assert event.seats_taken == 1
    assert Registration.objects.filter(event=event).count() == 1


def test_one_active_checkin_per_registration(event, make_person, coordinator):
    registration, _ = register(event=event, person=make_person())
    CheckIn.objects.create(registration=registration, event=event, method="scan", checked_in_by=coordinator)
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckIn.objects.create(registration=registration, event=event, method="search", checked_in_by=coordinator)


def test_checkin_rows_are_never_edited_or_deleted(event, make_person, coordinator):
    registration, _ = register(event=event, person=make_person())
    checkin = CheckIn.objects.create(registration=registration, event=event, method="scan", checked_in_by=coordinator)
    with pytest.raises(Exception), transaction.atomic():
        CheckIn.objects.filter(pk=checkin.pk).update(method="walk_in")
    with pytest.raises(Exception), transaction.atomic():
        CheckIn.objects.filter(pk=checkin.pk).delete()


def test_scan_log_is_append_only(event, coordinator):
    log = ScanLog.objects.create(event=event, raw_input="x", outcome="not_found", scanned_by=coordinator)
    with pytest.raises(Exception), transaction.atomic():
        ScanLog.objects.filter(pk=log.pk).update(raw_input="y")


def test_result_checker_must_differ_from_drafter(event, make_person, coordinator):
    registration, _ = register(event=event, person=make_person())
    result = Result.objects.create(event=event, registration=registration, position="Winner", drafted_by=coordinator)
    result.status = Result.Status.CHECKED
    result.checked_by = coordinator
    with pytest.raises(IntegrityError), transaction.atomic():
        result.save()


def test_result_cannot_be_published_unchecked(event, make_person, coordinator):
    registration, _ = register(event=event, person=make_person())
    with pytest.raises(IntegrityError), transaction.atomic():
        Result.objects.create(
            event=event, registration=registration, position="Winner", drafted_by=coordinator, status="published"
        )


def test_history_records_changes(event, make_person):
    registration, _ = register(event=event, person=make_person())
    registration.answers = {"team": "Rockets"}
    registration.save()
    assert registration.events.count() >= 2  # insert and update, written by triggers
