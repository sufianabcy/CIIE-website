"""Supervisors and volunteers: one tab, one Add form and import, people matched (never duplicated), volunteer rooms
and attendance, supervisor switches in groups with presets. All names, IDs and emails here are made up."""

import re
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from events.models import Event
from hackathon import services
from hackathon.access import DEFAULTS, PERMISSION_GROUPS, PERMISSIONS, PRESETS
from hackathon.models import Assignment, Attendance, Criterion, EventFaculty, Room, Round, Score, Supervisor, Team, TeamMember, Volunteer
from people.models import Person
from registrations.services import register


def xlsx(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "helpers.xlsx"
    return buffer


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin Example", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event, make_person):
    """Two rooms (L502, L503), one open round, a judge per room, a team per room; L502's marks are in (8.25)."""
    event = make_event(title="Helper Hack", kind=Event.Kind.HACKATHON, team_size_min=1, team_size_max=3)
    now = timezone.now()
    round1 = Round.objects.create(event=event, name="Round 1", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=3))
    criterion = Criterion.objects.create(round=round1, label="Idea", max_marks=10)
    rooms = [Room.objects.create(event=event, name=code) for code in ("L502", "L503")]
    teams, judges = [], []
    for i, room in enumerate(rooms, start=1):
        team = Team.objects.create(event=event, name=f"Team {room.name}", room=room)
        reg, _ = register(event=event, person=make_person(full_name=f"Member {room.name}", email=f"member{i}@example.com"))
        TeamMember.objects.create(team=team, registration=reg)
        judge = Person.objects.create_user(email=f"judge{i}@kluniversity.in", full_name=f"Dr. Judge {i}", employee_id=f"EMP10{i}")
        role = services.add_faculty(event, judge)[0]
        services.allocate_room(round1, role, room)
        teams.append(team)
        judges.append(role)
    first = Assignment.objects.get(team=teams[0])
    Score.objects.create(assignment=first, criterion=criterion, marks=Decimal("8.25"))
    first.submitted_at = now
    first.save()
    return {"event": event, "round": round1, "rooms": rooms, "teams": teams, "judges": judges}


def add(client, event, role, **fields):
    data = {"p-role": role, "s-is_active": "on", "v-is_active": "on"}
    data.update({f"p-{k}": v for k, v in fields.items() if k in ("name", "employee_id", "student_id", "email", "phone")})
    data.update({k: v for k, v in fields.items() if k.startswith(("s-", "v-"))})
    return client.post(reverse("hackathon_manage_supervisor_add", args=[event.slug]), data)


# --- 1. Split tabs ------------------------------------------------------------------------------------------


def test_faculty_tab_has_judges_and_one_tab_holds_supervisors_and_volunteers(client, hack, admin_user):
    event = hack["event"]
    sup = Person.objects.create_user(email="sup@example.com", full_name="Sup Example", employee_id="EMP201")
    services.add_supervisor(event, sup)
    vol = Person.objects.create_user(email="2300000301@kluniversity.in", full_name="Vol Example", student_id="2300000301")
    services.add_volunteer(event, vol)
    client.force_login(admin_user)
    faculty = client.get(reverse("hackathon_manage_faculty", args=[event.slug])).content.decode()
    assert "Dr. Judge 1" in faculty and "Sup Example" not in faculty and "Vol Example" not in faculty
    page = client.get(reverse("hackathon_manage_helpers", args=[event.slug])).content.decode()
    assert "Supervisors &amp; volunteers" in page and "<th>Role</th>" in page
    assert "Sup Example" in page and "Vol Example" in page and "Dr. Judge 1" not in page
    only_vol = client.get(reverse("hackathon_manage_helpers", args=[event.slug]) + "?role=volunteer").content.decode()
    assert "Vol Example" in only_vol and "Sup Example" not in only_vol
    only_sup = client.get(reverse("hackathon_manage_helpers", args=[event.slug]) + "?role=supervisor").content.decode()
    assert "Sup Example" in only_sup and "Vol Example" not in only_sup
    assert client.get(reverse("hackathon_manage_volunteers", args=[event.slug]))["Location"].endswith("?role=volunteer")
    nav = client.get(reverse("hackathon_dashboard", args=[event.slug])).content.decode()
    assert "Supervisors &amp; volunteers" in nav and ">Volunteers<" not in nav


# --- 2. Adding: one form, matched people, no duplicates --------------------------------------------------------


def test_the_add_form_reuses_existing_people_and_makes_new_ones_by_their_id(client, hack, admin_user, make_person):
    event = hack["event"]
    judge = hack["judges"][0].person
    student = make_person(email="2300000401@kluniversity.in", full_name="Student Example", student_id="2300000401")
    by_email = make_person(email="mail@example.com", full_name="Mail Example")
    client.force_login(admin_user)
    people = Person.objects.count()
    # A judge here (faculty, matched by employee ID) becomes a supervisor on the same account.
    assert add(client, event, "supervisor", name="Anything", employee_id="emp101").status_code == 302
    # A student anywhere (matched by KL ID) becomes a volunteer on the same account.
    add(client, event, "volunteer", name="Student Example", student_id="2300000401.0", **{"v-rooms": [hack["rooms"][1].pk]})
    # Matched by email when the ID is new to the portal; the ID is filled in.
    add(client, event, "volunteer", name="Mail Example", student_id="2300000402", email="MAIL@example.com")
    assert Person.objects.count() == people
    assert Supervisor.objects.get(event=event).person == judge
    assert set(Volunteer.objects.filter(event=event).values_list("person", flat=True)) == {student.pk, by_email.pk}
    by_email.refresh_from_db()
    assert by_email.student_id == "2300000402"
    # New people: an employee ID makes Faculty, a KL ID a Student who signs in with their KL email.
    add(client, event, "supervisor", name="New Faculty", employee_id="EMP777", email="newfac@example.com")
    add(client, event, "volunteer", name="New Student", student_id="2300000403", email="newstu@example.com")
    new_fac, new_stu = Person.objects.get(employee_id="EMP777"), Person.objects.get(student_id="2300000403")
    assert new_fac.kind == Person.Kind.FACULTY and new_fac.email == "newfac@example.com"
    assert Supervisor.objects.get(person=new_fac).kind == Supervisor.Kind.FACULTY
    assert new_stu.kind == Person.Kind.STUDENT and new_stu.email == "2300000403@kluniversity.in" and new_stu.contact_email == "newstu@example.com"
    # An ID is required, and only one of them.
    none = add(client, event, "volunteer", name="No Id")
    assert "Give their employee ID or their KL ID." in none.content.decode()
    both = add(client, event, "volunteer", name="Both", employee_id="EMP9", student_id="2300000409")
    assert "not both" in both.content.decode()


def test_the_role_shows_in_their_menu_next_time_they_sign_in(client, hack, admin_user):
    event = hack["event"]
    client.force_login(admin_user)
    add(client, event, "supervisor", name="Menu Sup", employee_id="EMP301")
    add(client, event, "volunteer", name="Menu Vol", student_id="2300000501", **{"v-rooms": [hack["rooms"][0].pk]})
    client.force_login(Person.objects.get(employee_id="EMP301"))
    page = client.get(reverse("my_events")).content.decode()
    assert 'data-testid="menu-supervising"' in page and 'data-testid="menu-volunteering"' not in page
    client.force_login(Person.objects.get(student_id="2300000501"))
    page = client.get(reverse("my_events")).content.decode()
    assert 'data-testid="menu-volunteering"' in page and 'data-testid="menu-supervising"' not in page
    assert "L502" in client.get(reverse("staff_home")).content.decode()


def test_the_excel_import_matches_people_and_sets_rooms(client, hack, admin_user, make_person):
    event = hack["event"]
    existing = make_person(email="2300000601@kluniversity.in", full_name="Known Student", student_id="2300000601")
    client.force_login(admin_user)
    start = client.get(reverse("hackathon_import_start", args=[event.slug]) + "?kind=helpers").content.decode()
    assert "Supervisors and volunteers" in start
    response = client.post(reverse("hackathon_upload", args=[event.slug, "helpers"]), {"file": xlsx([
        ["Name", "Role", "Employee ID", "KL ID", "Email", "Rooms", "Change marks"],
        ["Dr. New Sup", "Supervisor", "EMP401", "", "newsup@example.com", "", "Yes"],
        ["Known Student", "volunteer", "", "2300000601", "", "L502, l503", ""],
        ["Lost Room", "Volunteer", "", "2300000602", "", "Z999", ""],
        ["Bad Role", "Judge", "EMP402", "", "", "", ""],
        ["No Id", "Volunteer", "", "", "noid@example.com", "", ""],
    ])})
    check = client.post(response["Location"], {"action": "check"}).content.decode()
    assert "Row 4: no room Z999 in this hackathon." in check and "Row 5: the role &quot;Judge&quot; isn&#x27;t Supervisor or Volunteer." in check
    assert "Row 6: needs an employee ID or a KL ID." in check
    client.post(response["Location"], {"action": "import"})
    sup = Supervisor.objects.get(event=event, person__employee_id="EMP401")
    assert sup.can_change_marks and sup.person.kind == Person.Kind.FACULTY
    vol = Volunteer.objects.get(event=event, person=existing)
    assert sorted(r.name for r in vol.rooms.all()) == ["L502", "L503"]
    assert Person.objects.filter(student_id="2300000601").count() == 1 and not Person.objects.filter(student_id="2300000602").exists()


def test_a_volunteer_never_judges_the_teams_they_take_attendance_for(client, hack, admin_user):
    event, round1, (l502, l503) = hack["event"], hack["round"], hack["rooms"]
    judge = hack["judges"][0]  # judges Team L502
    client.force_login(admin_user)
    refused = add(client, event, "volunteer", name="x", employee_id=judge.person.employee_id, **{"v-rooms": [l502.pk]})
    assert "judges Team L502 in L502, so they can&#x27;t volunteer there." in refused.content.decode()
    assert not Volunteer.objects.filter(person=judge.person).exists()
    # Volunteering in the other room is fine; then they can't be allocated to judge there.
    add(client, event, "volunteer", name="x", employee_id=judge.person.employee_id, **{"v-rooms": [l503.pk]})
    assert Volunteer.objects.get(person=judge.person).rooms.get() == l503
    round2 = Round.objects.create(event=event, name="Round 2", position=2)
    with pytest.raises(ValidationError, match="volunteer in L503"):
        services.allocate_room(round2, judge, l503)
    report = services.import_allocations(event, xlsx([["Round", "Faculty", "Team"], ["Round 2", judge.person.email, "Team L503"]]))
    assert "volunteer in L503" in report.problems[0]
    assert not Assignment.objects.filter(round=round2).exists()
    # If a team they judge is moved into their room later, they still can't mark its attendance.
    team = hack["teams"][0]
    team.room = l503
    team.save()
    client.force_login(judge.person)
    mark = client.post(reverse("hackathon_attendance_mark", args=[event.slug]), {"round": round1.pk, "team": team.pk, "present": "1"})
    assert mark.status_code == 404  # student attendance is taken with attendance links now


def test_email_login_details_works_on_this_tab(client, hack, admin_user, django_capture_on_commit_callbacks):
    event = hack["event"]
    services.add_supervisor(event, Person.objects.create_user(email="s1@example.com", full_name="Sup One", employee_id="EMP501"))
    services.add_volunteer(event, Person.objects.create_user(email="v1@example.com", full_name="Vol One", student_id="2300000701"))
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_helpers", args=[event.slug])).content.decode()
    assert "Email login details" in page and "Email login" in page
    url = reverse("hackathon_login_details", args=[event.slug, "helpers"])
    assert "Send to the 2 " in client.get(url).content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {"scope": "remaining"})
    assert sorted(m.to[0] for m in mail.outbox) == ["s1@example.com", "v1@example.com"]
    assert "Supervising or Volunteering" in mail.outbox[0].body
    again = client.get(url).content.decode()
    assert "2 of 2 already have their login details" in again
    other = services.add_supervisor(event, Person.objects.create_user(email="s2@example.com", full_name="Sup Two",
                                                                      employee_id="EMP502"), switches={"can_make_logins": True})[0]
    client.force_login(other.person)
    assert client.get(url).status_code == 403  # admins only, even with "make faculty passwords"


# --- 3. Volunteers: their rooms only -------------------------------------------------------------------------


def test_volunteers_see_and_mark_only_their_rooms_and_never_marks(client, hack, admin_user):
    event, round1, (l502, l503) = hack["event"], hack["round"], hack["rooms"]
    vol = services.add_volunteer(event, Person.objects.create_user(email="vol@example.com", full_name="Vol Example"))[0]
    client.force_login(vol.person)
    empty = client.get(reverse("hackathon_volunteer_today", args=[event.slug])).content.decode()
    assert 'data-testid="no-rooms"' in empty and "Team L502" not in empty
    vol.rooms.set([l502])
    page = client.get(reverse("hackathon_volunteer_today", args=[event.slug])).content.decode()
    assert "Your rooms" in page and "Team L502" in page and "Member L502" in page and "Dr. Judge 1" in page
    assert "Team L503" not in page and "Member L503" not in page and "Dr. Judge 2" not in page
    assert "done" in page and "8.25" not in page  # whether marks are in, never the marks
    floors = client.get(reverse("hackathon_floors", args=[event.slug])).content.decode()
    assert "Team L503" not in floors
    mark = reverse("hackathon_attendance_mark", args=[event.slug])
    member = hack["teams"][0].members.get().registration
    assert client.post(mark, {"round": round1.pk, "team": hack["teams"][0].pk, "present": "1", "members": [member.pk]}).status_code == 404
    client.post(mark, {"round": round1.pk, "faculty": hack["judges"][0].pk, "present": "0"})
    assert not Attendance.objects.get(faculty=hack["judges"][0]).present
    assert client.post(mark, {"round": round1.pk, "faculty": hack["judges"][1].pk, "present": "1"}).status_code == 403
    vol.can_view_progress = False
    vol.save()
    assert "done" not in client.get(reverse("hackathon_volunteer_today", args=[event.slug])).content.decode().split("Your rooms")[1]
    export = client.get(reverse("hackathon_attendance_export", args=[event.slug]))
    assert b"Team L503" not in export.content
    # Admins and supervisors see every room.
    sup = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup Example"))[0]
    for viewer in (admin_user, sup.person):
        client.force_login(viewer)
        everything = client.get(reverse("hackathon_floors", args=[event.slug])).content.decode()
        assert "Team L502" in everything and "Team L503" in everything


def test_volunteer_rooms_and_switches_are_set_on_the_tab(client, hack, admin_user):
    event, (l502, l503) = hack["event"], hack["rooms"]
    vol = services.add_volunteer(event, Person.objects.create_user(email="vol@example.com", full_name="Vol Example"))[0]
    client.force_login(admin_user)
    url = reverse("hackathon_manage_volunteer_edit", args=[event.slug, vol.pk])
    page = client.get(url).content.decode()
    for label in ("Team attendance", "Faculty attendance", "See marking progress", "See floor lists", "L502", "L503"):
        assert label in page
    client.post(url, {"v-is_active": "on", "v-rooms": [l502.pk, l503.pk], "v-can_team_attendance": "on"})
    vol.refresh_from_db()
    assert set(vol.rooms.all()) == {l502, l503} and vol.can_team_attendance and not vol.can_faculty_attendance and not vol.can_view_progress
    listing = client.get(reverse("hackathon_manage_helpers", args=[event.slug])).content.decode()
    assert "L502, L503" in listing


# --- 4. Supervisors: grouped switches, presets, admins only ---------------------------------------------------


def test_switch_groups_cover_every_switch_once_and_presets_are_as_described():
    grouped = [name for _, names in PERMISSION_GROUPS for name in names]
    assert sorted(grouped) == sorted(PERMISSIONS) and len(grouped) == len(set(grouped))
    assert [title for title, _ in PERMISSION_GROUPS] == ["Teams", "Rooms and allocation", "Rounds and rubric", "Marks", "People", "Day of the event"]
    on = {"can_view_marks", "can_edit_teams", "can_edit_allocations", "can_edit_rounds", "can_download", "can_take_attendance", "can_view_floors"}
    assert {name for name, default in DEFAULTS.items() if default} == on
    assert set(PRESETS["floor"][2]) == {"can_take_attendance", "can_view_floors"}
    assert set(PRESETS["marks"][2]) == {"can_view_marks", "can_change_marks", "can_reopen_marks", "can_download"}
    assert set(PRESETS["full"][2]) == set(PERMISSIONS) - {"can_publish"}


def test_the_supervisor_form_shows_grouped_switches_with_defaults_and_presets(client, hack, admin_user):
    event = hack["event"]
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_supervisor_add", args=[event.slug])).content.decode()
    for title in ("Teams", "Rooms and allocation", "Rounds and rubric", "Marks", "People", "Day of the event"):
        assert f'<legend class="heading-6 mb-2">{title}</legend>' in page
    assert re.search(r'name="s-can_view_marks"[^>]*checked', page) and not re.search(r'name="s-can_change_marks"[^>]*checked', page)
    for label in ("Floor supervisor", "Marks supervisor", "Full helper"):
        assert label in page
    # A preset fills the switches in and saves nothing; single switches can still be changed before saving.
    filled = client.post(reverse("hackathon_manage_supervisor_add", args=[event.slug]),
                         {"preset": "marks", "p-role": "supervisor", "p-name": "Preset Sup", "p-employee_id": "EMP601",
                          "s-is_active": "on", "s-can_edit_teams": "on"}).content.decode()
    assert re.search(r'name="s-can_change_marks"[^>]*checked', filled) and not re.search(r'name="s-can_edit_teams"[^>]*checked', filled)
    assert not Supervisor.objects.filter(event=event).exists()
    switches = {f"s-{n}": "on" for n in PRESETS["marks"][2] if n != "can_reopen_marks"}
    add(client, event, "supervisor", name="Preset Sup", employee_id="EMP601", **switches)
    row = Supervisor.objects.get(event=event)
    assert row.can_change_marks and row.can_view_marks and not row.can_reopen_marks and not row.can_edit_teams
    edit = reverse("hackathon_manage_supervisor_edit", args=[event.slug, row.pk])
    client.post(edit, {"preset": "full", "s-kind": "faculty", "s-is_active": "on"})
    row.refresh_from_db()
    assert not row.can_edit_teams  # still nothing saved by a preset
    client.post(edit, {"s-kind": "faculty", "s-is_active": "on", **{f"s-{n}": "on" for n in PRESETS["full"][2]}})
    row.refresh_from_db()
    assert row.can_edit_teams and row.can_import and not row.can_publish


def test_only_admins_change_supervisors_and_they_see_only_their_hackathon(client, hack, make_event):
    event = hack["event"]
    row = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup Example"))[0]
    other = make_event(title="Other Hack", kind=Event.Kind.HACKATHON)
    client.force_login(row.person)
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 200
    assert client.get(reverse("hackathon_dashboard", args=[other.slug])).status_code == 403
    edit = reverse("hackathon_manage_supervisor_edit", args=[event.slug, row.pk])
    assert client.get(edit).status_code == 403
    assert client.post(edit, {"s-kind": "faculty", "s-is_active": "on", "s-can_publish": "on"}).status_code == 403
    assert client.get(reverse("hackathon_manage_supervisor_add", args=[event.slug])).status_code == 403
    row.refresh_from_db()
    assert not row.can_publish
