import importlib

import pytest
from django.apps import apps
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.urls import reverse

from people.models import Person

ADD = "/admin/people/person/add/"


@pytest.fixture
def portal_admin(make_person):
    person = make_person(full_name="Admin Only", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def superuser(make_person):
    return make_person(full_name="Root", is_staff=True, is_superuser=True)


def _post(client, url, **data):
    base = {"is_active": "on", "temp_password_expires_at_0": "", "temp_password_expires_at_1": ""}
    return client.post(url, {**base, **data})


def test_add_person_asks_student_or_faculty_first(client, portal_admin):
    client.force_login(portal_admin)
    chooser = client.get(ADD).content.decode()
    assert "?type=student" in chooser and "?type=faculty" in chooser and 'name="email"' not in chooser
    faculty = client.get(ADD + "?type=faculty").content.decode()
    assert 'name="employee_id"' in faculty and 'name="kl_email"' in faculty and "Department" in faculty
    for name in ("institution", "year_of_study", "student_id", "kl_verified_at_0"):
        assert f'name="{name}"' not in faculty, name
    student = client.get(ADD + "?type=student").content.decode()
    for name in ("institution", "year_of_study", "student_id", "kl_email", "branch"):
        assert f'name="{name}"' in student, name
    assert 'name="employee_id"' not in student and 'name="kl_verified_at_0"' not in student
    assert 'name="is_coordinator"' in student and "Coordinator (runs" not in student  # a role, not a type


def test_faculty_form_saves_only_faculty_details_and_gives_the_faculty_role(client, portal_admin):
    client.force_login(portal_admin)
    response = _post(client, ADD + "?type=faculty", kind="faculty", email="newfac@kluniversity.in", full_name="Dr. New",
                     phone="9000000001", branch="CSE", employee_id="KLF900", kl_email="newfac@kluniversity.in",
                     institution="Somewhere", year_of_study="3", student_id="999")
    assert response.status_code == 302, response.content[-2000:]
    person = Person.objects.get(email="newfac@kluniversity.in")
    assert (person.kind, person.employee_id, person.branch) == ("faculty", "KLF900", "CSE")
    assert (person.institution, person.year_of_study, person.student_id) == ("", "", "")  # not saved from a faculty form
    assert person.is_faculty and not person.has_usable_password() and person.kl_verified_at is not None
    no_id = _post(client, ADD + "?type=faculty", kind="faculty", email="noid@example.com", full_name="Dr. NoId", employee_id="")
    assert b"Faculty need an employee ID." in no_id.content


def test_student_form_does_not_save_faculty_details(client, portal_admin):
    client.force_login(portal_admin)
    _post(client, ADD + "?type=student", kind="student", email="stu@example.com", full_name="Stu", phone="9000000002",
          institution="KL University", branch="CSE", year_of_study="2", student_id="2300011111", employee_id="KLF1")
    person = Person.objects.get(email="stu@example.com")
    assert (person.kind, person.student_id, person.year_of_study, person.employee_id) == ("student", "2300011111", "2", "")
    assert not person.is_faculty


def test_employee_id_is_unique_ignoring_case(client, portal_admin, make_person):
    make_person(email="a@example.com", employee_id="KLF100", kind="faculty")
    client.force_login(portal_admin)
    response = _post(client, ADD + "?type=faculty", kind="faculty", email="b@example.com", full_name="Dr. B", employee_id="klf100")
    assert b"Someone else already has this employee ID." in response.content
    with pytest.raises(IntegrityError), transaction.atomic():
        make_person(email="c@example.com", employee_id="klf100")


def test_coordinator_and_admin_are_roles_on_any_person(client, portal_admin, superuser, make_person):
    person = make_person(email="role@example.com", kind="student")
    url = reverse("admin:people_person_change", args=[person.pk])
    data = {"kind": "student", "email": person.email, "full_name": person.full_name, "phone": person.phone,
            "institution": person.institution, "branch": "", "year_of_study": "", "kl_email": "", "student_id": ""}
    client.force_login(portal_admin)
    _post(client, url, **data, is_coordinator="on", is_admin="on")
    person.refresh_from_db()
    assert person.is_coordinator and person.is_staff and not person.is_portal_admin  # only superusers grant Admin
    _post(client, url, **data)
    person.refresh_from_db()
    assert not person.groups.exists() and not person.is_staff
    client.force_login(superuser)

    def groups():  # a superuser's form also has the raw groups box, which a browser sends back as it is
        return [g.pk for g in person.groups.all()]

    _post(client, url, **data, is_admin="on", groups=groups())
    person.refresh_from_db()
    assert person.is_portal_admin and person.is_staff
    # Switching type keeps the roles, and a faculty type brings the Faculty role.
    faculty_data = {"kind": "faculty", "email": person.email, "full_name": person.full_name, "phone": person.phone,
                    "branch": "ECE", "employee_id": "KLF555", "kl_email": ""}
    response = _post(client, url, **faculty_data, is_admin="on", is_coordinator="on", groups=groups())
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.kind == "faculty" and set(person.groups.values_list("name", flat=True)) == {"Admin", "Coordinator", "Faculty"}


def test_people_list_has_type_and_role_badges(client, portal_admin, make_person):
    coordinator = make_person(full_name="Coord Person", kind="faculty", employee_id="KLF7")
    coordinator.groups.add(Group.objects.get(name="Coordinator"))
    client.force_login(portal_admin)
    page = client.get(reverse("admin:people_person_changelist") + "?kind__exact=faculty").content.decode()
    assert "Coord Person" in page and "Coordinator</span>" in page and "KLF7" in page
    assert "Admin Only" not in page  # a student, filtered out


def test_migration_gives_everyone_a_type_and_keeps_roles(make_person):
    migration = importlib.import_module("people.migrations.0003_person_kind")
    with_id = make_person(email="x@example.com", employee_id="E1")
    in_group = make_person(email="y@example.com")
    in_group.groups.add(Group.objects.get(name="Faculty"), Group.objects.get(name="Coordinator"))
    student_coordinator = make_person(email="z@example.com")
    student_coordinator.groups.add(Group.objects.get(name="Coordinator"))
    Person.objects.update(kind="student")
    migration.set_kinds(apps, None)
    assert Person.objects.get(pk=with_id.pk).kind == "faculty"
    assert Person.objects.get(pk=in_group.pk).kind == "faculty"
    assert Person.objects.get(pk=student_coordinator.pk).kind == "student"
    assert set(Person.objects.get(pk=in_group.pk).groups.values_list("name", flat=True)) == {"Faculty", "Coordinator"}


def test_faculty_added_on_a_hackathon_get_the_faculty_type(make_person, make_event):
    from events.models import Event
    from hackathon.services import add_faculty

    person = make_person(email="judge@example.com")
    add_faculty(make_event(kind=Event.Kind.HACKATHON), person)
    person.refresh_from_db()
    assert person.kind == "faculty" and person.is_faculty
