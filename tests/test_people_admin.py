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
    assert b"Someone already uses this employee ID" in response.content
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


def test_an_email_already_on_the_portal_is_a_form_error_not_a_crash(client, portal_admin, make_person):
    existing = make_person(email="test@faculty.com", full_name="Existing One")
    client.force_login(portal_admin)
    for email in ("test@faculty.com", "Test@Faculty.com"):
        response = _post(client, ADD + "?type=faculty", kind="faculty", email=email, full_name="Dr. Copy", employee_id="KLF321")
        assert response.status_code == 200
        assert b"Someone already uses this email" in response.content and b"Existing One" in response.content
    clash = _post(client, ADD + "?type=student", kind="student", email="new@example.com", full_name="New", kl_email="TEST@faculty.com")
    assert b"Someone already uses this email" in clash.content
    assert Person.objects.filter(email__iexact="test@faculty.com").count() == 1
    # Saving someone with their own email again is fine.
    url = reverse("admin:people_person_change", args=[existing.pk])
    ok = _post(client, url, kind="student", email="test@faculty.com", full_name="Existing One", phone=existing.phone,
               institution=existing.institution, branch="", year_of_study="", kl_email="", student_id="")
    assert ok.status_code == 302


# --- No unique clash ever gives a 500 -------------------------------------------------------------


def test_exact_bug_existing_email_in_other_case_is_a_form_error(client, portal_admin, make_person):
    make_person(email="test@facutly.com", full_name="First Faculty", kind="faculty", employee_id="KLF1")
    client.force_login(portal_admin)
    response = _post(client, ADD + "?type=faculty", kind="faculty", email="TEST@facutly.com", full_name="Again", employee_id="KLF2")
    assert response.status_code == 200
    assert b"Someone already uses this email: First Faculty (Faculty)." in response.content and b"Open their page" in response.content


def test_kl_id_clash_is_a_form_error(client, portal_admin, make_person):
    make_person(email="one@example.com", student_id="2300011111", full_name="Has The ID")
    client.force_login(portal_admin)
    response = _post(client, ADD + "?type=student", kind="student", email="two@example.com", full_name="Two", student_id="2300011111")
    assert b"Someone already uses this KL ID: Has The ID (Student)." in response.content


def test_a_race_at_save_time_becomes_a_message(client, portal_admin, monkeypatch):
    from django.contrib.admin import ModelAdmin

    def boom(*args, **kwargs):
        raise IntegrityError("duplicate key value violates unique constraint")

    monkeypatch.setattr(ModelAdmin, "save_model", boom)
    client.force_login(portal_admin)
    response = _post(client, ADD + "?type=student", kind="student", email="race@example.com", full_name="Race")
    assert response.status_code == 302
    page = client.get(response["Location"]).content.decode()
    assert "Not saved: someone else already uses that email" in page


def test_portal_forms_turn_clashes_into_form_errors(client, make_person, make_event):
    from events.models import Event
    from hackathon import services
    from hackathon.models import Room, Team
    from registrations.services import register

    event = make_event(kind=Event.Kind.HACKATHON)
    admin_user = make_person(full_name="Admin", is_staff=True)
    admin_user.groups.add(Group.objects.get(name="Admin"))
    client.force_login(admin_user)
    slug = event.slug
    make_person(email="taken@example.com", student_id="2300099999", employee_id="KLF77")
    # A student whose KL ID belongs to someone else.
    page = client.post(reverse("hackathon_manage_student_add", args=[slug]), {"d-f_name": "New", "d-f_email": "new@example.com", "d-f_student_id": "2300099999", "t-team": ""})
    assert page.status_code == 200 and b"Someone already uses this KL ID" in page.content and not Person.objects.filter(email="new@example.com").exists()
    # Faculty with an employee ID someone already has (in other capitals).
    page = client.post(reverse("hackathon_manage_faculty_add", args=[slug]), {"d-f_name": "Dr. X", "d-f_email": "x@example.com", "d-f_employee_id": "klf77", "a-is_active": "on"})
    assert page.status_code == 200 and b"Someone already uses this Employee ID" in page.content
    # Rooms and teams with names already used (in other capitals).
    Room.objects.create(event=event, name="R206")
    page = client.post(reverse("hackathon_manage_room_add", args=[slug]), {"name": "r206"})
    assert b"Another room already has this name." in page.content
    Team.objects.create(event=event, name="Rocket")
    page = client.post(reverse("hackathon_manage_team_add", args=[slug]), {"name": "ROCKET", "project_title": "", "project_description": "", "room": "", "mentor": "", "mentor_name": ""})
    assert b"Another team already has this name." in page.content
    # Supervisors: a race while saving becomes a message.
    original = services.add_supervisor

    def racing(*args, **kwargs):
        raise IntegrityError("duplicate")

    services.add_supervisor = racing
    try:
        response = client.post(reverse("hackathon_manage_supervisor_add", args=[slug]), {"p-who": "taken@example.com", "s-kind": "faculty", "s-is_active": "on"})
    finally:
        services.add_supervisor = original
    assert response.status_code == 302 and "Not saved" in client.get(response["Location"]).content.decode()
    # Faculty self-registration twice at once: no 500.
    student = make_person(email="stu2@example.com")
    register(event=event, person=student)
    client.force_login(student)
    data = {"full_name": "S", "employee_id": "E55", "department": "CSE", "designation": "AP", "phone": "9000000000"}
    assert client.post(reverse("faculty_register"), data).status_code == 302
    assert client.post(reverse("faculty_register"), data).status_code in (200, 302)


def test_walk_in_desk_uses_the_record_another_desk_just_made(make_person, monkeypatch):
    from checkin import services as door

    # The other desk's record exists, but this desk's first look happened just before it was saved.
    make_person(email="walk@example.com", full_name="Made By Other Desk")
    real_filter = Person.objects.filter
    state = {"tried_create": False}

    def first_look_misses(*args, **kwargs):
        return real_filter(*args, **kwargs) if state["tried_create"] else Person.objects.none()

    def create_clashes(**kwargs):
        state["tried_create"] = True
        raise IntegrityError("duplicate")

    monkeypatch.setattr(Person.objects, "filter", first_look_misses)
    monkeypatch.setattr(Person.objects, "create_user", create_clashes)
    person = door.find_or_create_person(name="Walker", email="walk@example.com")
    assert person.full_name == "Made By Other Desk"
