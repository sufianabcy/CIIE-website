from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from events.models import Event
from hackathon import rooms as room_tools
from hackathon import services
from hackathon.models import Assignment, Attendance, Criterion, Room, Round, Score, Team, TeamMember, Volunteer, parse_room_code
from people.models import Person
from registrations.services import register


def xlsx(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "upload.xlsx"
    return buffer


def text_of_xlsx(content):
    wb = load_workbook(BytesIO(content))
    return " ".join(str(c) for ws in wb.worksheets for row in ws.iter_rows(values_only=True) for c in row if c is not None)


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event, make_person):
    """8 teams, 4 faculty, one open round with a rubric."""
    event = make_event(title="Floor Hack", kind=Event.Kind.HACKATHON)
    now = timezone.now()
    r1 = Round.objects.create(event=event, name="Round 1", position=1, opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=3))
    Criterion.objects.create(round=r1, label="Idea", max_marks=10)
    for i in range(1, 9):
        team = Team.objects.create(event=event, name=f"T{i}")
        reg, _ = register(event=event, person=make_person(full_name=f"Member {i}", email=f"m{i}@example.com"))
        TeamMember.objects.create(team=team, registration=reg)
    faculty = []
    for name in "ABCD":
        person = Person.objects.create_user(email=f"{name.lower()}@kluniversity.in", full_name=f"Dr. {name}")
        faculty.append(services.add_faculty(event, person)[0])
    return {"event": event, "round": r1, "faculty": faculty}


def test_room_numbers_give_block_and_floor(hack):
    assert parse_room_code("L502") == ("L", "5", "02")
    assert parse_room_code("R206") == ("R", "2", "06")
    assert parse_room_code("L1002") == ("L", "10", "02")
    assert parse_room_code(" l 502 ") == ("L", "5", "02")
    for bad in ("L12", "502", "Main hall", "L5O2", ""):
        assert parse_room_code(bad) is None, bad
    room = Room.objects.create(event=hack["event"], name=" l1002 ")
    assert (room.name, room.block, room.floor, str(room)) == ("L1002", "L", "10", "L1002")
    assert parse_room_code("s-101") == ("S", "1", "01")
    assert parse_room_code("Main hall") is None
    odd = Room.objects.create(event=hack["event"], name="Main hall")
    assert odd.code_unread and not odd.block
    odd.block, odd.floor = "M", "0"
    odd.save()
    assert not odd.code_unread
    room = Room.objects.create(event=hack["event"], name="R206")
    assert (room.block, room.floor) == ("R", "2")


def test_random_allocation_is_even_avoids_mentors_and_can_copy(hack):
    event, round1, faculty = hack["event"], hack["round"], hack["faculty"]
    t1 = Team.objects.get(event=event, name="T1")
    t1.mentor = faculty[0].person
    t1.save()
    made, _ = room_tools.random_allocate(round1, per_team=1, seed=1)
    assert made == 8
    loads = sorted(Assignment.objects.filter(round=round1, faculty=f).count() for f in faculty)
    assert loads == [2, 2, 2, 2]
    assert not Assignment.objects.filter(round=round1, team=t1, faculty=faculty[0]).exists()
    assert room_tools.random_allocate(round1, per_team=1)[0] == 0  # only unallocated teams by default
    round2 = Round.objects.create(event=event, name="Round 2", position=2)
    room_tools.random_allocate(round2, copy_from=round1)
    assert set(Assignment.objects.filter(round=round2).values_list("faculty", "team")) == set(Assignment.objects.filter(round=round1).values_list("faculty", "team"))


def test_two_judges_per_team_form_panels(hack):
    room_tools.random_allocate(hack["round"], per_team=2, seed=3)
    groups = room_tools.groups_for(hack["round"])
    assert sorted(g.size for g in groups) == [4, 4] and all(len(g.faculty) == 2 for g in groups)


def test_rooms_are_assigned_by_capacity_one_room_per_faculty(client, hack, admin_user):
    event, round1 = hack["event"], hack["round"]
    room_tools.random_allocate(round1, per_team=1, seed=2)
    Room.objects.create(event=event, name="R206", capacity=4)
    Room.objects.create(event=event, name="R207", capacity=4)
    Room.objects.create(event=event, name="S101", capacity=3)
    client.force_login(admin_user)
    url = reverse("hackathon_rooms_plan", args=[event.slug]) + f"?round={round1.pk}"
    preview = client.get(url)
    assert preview.status_code == 200 and b"Save these rooms" in preview.content
    assert not Team.objects.filter(event=event, room__isnull=False).exists()  # preview saves nothing
    client.post(reverse("hackathon_rooms_plan", args=[event.slug]), {"round": round1.pk, "action": "apply"})
    for room in Room.objects.filter(event=event):
        assert room.teams.count() <= room.capacity
    for f in hack["faculty"]:
        assert Assignment.objects.filter(round=round1, faculty=f).values("team__room").distinct().count() == 1
    assert services.room_rule_warnings(event) == []


def test_room_assignment_explains_what_is_short(hack):
    event, round1 = hack["event"], hack["round"]
    a = hack["faculty"][0]
    for team in Team.objects.filter(event=event):
        Assignment.objects.create(round=round1, faculty=a, team=team, source=Assignment.Source.RANDOM)
    Room.objects.create(event=event, name="R206", capacity=4)
    Room.objects.create(event=event, name="R207", capacity=4)
    plan = room_tools.plan_rooms(round1)
    assert plan.errors == ["Dr. A's 8 teams must share one room, but the largest room holds 4. Add a bigger room, or allocate fewer teams to them."]
    with pytest.raises(ValidationError):
        room_tools.apply_plan(round1)
    assert not Team.objects.filter(event=event, room__isnull=False).exists()
    Room.objects.filter(event=event).update(capacity=3)
    assert "8 teams, but the rooms hold 6 in total" in room_tools.plan_rooms(round1).errors[0]


def test_manual_moves_cannot_split_a_faculty_or_overfill(hack):
    event, round1 = hack["event"], hack["round"]
    room_tools.random_allocate(round1, per_team=1, seed=2)
    r206 = Room.objects.create(event=event, name="R206", capacity=4)
    r207 = Room.objects.create(event=event, name="R207", capacity=4)
    Room.objects.create(event=event, name="S101", capacity=3)
    room_tools.apply_plan(round1)
    team = Team.objects.filter(event=event, room=r206).first()
    other = r207 if team.room == r206 else r206
    problems = room_tools.move_problems(team, other)
    assert any("two rooms" in p for p in problems)
    faculty = Assignment.objects.filter(round=round1, team=team).first().faculty
    full = Room.objects.create(event=event, name="R208", capacity=1)
    with pytest.raises(ValidationError, match="holds 1 teams"):
        room_tools.move_group(round1, faculty, full)
    big = Room.objects.create(event=event, name="R209", capacity=10)
    group = room_tools.move_group(round1, faculty, big)
    assert all(Team.objects.get(pk=t.pk).room == big for t in group.teams)


@pytest.fixture
def judged(hack):
    """Rooms assigned, one submitted mark with a distinctive value and remark, and a volunteer."""
    event, round1 = hack["event"], hack["round"]
    room_tools.random_allocate(round1, per_team=1, seed=2)
    Room.objects.create(event=event, name="R206", capacity=4)
    Room.objects.create(event=event, name="R207", capacity=4)
    room_tools.apply_plan(round1)
    a = Assignment.objects.filter(round=round1).first()
    Score.objects.create(assignment=a, criterion=round1.criteria.get(), marks=Decimal("7.75"))
    a.remarks = "Secret remark"
    a.submitted_at = timezone.now()
    a.save()
    person = Person.objects.create_user(email="vol@example.com", full_name="Vol Unteer")
    volunteer = services.add_volunteer(event, person)[0]
    volunteer.rooms.set(Room.objects.filter(event=event))  # volunteers see and mark only their rooms
    return {**hack, "assignment": a, "volunteer": volunteer}


def test_volunteers_never_see_marks(client, judged):
    event, v = judged["event"], judged["volunteer"]
    client.force_login(v.person)
    floors = client.get(reverse("hackathon_floors", args=[event.slug]))
    assert floors.status_code == 200
    body = floors.content.decode()
    assert "done" in body and "pending" in body  # progress, as done / pending
    assert "7.75" not in body and "Secret remark" not in body
    for url in (reverse("hackathon_floors_export", args=[event.slug]), reverse("hackathon_attendance_export", args=[event.slug])):
        response = client.get(url)
        assert response.status_code == 200
        text = text_of_xlsx(response.content)
        assert "7.75" not in text and "Secret remark" not in text
    for url in (reverse("hackathon_marks", args=[event.slug]), reverse("hackathon_dashboard", args=[event.slug]),
                reverse("event_export", args=[event.slug, "marks"]), reverse("event_export", args=[event.slug, "teams"]),
                reverse("hackathon_change_marks", args=[event.slug, judged["assignment"].pk])):
        assert client.get(url).status_code == 403, url
    print_page = client.get(reverse("hackathon_floors", args=[event.slug]) + "?print=1").content.decode()
    assert "7.75" not in print_page and "Secret remark" not in print_page


def test_volunteer_switches_and_other_hackathons(client, judged, make_event):
    event, v = judged["event"], judged["volunteer"]
    team = Team.objects.filter(event=event).first()
    reg = team.members.first().registration
    client.force_login(v.person)
    mark = reverse("hackathon_attendance_mark", args=[event.slug])
    # Student attendance is taken with attendance links now; the old team marking is refused.
    assert client.post(mark, {"round": judged["round"].pk, "team": team.pk, "present": "1", "members": [reg.pk]}).status_code == 404
    assert not Attendance.objects.filter(team=team).exists()
    faculty = judged["faculty"][0]
    client.post(mark, {"round": judged["round"].pk, "faculty": faculty.pk, "present": "1"})
    live = Attendance.objects.get(faculty=faculty, undone_at__isnull=True)
    assert live.present and live.marked_by == v.person
    client.post(reverse("hackathon_attendance_undo", args=[event.slug, live.pk]), {"reason": ""})
    assert Attendance.objects.filter(faculty=faculty, undone_at__isnull=True).exists()  # a reason is required
    client.post(reverse("hackathon_attendance_undo", args=[event.slug, live.pk]), {"reason": "Wrong person"})
    assert not Attendance.objects.filter(faculty=faculty, undone_at__isnull=True).exists()

    v.can_team_attendance = v.can_faculty_attendance = False
    v.save()
    assert client.post(mark, {"round": judged["round"].pk, "faculty": faculty.pk, "present": "1"}).status_code == 403
    v.can_view_progress = False
    v.save()
    assert b"pending" not in client.get(reverse("hackathon_floors", args=[event.slug])).content
    v.can_view_floors = False
    v.save()
    assert client.get(reverse("hackathon_floors", args=[event.slug])).status_code == 403
    assert client.get(reverse("hackathon_floors_export", args=[event.slug])).status_code == 403
    other = make_event(kind=Event.Kind.HACKATHON)
    v.can_view_floors = True
    v.save()
    assert client.get(reverse("hackathon_floors", args=[other.slug])).status_code == 403
    assert b"Floor Hack" in client.get(reverse("staff_home")).content


def test_supervisor_attendance_and_floor_switches(client, judged):
    event = judged["event"]
    sup = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup"))[0]
    client.force_login(sup.person)
    team = Team.objects.filter(event=event).first()
    assert client.get(reverse("hackathon_floors", args=[event.slug])).status_code == 200
    sup.can_take_attendance = False
    sup.can_view_floors = False
    sup.save()
    assert client.get(reverse("hackathon_floors", args=[event.slug])).status_code == 403
    faculty = judged["faculty"][0]
    response = client.post(reverse("hackathon_attendance_mark", args=[event.slug]), {"round": judged["round"].pk, "faculty": faculty.pk, "present": "1"})
    assert response.status_code == 403
    assert client.post(reverse("hackathon_attendance_mark", args=[event.slug]), {"round": judged["round"].pk, "team": team.pk,
                                                                               "present": "1"}).status_code == 404


def test_only_admins_manage_volunteers_and_they_get_logins(client, judged, admin_user):
    event, v = judged["event"], judged["volunteer"]
    sup = services.add_supervisor(event, Person.objects.create_user(email="sup2@example.com", full_name="Sup"))[0]
    client.force_login(sup.person)
    assert client.get(reverse("hackathon_manage_volunteer_add", args=[event.slug])).status_code == 403
    client.force_login(v.person)
    assert client.get(reverse("hackathon_manage_volunteer_edit", args=[event.slug, v.pk])).status_code == 403
    client.force_login(admin_user)
    client.post(reverse("hackathon_manage_volunteer_edit", args=[event.slug, v.pk]), {"v-is_active": "on", "v-can_view_floors": "on"})
    v.refresh_from_db()
    assert v.can_view_floors and not v.can_team_attendance and not v.can_view_progress
    rows = load_workbook(BytesIO(client.post(reverse("hackathon_logins", args=[event.slug]), {
        "which": "new", "expires_at": (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")}).content))["Logins"]
    assert ("Vol Unteer", "Volunteer") in [(r[0], r[1]) for r in rows.iter_rows(values_only=True)]


def _wizard(client, slug, kind, file):
    response = client.post(reverse("hackathon_upload", args=[slug, kind]), {"file": file})
    return client.post(response["Location"], {"action": "import"}, follow=True)


def test_rooms_volunteers_and_member_allocations_import(client, hack, admin_user):
    event, round1 = hack["event"], hack["round"]
    client.force_login(admin_user)
    page = _wizard(client, event.slug, "rooms", xlsx([["Room", "Max teams", "Notes"], ["r 206", 4, "Projector"], ["Main hall", 10, ""], ["R207", "many", ""]]))
    assert b"1 room(s) added" in page.content
    r206 = Room.objects.get(event=event, name="R206")
    assert (r206.capacity, r206.block, r206.floor) == (4, "R", "2")
    assert not Room.objects.filter(event=event, name__icontains="hall").exists()
    _wizard(client, event.slug, "volunteers", xlsx([["Name", "Email", "See marking progress"], ["Vee", "vee@example.com", "No"]]))
    vee = Volunteer.objects.get(event=event, person__email="vee@example.com")
    assert vee.can_team_attendance and not vee.can_view_progress
    _wizard(client, event.slug, "allocations", xlsx([["Round", "Faculty email", "Member email"], ["Round 1", "a@kluniversity.in", "m3@example.com"]]))
    assert Assignment.objects.get(round=round1).team.name == "T3"


def test_exports_have_block_floor_and_room(client, judged, admin_user):
    from events.exports import build_export

    event = judged["event"]
    rows = list(load_workbook(BytesIO(build_export(event, "allocations", admin_user)[1]))["Allocations"].iter_rows(values_only=True))
    assert rows[0][:4] == ("Round", "Block", "Floor", "Room") and rows[1][1] == "R" and rows[1][2] == "2"


def test_room_form_asks_only_number_and_capacity(client, hack, admin_user):
    event = hack["event"]
    client.force_login(admin_user)
    form = client.get(reverse("hackathon_manage_room_add", args=[event.slug])).content.decode()
    assert 'name="name"' in form and 'name="capacity"' in form
    for hidden in ("block", "floor", "building"):
        assert f'name="{hidden}"' not in form
    bad = client.post(reverse("hackathon_manage_room_add", args=[event.slug]), {"name": "Main hall", "capacity": 3})
    assert b"Use a room number like L502" in bad.content
    client.post(reverse("hackathon_manage_room_add", args=[event.slug]), {"name": "l502", "capacity": 3})
    room = Room.objects.get(event=event, name="L502")
    assert (room.block, room.floor, room.capacity) == ("L", "5", 3)
    dupe = client.post(reverse("hackathon_manage_room_add", args=[event.slug]), {"name": "L 502", "capacity": 2})
    assert b"There is already a room L502" in dupe.content
    # Students see just the room number.
    team = Team.objects.filter(event=event).first()
    team.room = room
    team.save()
    student = team.members.first().registration.person
    client.force_login(student)
    page = client.get(reverse("my_hackathon", args=[event.slug])).content.decode()
    assert "<dd>L502</dd>" in page and "Block L" not in page and "Floor 5" not in page
