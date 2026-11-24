from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from events.exports import build_export
from events.models import Event, Question
from hackathon import services
from hackathon.admin import AssignmentAdmin, ScoreInline
from hackathon.models import Assignment, Criterion, EventFaculty, Room, Round, Score, Supervisor, Team, TeamMember
from people.models import Person
from people.staff import MAX_FAILED_SIGNINS, issue_temp_password
from registrations.models import Registration
from registrations.services import register

GOOD_PASSWORD = "river-lantern-42"


def xlsx(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "upload.xlsx"
    return buffer


def sheet(content, name):
    return list(load_workbook(BytesIO(content))[name].iter_rows(values_only=True))


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event):
    event = make_event(title="Hack Day", kind=Event.Kind.HACKATHON)
    Question.objects.create(event=event, key="track", label="Track")
    now = timezone.now()
    r1 = Round.objects.create(event=event, name="Round 1", position=1, opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2))
    r2 = Round.objects.create(event=event, name="Round 2", position=2, opens_at=now + timedelta(days=1))
    Criterion.objects.create(round=r1, label="Idea", max_marks=10, position=1)
    Criterion.objects.create(round=r1, label="Demo", max_marks=20, position=2)
    Criterion.objects.create(round=r2, label="Pitch", max_marks=5, position=1)
    return event


def make_faculty(event, name, **kw):
    person = Person.objects.create_user(email=f"{name.lower()}@kluniversity.in", full_name=f"Dr. {name}", **kw)
    return services.add_faculty(event, person)[0]


@pytest.fixture
def setup(hack, make_person):
    """Two rooms, three teams (two in R1, one in R2), three faculty; Dr. C mentors team Gamma."""
    r1, r2 = Room.objects.create(event=hack, name="R1"), Room.objects.create(event=hack, name="R2")
    a, b, c = (make_faculty(hack, n) for n in ("A", "B", "C"))
    teams = {}
    for name, room in (("Alpha", r1), ("Beta", r1), ("Gamma", r2)):
        team = Team.objects.create(event=hack, name=name, room=room, project_title=f"{name} project")
        reg, _ = register(event=hack, person=make_person(full_name=f"{name} student"))
        TeamMember.objects.create(team=team, registration=reg)
        teams[name] = team
    teams["Gamma"].mentor = c.person
    teams["Gamma"].save()
    rounds = list(Round.objects.filter(event=hack))
    return {"event": hack, "rooms": (r1, r2), "faculty": (a, b, c), "teams": teams, "rounds": rounds}


# --- Event kind -------------------------------------------------------------------


def test_events_are_normal_by_default_and_hackathon_pages_need_a_hackathon(client, event, admin_user):
    assert event.kind == Event.Kind.NORMAL and not event.is_hackathon
    client.force_login(admin_user)
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 404
    assert client.get(reverse("event_export", args=[event.slug, "marks"])).status_code == 404


# --- Imports ----------------------------------------------------------------------


def test_student_import_matches_columns_and_makes_teams_rooms_and_mentors(hack, admin_user):
    mentor = make_faculty(hack, "Mentor", employee_id="KLF9")
    report = services.import_students(hack, xlsx([
        ["S.No", "Student Name", "Email ID", "Mobile", "KL ID", "Dept", "Year", "Team Name", "Project Title", "Problem Statement", "Room No", "Mentor", "Track", "T-shirt"],
        [1, "Asha", "asha@kluniversity.in", 9000000001, 2300011111, "CSE", 3, "Rocket", "Smart bins", "Waste sorting", "Lab 1", "KLF9", "Open", "M"],
        [2, "Ravi", "ravi@example.com", "", "", "ECE", 2, "rocket", "", "", "", "", "", ""],
        [3, "Solo", "", "", 2300033333, "CSE", 4, "", "", "", "Lab 2", "Prof. Outside", "", ""],
        [4, "Nobody", "", "", "", "", "", "Rocket", "", "", "", "", "", ""],
    ]), by=admin_user)
    assert (report.created, report.updated, len(report.problems)) == (3, 0, 1)
    assert "Row 5" in report.problems[0]
    asha = Person.objects.get(email="asha@kluniversity.in")
    assert (asha.full_name, asha.student_id, asha.phone, asha.kl_email) == ("Asha", "2300011111", "9000000001", "asha@kluniversity.in")
    reg = Registration.objects.get(event=hack, person=asha)
    assert reg.source == Registration.Source.IMPORT and reg.answers == {"track": "Open"}
    rocket = Team.objects.get(event=hack, name="Rocket")
    assert rocket.members.count() == 2 and rocket.room.name == "Lab 1"
    assert rocket.project_description == "Waste sorting" and rocket.mentor == mentor.person
    assert reg.team_membership.extra == {"S.No": "1", "T-shirt": "M"}
    solo = Team.objects.get(event=hack, name="Solo")
    assert solo.members.count() == 1 and solo.mentor_name == "Prof. Outside" and solo.room.name == "Lab 2"

    # Uploading again matches people (by email, then KL ID) instead of duplicating them.
    again = services.import_students(hack, xlsx([["KL ID", "Name", "Team"], [2300033333, "Solo", "Rocket"]]))
    assert (again.created, again.updated) == (0, 1)
    assert "moved from Solo to Rocket" in again.warnings[0]
    assert Registration.objects.filter(event=hack).count() == 3


def test_student_import_flags_a_row_whose_email_and_id_are_two_people(hack, make_person):
    make_person(email="one@example.com", student_id="111")
    make_person(email="two@example.com", student_id="222")
    report = services.import_students(hack, xlsx([["Email", "KL ID", "Name"], ["one@example.com", "222", "X"]]))
    assert report.created == 0 and "different people" in report.problems[0]


def test_unreadable_file_and_missing_columns_are_reported(hack):
    with pytest.raises(services.ImportFileError):
        services.import_students(hack, BytesIO(b"not excel"))
    assert services.import_students(hack, xlsx([["Name", "Team"], ["A", "B"]])).problems
    assert services.import_allocations(hack, xlsx([["Round", "Room"], ["1", "R1"]])).problems


def test_faculty_import_creates_accounts_and_updates_by_employee_id(hack):
    report = services.import_staff(hack, xlsx([
        ["Faculty Name", "Email ID", "Emp ID", "Dept", "Cabin"],
        ["Dr. A", "A@KLUniversity.in", "KLF1", "CSE", "C-101"],
        ["Dr. B", "", "KLF2", "ECE", ""],
        ["", "nobody@kluniversity.in", "", "", ""],
    ]), "faculty")
    assert (report.created, report.updated, len(report.problems)) == (2, 0, 1)
    a = Person.objects.get(email="a@kluniversity.in")
    assert a.is_faculty and a.employee_id == "KLF1" and a.branch == "CSE" and not a.has_usable_password()
    assert EventFaculty.objects.get(event=hack, person=a).extra == {"Cabin": "C-101"}
    again = services.import_staff(hack, xlsx([["Name", "Employee ID", "Phone"], ["Dr. B", "KLF2", "9000000000"]]), "faculty")
    assert (again.created, again.updated) == (0, 1)
    assert Person.objects.get(employee_id="KLF2").phone == "9000000000"


def test_only_admins_add_supervisors_and_supervisors_get_admin_access(client, hack, admin_user, make_person):
    upload = xlsx([["Name", "Email", "KL ID", "Type"], ["Stu Dent", "stu@kluniversity.in", "2300099999", "Student"], ["Dr. S", "s@kluniversity.in", "", "Faculty"]])
    client.force_login(admin_user)
    page = client.post(reverse("hackathon_upload", args=[hack.slug, "supervisors"]), {"file": upload})
    assert b"2 supervisor(s) added" in page.content
    student = Supervisor.objects.get(event=hack, person__email="stu@kluniversity.in")
    assert student.kind == Supervisor.Kind.STUDENT and student.person.is_staff and student.person.is_supervisor
    assert not student.person.is_faculty
    assert Person.objects.get(email="s@kluniversity.in").is_faculty

    client.force_login(student.person)
    upload.seek(0)
    assert client.post(reverse("hackathon_upload", args=[hack.slug, "supervisors"]), {"file": upload}).status_code == 403


# --- Staff sign-in ---------------------------------------------------------------


def test_temporary_password_must_be_changed_before_anything_else(client, hack):
    faculty = make_faculty(hack, "A")
    temp = issue_temp_password(faculty.person, timezone.now() + timedelta(days=1))
    faculty.person.refresh_from_db()
    response = client.post(reverse("staff_signin"), {"login": faculty.person.login_id.lower(), "password": temp})
    assert response["Location"] == reverse("password_change")
    assert client.get(reverse("staff_home"))["Location"] == reverse("password_change")
    assert client.get(reverse("my_registrations"))["Location"] == reverse("password_change")
    weak = client.post(reverse("password_change"), {"new_password1": "12345678", "new_password2": "12345678"})
    assert weak.status_code == 200
    client.post(reverse("password_change"), {"new_password1": GOOD_PASSWORD, "new_password2": GOOD_PASSWORD})
    faculty.person.refresh_from_db()
    assert not faculty.person.must_change_password and faculty.person.temp_password_expires_at is None
    assert client.get(reverse("staff_home")).status_code == 200

    # The old temporary password no longer works; the new one does, by email too.
    client.logout()
    assert b"don&#x27;t match" in client.post(reverse("staff_signin"), {"login": faculty.person.login_id, "password": temp}).content
    assert client.post(reverse("staff_signin"), {"login": "A@kluniversity.in", "password": GOOD_PASSWORD})["Location"] == reverse("staff_home")

    # Regenerating forces the change again.
    client.logout()
    temp2 = issue_temp_password(faculty.person, timezone.now() + timedelta(days=1))
    assert client.post(reverse("staff_signin"), {"login": "a@kluniversity.in", "password": temp2})["Location"] == reverse("password_change")


def test_expired_temporary_password_and_lockout(client, hack):
    person = make_faculty(hack, "A").person
    temp = issue_temp_password(person, timezone.now() - timedelta(minutes=1))
    assert b"expired" in client.post(reverse("staff_signin"), {"login": person.email, "password": temp}).content

    temp = issue_temp_password(person, timezone.now() + timedelta(days=1))
    for _ in range(MAX_FAILED_SIGNINS):
        client.post(reverse("staff_signin"), {"login": person.email, "password": "wrong"})
    locked = client.post(reverse("staff_signin"), {"login": person.email, "password": temp})
    assert b"Too many wrong passwords" in locked.content
    assert "_auth_user_id" not in client.session


def test_students_cannot_sign_in_with_a_password(client, make_person):
    student = make_person()
    response = client.post(reverse("staff_signin"), {"login": student.email, "password": "anything"})
    assert b"don&#x27;t match" in response.content


def test_only_admins_make_logins_and_the_excel_holds_them(client, setup, admin_user):
    event = setup["event"]
    supervisor = services.add_supervisor(event, Person.objects.create_user(email="sup@kluniversity.in", full_name="Sup"))[0].person
    url = reverse("hackathon_logins", args=[event.slug])
    data = {"which": "new", "expires_at": (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")}
    client.force_login(supervisor)
    assert client.post(url, data).status_code == 403
    client.force_login(admin_user)
    rows = sheet(client.post(url, data).content, "Logins")
    assert rows[0][:5] == ("Name", "Role", "Email", "Login ID", "Temporary password")
    by_name = {r[0]: r for r in rows[1:]}
    assert set(by_name) == {"Dr. A", "Dr. B", "Dr. C", "Sup"} and by_name["Sup"][1] == "Supervisor"
    person = Person.objects.get(email="a@kluniversity.in")
    assert person.must_change_password and person.check_password(by_name["Dr. A"][4])
    # "new" skips people who already have a password.
    assert client.post(url, data)["Location"] == reverse("hackathon_dashboard", args=[event.slug])


# --- Allocation --------------------------------------------------------------------


def test_room_allocation_one_room_per_round_and_mentors_skipped(setup):
    r1, r2 = setup["rooms"]
    a, b, c = setup["faculty"]
    round1, round2 = setup["rounds"]
    assert services.allocate_room(round1, a, r1) == (2, [])
    assert set(a.assignments.values_list("team__name", flat=True)) == {"Alpha", "Beta"}
    with pytest.raises(services.AllocationError, match="one room per round"):
        services.allocate_room(round1, a, r2)
    assert services.allocate_room(round2, a, r2)[0] == 1  # another round, another room: fine
    made, notes = services.allocate_room(round1, c, r2)
    assert made == 0 and "mentors it" in notes[0]
    # Allocating the same room again adds nothing.
    assert services.allocate_room(round1, a, r1)[0] == 0


def test_model_validation_enforces_room_rule_and_mentor_conflict(setup):
    a, b, c = setup["faculty"]
    round1 = setup["rounds"][0]
    teams = setup["teams"]
    Assignment.objects.create(round=round1, faculty=a, team=teams["Alpha"])
    with pytest.raises(ValidationError, match="one room per round"):
        Assignment(round=round1, faculty=a, team=teams["Gamma"]).full_clean()
    Assignment(round=round1, faculty=a, team=teams["Gamma"], source=Assignment.Source.IMPORT).full_clean()  # imports only warn
    with pytest.raises(ValidationError, match="mentors"):
        Assignment(round=round1, faculty=c, team=teams["Gamma"], source=Assignment.Source.IMPORT).full_clean()


def test_allocation_import_warns_on_two_rooms_and_refuses_mentors(setup):
    event = setup["event"]
    report = services.import_allocations(event, xlsx([
        ["Round", "Faculty Email", "Room", "Team"],
        ["Round 1", "a@kluniversity.in", "R1", ""],
        ["1", "a@kluniversity.in", "", "Gamma"],
        ["Round 1", "c@kluniversity.in", "R2", ""],
        ["Round 9", "b@kluniversity.in", "R1", ""],
        ["Round 1", "nobody@kluniversity.in", "R1", ""],
    ]))
    assert report.created == 3
    assert len(report.warnings) == 1 and "Gamma" in report.warnings[0]
    assert len(report.problems) == 3 and "mentors Gamma" in report.problems[0]
    assert not Assignment.objects.filter(faculty=setup["faculty"][2]).exists()
    assert services.room_rule_warnings(event) == ["Dr. A judges in 2 rooms in Round 1: R1, R2."]
    warnings = sheet(build_export(event, "allocations", None)[1], "Warnings")
    assert warnings[1][0].startswith("Dr. A judges in 2 rooms")


def test_moving_a_team_after_allocation_warns(client, setup, admin_user):
    r1, r2 = setup["rooms"]
    a = setup["faculty"][0]
    services.allocate_room(setup["rounds"][0], a, r1)
    alpha = setup["teams"]["Alpha"]
    assert "two rooms" in services.room_change_warnings(alpha, r2)[0]
    client.force_login(admin_user)
    response = client.post(reverse("hackathon_move_team", args=[setup["event"].slug, alpha.pk]), {"room": r2.pk}, follow=True)
    assert b"two rooms" in response.content
    alpha.refresh_from_db()
    assert alpha.room == r2


def test_allocation_page_refuses_a_second_room(client, setup, admin_user):
    r1, r2 = setup["rooms"]
    a = setup["faculty"][0]
    round1 = setup["rounds"][0]
    url = reverse("hackathon_allocate", args=[setup["event"].slug])
    client.force_login(admin_user)
    client.post(url, {"round": round1.pk, "faculty": a.pk, "room": r1.pk, "action": "allocate"})
    page = client.post(url, {"round": round1.pk, "faculty": a.pk, "room": r2.pk, "action": "allocate"}, follow=True)
    assert b"one room per round" in page.content
    assert a.assignments.count() == 2
    client.post(url, {"round": round1.pk, "faculty": a.pk, "room": r1.pk, "action": "remove"})
    assert a.assignments.count() == 0


# --- Faculty marking ------------------------------------------------------------


def test_faculty_see_only_their_teams_and_their_own_marks(client, setup, make_event):
    r1, r2 = setup["rooms"]
    a, b, c = setup["faculty"]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, r1)
    services.allocate_room(round1, b, r1)
    b_alpha = b.assignments.get(team__name="Alpha")
    Score.objects.create(assignment=b_alpha, criterion=round1.criteria.get(label="Demo"), marks=Decimal("17.25"))
    b_alpha.submitted_at = timezone.now()
    b_alpha.save()

    client.force_login(a.person)
    page = client.get(reverse("hackathon_judge_event", args=[setup["event"].slug]))
    assert b"Alpha" in page.content and b"Beta" in page.content and b"Gamma" not in page.content
    assert b"17.25" not in page.content
    mine = a.assignments.get(team__name="Alpha")
    mark_page = client.get(reverse("hackathon_mark", args=[mine.pk]))
    assert b"Alpha project" in mark_page.content and b"Alpha student" in mark_page.content and b"17.25" not in mark_page.content
    # Someone else's allocation, a team in another room, another event: all 404.
    assert client.get(reverse("hackathon_mark", args=[b_alpha.pk])).status_code == 404
    other = make_event(kind=Event.Kind.HACKATHON)
    assert client.get(reverse("hackathon_judge_event", args=[other.slug])).status_code == 404
    # Faculty are not overseers.
    assert client.get(reverse("hackathon_dashboard", args=[setup["event"].slug])).status_code == 403
    assert client.get(reverse("hackathon_marks", args=[setup["event"].slug])).status_code == 403


def test_marks_only_while_the_round_is_open_then_submit_locks(client, setup):
    a = setup["faculty"][0]
    round1, round2 = setup["rounds"]
    services.allocate_room(round1, a, setup["rooms"][0])
    services.allocate_room(round2, a, setup["rooms"][0])
    idea, demo = round1.criteria.all()
    client.force_login(a.person)

    later = a.assignments.get(round=round2, team__name="Alpha")
    pitch = round2.criteria.get()
    client.post(reverse("hackathon_mark", args=[later.pk]), {f"c_{pitch.pk}": "4", "save": "1"})
    assert not later.scores.exists()  # round 2 is not open yet

    mine = a.assignments.get(round=round1, team__name="Alpha")
    url = reverse("hackathon_mark", args=[mine.pk])
    assert b"less than or equal to 10" in client.post(url, {f"c_{idea.pk}": "11", "save": "1"}).content
    client.post(url, {f"c_{idea.pk}": "8", "save": "1"})
    assert mine.scores.get().marks == Decimal("8")
    assert b"Give marks before submitting" in client.post(url, {f"c_{idea.pk}": "8", "submit": "1"}).content
    client.post(url, {f"c_{idea.pk}": "8", f"c_{demo.pk}": "15.5", "remarks": "Good", "submit": "1"})
    mine.refresh_from_db()
    assert mine.submitted_at and mine.total() == Decimal("23.5")
    client.post(url, {f"c_{idea.pk}": "1", f"c_{demo.pk}": "1", "save": "1"})
    assert mine.total() == Decimal("23.5")

    # After the round closes, drafts can't be changed either.
    beta = a.assignments.get(round=round1, team__name="Beta")
    services.close_round_now(round1)
    client.post(reverse("hackathon_mark", args=[beta.pk]), {f"c_{idea.pk}": "5", "save": "1"})
    assert not beta.scores.exists()
    services.open_round_now(round1)
    client.post(reverse("hackathon_mark", args=[beta.pk]), {f"c_{idea.pk}": "5", "save": "1"})
    assert beta.scores.get().marks == 5


# --- Supervisors and admins -----------------------------------------------------


def test_supervisor_sees_their_hackathon_only_and_cannot_change_marks(client, setup, make_event, admin_user):
    event = setup["event"]
    a = setup["faculty"][0]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    assignment = a.assignments.first()
    Score.objects.create(assignment=assignment, criterion=round1.criteria.first(), marks=Decimal("7.5"))
    supervisor = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup"), Supervisor.Kind.STUDENT)[0].person

    client.force_login(supervisor)
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 200
    marks = client.get(reverse("hackathon_marks", args=[event.slug]))
    assert b"7.5" in marks.content and b"Read-only" in marks.content
    assert client.get(reverse("event_export", args=[event.slug, "marks"])).status_code == 200
    other = make_event(kind=Event.Kind.HACKATHON)
    assert client.get(reverse("hackathon_dashboard", args=[other.slug])).status_code == 403
    # Details yes: round times. Publishing results and marks: no.
    client.post(reverse("hackathon_round_action", args=[event.slug, round1.pk, "close"]))
    round1.refresh_from_db()
    assert not round1.is_open()
    assert client.post(reverse("hackathon_round_action", args=[event.slug, round1.pk, "publish"])).status_code == 403

    request = RequestFactory().get("/")
    request.user = supervisor
    inline = ScoreInline(Assignment, django_admin.site)
    assert not inline.has_change_permission(request) and not inline.has_add_permission(request)
    assignment_admin = AssignmentAdmin(Assignment, django_admin.site)
    assert {"remarks", "submitted_at"} <= set(assignment_admin.get_readonly_fields(request, assignment))
    assert list(assignment_admin.get_queryset(request)) == [assignment] + list(a.assignments.exclude(pk=assignment.pk))
    request.user = admin_user
    assert inline.has_change_permission(request)
    # In the admin a supervisor sees only their hackathon's rows.
    other_team = Team.objects.create(event=other, name="Elsewhere")
    from hackathon.admin import TeamAdmin

    request.user = supervisor
    assert other_team not in TeamAdmin(Team, django_admin.site).get_queryset(request)


def test_admin_changes_a_submitted_mark_in_the_admin(client, setup, admin_user):
    a = setup["faculty"][0]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    assignment = a.assignments.first()
    idea = round1.criteria.get(label="Idea")
    score = Score.objects.create(assignment=assignment, criterion=idea, marks=Decimal("3"))
    assignment.submitted_at = timezone.now()
    assignment.save()
    client.force_login(admin_user)
    url = reverse("admin:hackathon_assignment_change", args=[assignment.pk])
    assert client.get(url).status_code == 200
    response = client.post(url, {
        "round": round1.pk, "faculty": a.pk, "team": assignment.team.pk, "remarks": "fixed",
        "submitted_at_0": "", "submitted_at_1": "",
        "scores-TOTAL_FORMS": "1", "scores-INITIAL_FORMS": "1", "scores-MIN_NUM_FORMS": "0", "scores-MAX_NUM_FORMS": "1000",
        "scores-0-id": score.pk, "scores-0-assignment": assignment.pk, "scores-0-criterion": idea.pk, "scores-0-marks": "9",
    })
    assert response.status_code == 302, response.content[:2000]
    score.refresh_from_db()
    assert score.marks == Decimal("9")


def test_students_and_outsiders_reach_nothing(client, setup, make_person):
    event = setup["event"]
    student = Registration.objects.filter(event=event).first().person
    client.force_login(student)
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 403
    assert client.get(reverse("hackathon_judge_event", args=[event.slug])).status_code == 404
    a = setup["faculty"][0]
    services.allocate_room(setup["rounds"][0], a, setup["rooms"][0])
    assert client.get(reverse("hackathon_mark", args=[a.assignments.first().pk])).status_code == 404
    assert client.get(reverse("event_export", args=[event.slug, "marks"])).status_code == 403


# --- Exports ----------------------------------------------------------------------


def test_marks_export_per_round_with_summaries(setup, admin_user):
    event = setup["event"]
    a, b, _ = setup["faculty"]
    round1 = setup["rounds"][0]
    idea, demo = round1.criteria.all()
    services.allocate_room(round1, a, setup["rooms"][0])
    services.allocate_room(round1, b, setup["rooms"][0])
    for faculty, marks in ((a, (8, 12)), (b, (6, 16))):
        assignment = faculty.assignments.get(team__name="Alpha")
        services.save_marks(assignment, {idea: marks[0], demo: marks[1]}, "ok", submit=True)
    _, content = build_export(event, "marks", admin_user)
    names = load_workbook(BytesIO(content)).sheetnames
    assert names[:5] == ["R1 Round 1 marks", "R1 Round 1 summary", "R2 Round 2 marks", "R2 Round 2 summary", "Overall"]
    rows = sheet(content, "R1 Round 1 marks")
    header = rows[0]
    alpha_a = next(r for r in rows[1:] if r[0] == "Alpha" and r[2] == "Dr. A")
    assert alpha_a[header.index("Idea (/10)")] == 8 and alpha_a[header.index("Total")] == 20 and alpha_a[header.index("Status")] == "Submitted"
    summary = sheet(content, "R1 Round 1 summary")
    assert summary[1][:2] == (1, "Alpha") and summary[1][summary[0].index("Average total")] == 21
    overall = sheet(content, "Overall")
    assert overall[1][1] == "Alpha"
    teams = sheet(build_export(event, "teams", admin_user)[1], "Teams")
    assert ("Gamma", "Gamma project", None, "R2") == teams[[r[0] for r in teams].index("Gamma")][:4]


def test_admin_pages_load_for_admins_and_supervisors(client, setup, admin_user):
    event = setup["event"]
    services.allocate_room(setup["rounds"][0], setup["faculty"][0], setup["rooms"][0])
    supervisor = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup"))[0].person
    team = setup["teams"]["Alpha"]
    pages = [
        reverse("admin:hackathon_round_changelist"), reverse("admin:hackathon_round_change", args=[setup["rounds"][0].pk]),
        reverse("admin:hackathon_team_changelist"), reverse("admin:hackathon_team_change", args=[team.pk]),
        reverse("admin:hackathon_room_changelist"), reverse("admin:hackathon_room_add"),
        reverse("admin:hackathon_assignment_changelist"), reverse("admin:hackathon_assignment_change", args=[Assignment.objects.first().pk]),
        reverse("admin:hackathon_eventfaculty_changelist"), reverse("admin:hackathon_supervisor_changelist"),
    ]
    for person in (admin_user, supervisor):
        client.force_login(person)
        for url in pages:
            assert client.get(url).status_code == 200, (person, url)
    client.force_login(admin_user)
    for url in (reverse("admin:hackathon_round_add") + f"?event={event.pk}", reverse("admin:events_event_change", args=[event.pk]),
                reverse("admin:people_person_change", args=[supervisor.pk]), reverse("hackathon_dashboard", args=[event.slug]),
                reverse("manage")):
        assert client.get(url).status_code == 200, url
    client.force_login(supervisor)
    assert client.get(reverse("admin:hackathon_supervisor_add")).status_code == 403
    assert event.title.encode() in client.get(reverse("manage")).content


def test_seed_demo_builds_the_demo_hackathon_and_can_run_twice(db):
    from django.core.management import call_command

    call_command("seed_demo", "--admin", "boss@example.com", stdout=StringIO())
    call_command("seed_demo", "--admin", "boss@example.com", stdout=StringIO())
    event = Event.objects.get(slug="demo-hackathon")
    assert event.is_hackathon
    assert (event.rooms.count(), event.teams.count(), event.rounds.count(), event.faculty.count(), event.supervisors.count()) == (2, 4, 2, 3, 1)
    assert not Assignment.objects.filter(team__name="Team Pulse", faculty__person__employee_id="KLF102").exists()
    assert services.room_rule_warnings(event) == [] and services.mentor_conflicts(event) == []


# --- Students: My hackathon --------------------------------------------------------


def _student(team):
    return team.members.first().registration.person


def test_student_page_shows_own_team_and_nothing_of_other_teams(client, setup):
    event = setup["event"]
    gamma = setup["teams"]["Gamma"]
    gamma.project_description = "Gamma secret plan"
    gamma.save()
    alpha = setup["teams"]["Alpha"]
    alpha.mentor_name = "Prof. Outside"
    alpha.save()
    client.force_login(_student(alpha))
    page = client.get(reverse("my_hackathon", args=[event.slug]))
    assert page.status_code == 200
    body = page.content
    assert b"Alpha project" in body and b"Alpha student" in body and b"R1" in body and b"Prof. Outside" in body
    assert b"Round 1" in body and b"Round 2" in body
    for other in (b"Beta", b"Gamma", b"Gamma secret plan", b"Beta student"):
        assert other not in body
    # It's linked from My passes.
    assert reverse("my_hackathon", args=[event.slug]).encode() in client.get(reverse("my_registrations")).content


def test_student_page_needs_your_own_confirmed_hackathon_registration(client, setup, event, make_person):
    hack = setup["event"]
    outsider = make_person()
    client.force_login(outsider)
    assert client.get(reverse("my_hackathon", args=[hack.slug])).status_code == 404
    register(event=event, person=outsider)  # a normal event: no hackathon page
    assert client.get(reverse("my_hackathon", args=[event.slug])).status_code == 404
    reg, _ = register(event=hack, person=outsider)
    page = client.get(reverse("my_hackathon", args=[hack.slug]))
    assert b"haven't put you in a team yet" in page.content
    reg.status = Registration.Status.CANCELLED
    reg.save()
    assert client.get(reverse("my_hackathon", args=[hack.slug])).status_code == 404


def test_jury_shows_only_when_switched_on_and_only_for_own_team(client, setup):
    event = setup["event"]
    r1, r2 = setup["rooms"]
    a, b, c = setup["faculty"]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, r1)
    services.allocate_room(round1, b, r2)
    client.force_login(_student(setup["teams"]["Alpha"]))
    url = reverse("my_hackathon", args=[event.slug])
    assert b"Dr. A" not in client.get(url).content
    event.show_jury_to_students = True
    event.save()
    body = client.get(url).content
    assert b"Your jury:</strong> Dr. A" in body and b"Dr. B" not in body


def test_marks_show_only_after_publishing_and_only_own_submitted_marks(client, setup, admin_user):
    event = setup["event"]
    r1, r2 = setup["rooms"]
    a, b, _ = setup["faculty"]
    round1 = setup["rounds"][0]
    idea, demo = round1.criteria.all()
    services.allocate_room(round1, a, r1)
    services.allocate_room(round1, b, r1)
    services.allocate_room(round1, setup["faculty"][0].__class__.objects.create(
        event=event, person=Person.objects.create_user(email="d@kluniversity.in", full_name="Dr. D")), r2)
    services.save_marks(a.assignments.get(team__name="Alpha"), {idea: 8, demo: 12}, "", submit=True)
    services.save_marks(b.assignments.get(team__name="Alpha"), {idea: 6, demo: 15}, "", submit=True)
    services.save_marks(a.assignments.get(team__name="Beta"), {idea: 9.75, demo: 19.25}, "", submit=True)
    gamma = Assignment.objects.get(team__name="Gamma")
    services.save_marks(gamma, {idea: 3.25, demo: 4.75}, "", submit=True)
    client.force_login(_student(setup["teams"]["Alpha"]))
    url = reverse("my_hackathon", args=[event.slug])
    hidden = client.get(url).content
    assert b"Your marks" not in hidden and b"20.5" not in hidden and b"publish" in hidden

    client.force_login(admin_user)
    client.post(reverse("hackathon_round_action", args=[event.slug, round1.pk, "publish"]))
    client.force_login(_student(setup["teams"]["Alpha"]))
    body = client.get(url).content
    assert b"Your marks" in body and b"20.5 / 30" in body and b"7 / 10" in body and b"Average of 2 judges" in body
    for other in (b"9.75", b"19.25", b"29", b"3.25", b"4.75"):
        assert other not in body  # Beta's and Gamma's marks never show

    # Drafts never show, even after publishing.
    client.force_login(_student(setup["teams"]["Beta"]))
    b_beta = b.assignments.get(team__name="Beta")
    services.open_round_now(round1)
    services.save_marks(b_beta, {idea: 1, demo: 1}, "", submit=False)
    beta = client.get(url).content
    assert b"29 / 30" in beta and b"Average of" not in beta


def test_only_admins_publish_results(client, setup, make_person):
    event = setup["event"]
    round1 = setup["rounds"][0]
    sup = services.add_supervisor(event, make_person())[0].person
    client.force_login(sup)
    assert client.post(reverse("hackathon_round_action", args=[event.slug, round1.pk, "publish"])).status_code == 403
    round1.refresh_from_db()
    assert round1.results_published_at is None
