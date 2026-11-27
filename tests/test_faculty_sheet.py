import pytest
from django.contrib.auth.models import Group

from events.models import Event
from hackathon.models import EventFaculty
from people.models import Person
from tests.test_team_import import upload, xlsx


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


SHEET = [
    ["Sl.No.", "ID No", "Name of the faculty", "Designation", "Contact no ", "Email id"],
    [1, 1001, "Dr. Asha Rao", "PROFESSOR & HoD", 9000000001, "asha.rao@kluniversity.in"],
    [2, 1002, "Mr. Bala K", "Assistant Professor", 9000000002, "bala.k@kluniversity.in"],
    [3, 1003, "Ms. Chitra M", "Associate Professor", 9000000003, "chitra.m@kluniversity.in"],
]


def test_faculty_sheet_as_is_with_one_department_for_everyone(client, make_event, admin_user, make_person):
    hack = make_event(kind=Event.Kind.HACKATHON)
    existing = make_person(email="old@example.com", full_name="Dr. Old Name", employee_id="1002", kind="faculty")
    client.force_login(admin_user)
    url = upload(client, hack, "faculty", xlsx(SHEET))
    page = client.get(url)
    targets = [c["target"] for c in page.context["columns"]]
    assert targets == [None, "employee_id", "name", "designation", "phone", "email"]  # Sl.No. is skipped
    cols = {f"col_{i}": t or "" for i, t in enumerate(targets)}
    client.post(url, {"action": "check", **cols, "const_key_0": "branch", "const_val_0": "MDIE"})
    done = client.post(url, {"action": "import", **cols}, follow=True).content.decode()
    assert "3 faculty added" in done  # added to this hackathon; the existing person is reused, not duplicated
    asha = Person.objects.get(employee_id="1001")
    assert (asha.full_name, asha.designation, asha.phone, asha.email, asha.branch, asha.kind) == (
        "Dr. Asha Rao", "PROFESSOR & HoD", "9000000001", "asha.rao@kluniversity.in", "MDIE", "faculty")
    existing.refresh_from_db()  # matched by employee ID, not duplicated
    assert Person.objects.filter(employee_id="1002").count() == 1 and existing.designation == "Assistant Professor"
    assert EventFaculty.objects.filter(event=hack).count() == 3
    assert Person.objects.get(employee_id="1003").full_name == "Ms. Chitra M"
