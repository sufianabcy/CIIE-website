from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from events.models import Event
from people.models import Person


@pytest.fixture
def make_person(db):
    counter = {"n": 0}

    def make(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        defaults = {
            "email": f"person{n}@example.com",
            "full_name": f"Person {n}",
            "phone": f"98765432{n:02d}",
            "institution": "Somewhere College",
            "profile_completed_at": timezone.now(),
        }
        defaults.update(kwargs)
        return Person.objects.create_user(**defaults)

    return make


@pytest.fixture
def coordinator(make_person):
    person = make_person(full_name="Coordinator One", is_staff=True)
    person.groups.add(Group.objects.get(name="Coordinator"))
    return person


@pytest.fixture
def make_event(db):
    def make(**kwargs):
        now = timezone.now()
        defaults = {
            "title": "Pitch Day",
            "slug": f"pitch-day-{Event.objects.count() + 1}",
            "starts_at": now + timedelta(days=3),
            "ends_at": now + timedelta(days=3, hours=3),
            "status": Event.Status.PUBLISHED,
        }
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    return make


@pytest.fixture
def event(make_event, coordinator):
    event = make_event()
    event.coordinators.add(coordinator)
    return event
