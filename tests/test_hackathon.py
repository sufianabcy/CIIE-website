import re
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from events.exports import build_export
from events.models import Event, Question
from hackathon import services
from hackathon.models import Assignment, Criterion, EventFaculty, Room, Round, Score, Supervisor, Team, TeamMember
from people.models import Person
from people.staff import MAX_FAILED_SIGNINS, issue_temp_password
from registrations.models import Registration
from registrations.services import register

GOOD_PASSWORD = "river-lantern-42"


def wizard(client, slug, kind, file, mapping=None, action="import"):
    """Go through the import steps: upload, (optionally re-map), then check or import. Returns the last response."""
    response = client.post(reverse("hackathon_upload", args=[slug, kind]), {"file": file})
    if response.status_code != 302 or "/import/" not in response["Location"]:
        return response
    url = response["Location"]
    if action is None:
        return client.get(url)
    data = {"action": action}
    if mapping is not None:
        data.update({f"map_{k}": "" if v is None else v for k, v in mapping.items()})
    return client.post(url, data, follow=True)


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
        [2, "Ravi", "ravi@example.com", "", "2300022222.0", "ECE", 2, "rocket", "", "", "", "", "", ""],
        [3, "Solo", "", "", 2300033333, "CSE", 4, "", "", "", "Lab 2", "Prof. Outside", "", ""],
        [4, "Nobody", "", "", "", "", "", "Rocket", "", "", "", "", "", ""],
    ]), by=admin_user)
    assert (report.created, report.updated, len(report.problems)) == (3, 0, 1)
    assert "Row 5" in report.problems[0]
    # Students sign in with ID@kluniversity.in; the sheet's other address is kept as a contact email.
    asha = Person.objects.get(student_id="2300011111")
    assert (asha.full_name, asha.email, asha.kl_email, asha.contact_email, asha.phone) == (
        "Asha", "2300011111@kluniversity.in", "2300011111@kluniversity.in", "asha@kluniversity.in", "9000000001")
    reg = Registration.objects.get(event=hack, person=asha)
    assert reg.source == Registration.Source.IMPORT and reg.answers == {"track": "Open"}
    rocket = Team.objects.get(event=hack, name="Rocket")
    assert rocket.members.count() == 2 and rocket.room.name == "Lab 1"
    assert rocket.project_description == "Waste sorting" and rocket.mentor == mentor.person
    assert reg.team_membership.extra == {"S.No": "1", "T-shirt": "M"}
    # "2300022222.0" (a number read as text) is the KL ID 2300022222.
    assert Person.objects.get(student_id="2300022222").contact_email == "ravi@example.com"
    # A blank team name means not in a team: no team is made up for Solo.
    assert not Team.objects.filter(event=hack, name="Solo").exists()
    assert not hasattr(Registration.objects.get(event=hack, person__student_id="2300033333"), "team_membership")

    # Uploading again matches people by KL ID instead of duplicating them.
    again = services.import_students(hack, xlsx([["KL ID", "Name", "Team"], [2300033333, "Solo", "Rocket"]]))
    assert (again.created, again.updated) == (0, 1)
    assert rocket.members.count() == 3
    assert Registration.objects.filter(event=hack).count() == 3


def test_student_import_matches_by_kl_id_even_when_the_email_is_someone_elses(hack, make_person):
    make_person(email="one@example.com", student_id="111")
    two = make_person(email="two@example.com", student_id="222")
    people = Person.objects.count()
    report = services.import_students(hack, xlsx([["Email", "KL ID", "Name"], ["one@example.com", "222", "X"]]))
    assert report.problems == [] and Person.objects.count() == people
    assert Registration.objects.get(event=hack).person == two
    two.refresh_from_db()
    assert two.email == "two@example.com" and two.contact_email == ""  # someone else's address is never taken over


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


def test_only_admins_add_supervisors(client, hack, admin_user, make_person):
    upload = xlsx([["Name", "Email", "KL ID", "Type"], ["Stu Dent", "stu@kluniversity.in", "2300099999", "Student"], ["Dr. S", "s@kluniversity.in", "", "Faculty"]])
    client.force_login(admin_user)
    page = wizard(client, hack.slug, "supervisors", upload)
    assert b"2 supervisor(s) added" in page.content
    student = Supervisor.objects.get(event=hack, person__student_id="2300099999")
    assert (student.person.email, student.person.contact_email) == ("2300099999@kluniversity.in", "stu@kluniversity.in")
    assert student.kind == Supervisor.Kind.STUDENT and student.person.is_supervisor
    assert not student.person.is_staff  # supervisors work on the hackathon pages, not in the Django admin
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
    assert client.get(reverse("staff_home"))["Location"] == reverse("password_change") + "?next=%2Ffaculty%2F"
    assert client.get(reverse("my_registrations"))["Location"].startswith(reverse("password_change"))
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


def test_no_password_yet_means_no_password_sign_in(client, make_person):
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


def test_moving_one_team_that_would_split_a_faculty_is_blocked(client, setup, admin_user):
    r1, r2 = setup["rooms"]
    a = setup["faculty"][0]
    services.allocate_room(setup["rounds"][0], a, r1)
    alpha = setup["teams"]["Alpha"]
    assert "two rooms" in services.room_change_warnings(alpha, r2)[0]
    client.force_login(admin_user)
    response = client.post(reverse("hackathon_move_team", args=[setup["event"].slug, alpha.pk]), {"room": r2.pk}, follow=True)
    assert b"not moved" in response.content and b"two rooms" in response.content
    alpha.refresh_from_db()
    assert alpha.room == r1


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

    assert client.get(reverse("hackathon_change_marks", args=[event.slug, assignment.pk])).status_code == 403
    other_team = Team.objects.create(event=other, name="Elsewhere")
    assert client.get(reverse("hackathon_manage_team_edit", args=[other.slug, other_team.pk])).status_code == 403


def test_admin_changes_a_submitted_mark_with_a_recorded_reason(client, setup, admin_user):
    from hackathon.models import MarkChange

    event = setup["event"]
    a = setup["faculty"][0]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    assignment = a.assignments.get(team__name="Alpha")
    idea, demo = round1.criteria.all()
    services.save_marks(assignment, {idea: 3, demo: 10}, "ok", submit=True)
    client.force_login(admin_user)
    url = reverse("hackathon_change_marks", args=[event.slug, assignment.pk])
    assert client.get(url).status_code == 200
    no_reason = client.post(url, {f"c_{idea.pk}": "9", f"c_{demo.pk}": "10", "remarks": "ok", "action": "change"})
    assert no_reason.status_code == 200 and assignment.scores.get(criterion=idea).marks == 3
    client.post(url, {f"c_{idea.pk}": "9", f"c_{demo.pk}": "10", "remarks": "ok", "reason": "Typo by judge", "action": "change"})
    assert assignment.scores.get(criterion=idea).marks == 9
    change = MarkChange.objects.get(assignment=assignment)
    assert (change.changed_by, change.reason, change.before["Idea"], change.after["Idea"]) == (admin_user, "Typo by judge", 3, 9)
    rows = sheet(build_export(event, "marks", admin_user)[1], "R1 Round 1")
    header = next(r for r in rows if r[0] == "Team no")
    row = next(r for r in rows if r[1] == "Alpha")
    assert row[header.index("Idea (/10)")] == 9
    log = sheet(build_export(event, "marks", admin_user)[1], "Mark changes")
    assert [log[1][log[0].index(h)] for h in ("What", "Round", "Team", "Faculty", "Reason")] == [
        "Marks changed", "Round 1", "Alpha", "Dr. A", "Typo by judge"]
    # Reopening is recorded too, and lets the faculty member edit again.
    client.post(url, {f"c_{idea.pk}": "9", f"c_{demo.pk}": "10", "reason": "Judge asked", "action": "reopen"})
    assignment.refresh_from_db()
    assert assignment.submitted_at is None and assignment.changes.first().kind == MarkChange.Kind.REOPENED


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


def test_marks_export_final_rounds_and_rubric(setup, admin_user):
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
    assert names[:5] == ["Final", "R1 Round 1", "R2 Round 2", "Rubric", "Mark changes"]
    rows = sheet(content, "R1 Round 1")
    assert rows[0][0] == "Marking in progress: 2 of 4 marksheets submitted"
    header = rows[1]
    assert header[:5] == ("Team no", "Team", "Room", "Evaluator", "Employee ID")
    alpha_a = next(r for r in rows[2:] if r[1] == "Alpha" and r[header.index("Evaluator")] == "Dr. A")
    assert alpha_a[header.index("Idea (/10)")] == 8 and alpha_a[header.index("Idea band")] == "Adequate"
    assert alpha_a[header.index("Total")] == 20 and alpha_a[header.index("Status")] == "Submitted"
    final = sheet(content, "Final")
    header = final[1]
    alpha = next(r for r in final[2:] if r[header.index("Team")] == "Alpha")
    assert alpha[header.index("Round 1 (out of 100)")] == 70 and alpha[header.index("Rank")] == 1
    assert alpha[header.index("Weighted total (out of 100)")] is None  # Round 2 not marked yet
    teams = sheet(build_export(event, "teams", admin_user)[1], "Teams")
    gamma = teams[[r[1] for r in teams].index("Gamma")]
    assert [gamma[teams[0].index(h)] for h in ("Team", "Project title", "Project description", "Room")] == ["Gamma", "Gamma project", None, "R2"]


def test_portal_pages_load_and_the_admin_has_no_hackathon_models(client, setup, admin_user):
    event = setup["event"]
    services.allocate_room(setup["rounds"][0], setup["faculty"][0], setup["rooms"][0])
    supervisor = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup"))[0].person
    slug = event.slug
    lists = ["students", "teams", "rooms", "faculty", "helpers", "rounds", "allocations", "fields"]
    edits = [
        ("hackathon_manage_team_edit", setup["teams"]["Alpha"].pk), ("hackathon_manage_room_edit", setup["rooms"][0].pk),
        ("hackathon_manage_round_edit", setup["rounds"][0].pk), ("hackathon_manage_faculty_edit", setup["faculty"][0].pk),
        ("hackathon_manage_student_edit", Registration.objects.filter(event=event).first().pk),
    ]
    client.force_login(admin_user)
    for name in lists:
        assert client.get(reverse(f"hackathon_manage_{name}", args=[slug])).status_code == 200, name
    for name, pk in edits + [("hackathon_manage_supervisor_edit", Supervisor.objects.get().pk)]:
        assert client.get(reverse(name, args=[slug, pk])).status_code == 200, name
    for name in ("student", "team", "room", "faculty", "supervisor", "round", "field"):
        assert client.get(reverse(f"hackathon_manage_{name}_add", args=[slug])).status_code == 200, name
    dashboard = client.get(reverse("hackathon_dashboard", args=[slug])).content.decode()
    assert "/admin/hackathon" not in dashboard
    registered = {m._meta.app_label for m in django_admin.site._registry}
    assert "hackathon" not in registered
    # A supervisor with the default switches opens the lists, but not supervisors' or fields' edit pages.
    client.force_login(supervisor)
    for name in lists:
        assert client.get(reverse(f"hackathon_manage_{name}", args=[slug])).status_code == 200, name
    assert client.get(reverse("hackathon_manage_supervisor_add", args=[slug])).status_code == 403
    assert client.get(reverse("hackathon_manage_field_add", args=[slug])).status_code == 403
    assert client.get(reverse("hackathon_manage_faculty_add", args=[slug])).status_code == 403
    assert client.get(reverse("hackathon_manage_team_add", args=[slug])).status_code == 200
    assert event.title.encode() in client.get(reverse("manage")).content

def test_seed_demo_builds_the_demo_hackathon_and_can_run_twice(db):
    from django.core.management import call_command

    call_command("seed_demo", "--admin", "boss@example.com", stdout=StringIO())
    call_command("seed_demo", "--admin", "boss@example.com", stdout=StringIO())
    event = Event.objects.get(slug="demo-hackathon")
    assert event.is_hackathon
    assert (event.rooms.count(), event.teams.count(), event.rounds.count(), event.faculty.count(), event.supervisors.count(),
            event.volunteers.count()) == (3, 9, 2, 4, 1, 1)
    for room in event.rooms.all():
        assert room.capacity and room.teams.count() <= room.capacity
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
    assert b"Alpha project" in body and b"Alpha student" in body and b"R1" in body
    assert b"Prof. Outside" not in body  # the mentor isn't shown to students
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
    assert b"You're not in a team yet" in page.content
    reg.status = Registration.Status.CANCELLED
    reg.save()
    assert client.get(reverse("my_hackathon", args=[hack.slug])).status_code == 404


def test_students_never_see_their_jury_or_any_faculty_name(client, setup):
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
    assert b"Your jury" not in body and b"Dr. A" not in body and b"Dr. B" not in body


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
    assert b'data-testid="round-marks"' not in hidden and b"20.5" not in hidden and b"release them" in hidden

    client.force_login(admin_user)
    client.post(reverse("hackathon_round_action", args=[event.slug, round1.pk, "publish"]))
    client.force_login(_student(setup["teams"]["Alpha"]))
    response = client.get(url)
    body = response.content
    assert b"Your marks" in body and b"20.5 / 30" in body and b"7 / 10" in body and b"Average of 2 judges" in body
    # Only Alpha's own marks: the context holds Alpha's averages, and the marks blocks show nothing of Beta's or Gamma's.
    marks = [r["marks"] for r in response.context["rounds"] if r["marks"]]
    assert len(marks) == 1 and marks[0]["total"] == Decimal("20.5") and marks[0]["judges"] == 2
    blocks = " ".join(re.findall(r'data-testid="round-marks".*?</table>', body.decode(), re.S))
    assert blocks
    for other in ("9.75", "19.25", "29", "3.25", "4.75"):
        assert other not in blocks  # Beta's (29) and Gamma's (8) marks never show

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


# --- Faculty self-registration (Phase 3) --------------------------------------------


def _request_data(event=None, **kw):
    data = {"full_name": "Dr. New", "employee_id": "KLF500", "department": "CSE", "designation": "Assistant Professor", "phone": "9000000500"}
    if event:
        data["event"] = event.pk
    data.update(kw)
    return data


def test_faculty_request_is_pending_with_no_access_until_approved(client, setup, admin_user, make_person):
    from hackathon.models import FacultyRequest
    from mailer.models import OutboxEmail

    event = setup["event"]
    services.allocate_room(setup["rounds"][0], setup["faculty"][0], setup["rooms"][0])
    applicant = make_person(email="new@kluniversity.in")
    assert client.get(reverse("faculty_register")).status_code == 302  # sign in first
    client.force_login(applicant)
    client.post(reverse("faculty_register"), _request_data(event))
    req = FacultyRequest.objects.get(person=applicant)
    assert req.status == FacultyRequest.Status.PENDING and req.event == event
    assert b"Waiting for an admin" in client.get(reverse("faculty_register")).content
    # Pending: still a plain student.
    applicant.refresh_from_db()
    assert not applicant.is_faculty and not EventFaculty.objects.filter(person=applicant).exists()
    assert client.get(reverse("hackathon_judge_event", args=[event.slug])).status_code == 404
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 403
    assert client.get(reverse("hackathon_mark", args=[Assignment.objects.first().pk])).status_code == 404
    # Students can't approve anything, including themselves.
    assert client.get(reverse("faculty_requests")).status_code == 403
    client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "approve"})
    req.refresh_from_db()
    assert req.status == FacultyRequest.Status.PENDING
    # A second request while one is pending is refused.
    client.post(reverse("faculty_register"), _request_data())
    assert FacultyRequest.objects.filter(person=applicant).count() == 1

    client.force_login(admin_user)
    assert b"1 faculty request waiting" in client.get(reverse("hackathon_dashboard", args=[event.slug])).content
    client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "approve", "note": "Welcome"})
    req.refresh_from_db()
    applicant.refresh_from_db()
    assert req.status == FacultyRequest.Status.APPROVED and req.decided_by == admin_user
    assert applicant.is_faculty and applicant.employee_id == "KLF500" and applicant.branch == "CSE"
    role = EventFaculty.objects.get(person=applicant)
    assert role.event == event and applicant.designation == "Assistant Professor"
    # Exactly that: faculty at that event, nothing more.
    assert not applicant.is_supervisor and not applicant.is_staff
    mail = OutboxEmail.objects.filter(to_email="new@kluniversity.in").latest("pk")
    assert "registered as faculty" in mail.subject and "Welcome" in mail.text_body

    client.force_login(applicant)
    assert client.get(reverse("hackathon_judge_event", args=[event.slug])).status_code == 200
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 403
    # They can set a password so the faculty sign-in works too.
    client.post(reverse("password_change"), {"new_password1": GOOD_PASSWORD, "new_password2": GOOD_PASSWORD})
    client.logout()
    assert client.post(reverse("staff_signin"), {"login": "new@kluniversity.in", "password": GOOD_PASSWORD})["Location"] == reverse("staff_home")


def test_rejected_request_stays_a_student_and_gets_an_email(client, admin_user, make_person):
    from hackathon.models import FacultyRequest
    from mailer.models import OutboxEmail

    applicant = make_person(email="maybe@example.com")
    client.force_login(applicant)
    client.post(reverse("faculty_register"), _request_data(employee_id="KLF777"))
    req = FacultyRequest.objects.get(person=applicant)
    client.force_login(admin_user)
    client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "reject", "note": "Not faculty"})
    req.refresh_from_db()
    applicant.refresh_from_db()
    assert req.status == FacultyRequest.Status.REJECTED and not applicant.is_faculty and applicant.employee_id == ""
    assert "Not faculty" in OutboxEmail.objects.filter(to_email="maybe@example.com").latest("pk").text_body
    # They can still set a password like any student, and ask again.
    client.force_login(applicant)
    assert client.get(reverse("password_change")).status_code == 200
    assert b"not approved" in client.get(reverse("faculty_register")).content


def test_duplicate_faculty_requests_are_flagged_not_merged(client, setup, admin_user, make_person):
    from hackathon.models import FacultyRequest

    existing = setup["faculty"][0].person
    existing.employee_id = "KLF500"
    existing.save()
    applicant = make_person(email="copy@example.com")
    client.force_login(applicant)
    client.post(reverse("faculty_register"), _request_data())
    req = FacultyRequest.objects.get(person=applicant)
    client.force_login(admin_user)
    assert b"Possible duplicate" in client.get(reverse("faculty_requests")).content
    page = client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "approve"}, follow=True)
    assert b"Possible duplicate" in page.content
    req.refresh_from_db()
    applicant.refresh_from_db()
    assert req.status == FacultyRequest.Status.PENDING and not applicant.is_faculty
    # "Approve anyway" keeps them separate and never copies the clashing employee ID.
    client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "approve", "anyway": "1"})
    applicant.refresh_from_db()
    assert applicant.is_faculty and applicant.employee_id == ""
    assert Person.objects.filter(employee_id="KLF500").count() == 1


def test_existing_faculty_and_supervisors_cannot_self_register_as_supervisors(client, setup):
    a = setup["faculty"][0].person
    client.force_login(a)
    assert client.get(reverse("faculty_register"))["Location"] == reverse("staff_home")
    # There is no supervisor sign-up at all: supervisor roles come only from admins.
    from django.urls import NoReverseMatch

    with pytest.raises(NoReverseMatch):
        reverse("supervisor_register")


# --- Hackathons have no passes and no door ------------------------------------------


def test_hackathon_registration_goes_to_my_hackathon_not_a_pass(client, hack, make_person):
    from mailer.models import OutboxEmail

    student = make_person(email="reg@example.com")
    client.force_login(student)
    response = client.post(reverse("register", args=[hack.slug]), {"q_track": "Open"})
    assert response["Location"] == reverse("my_hackathon", args=[hack.slug])
    reg = Registration.objects.get(event=hack, person=student)
    # The pass page and its QR send them to My hackathon instead.
    assert client.get(reverse("pass", args=[reg.pass_code]))["Location"] == reverse("my_hackathon", args=[hack.slug])
    for url in (reverse("my_hackathon", args=[hack.slug]), reverse("my_registrations"), reverse("event_detail", args=[hack.slug])):
        body = client.get(url).content
        assert reg.pass_code_display.encode() not in body and reg.get_pass_url().encode() not in body, url
        assert b"Show my pass" not in body and b"My pass<" not in body, url
    mail = OutboxEmail.objects.filter(to_email="reg@example.com").latest("pk")
    assert reg.pass_code_display not in mail.text_body and "/pass/" not in mail.text_body
    assert reverse("my_hackathon", args=[hack.slug]) in mail.text_body
    # Someone else's hackathon registration code is still a 404.
    client.force_login(make_person())
    assert client.get(reverse("pass", args=[reg.pass_code])).status_code == 404


def test_hackathons_have_no_door(client, hack, admin_user, make_person):
    reg, _ = register(event=hack, person=make_person())
    client.force_login(admin_user)
    for name, args in (("door", [hack.slug]), ("door_walk_in", [hack.slug]), ("door_counts", [hack.slug]),
                       ("door_list", [hack.slug]), ("door_join", [hack.slug, hack.door_token])):
        assert client.get(reverse(name, args=args)).status_code == 404, name
    assert client.post(reverse("door_scan", args=[hack.slug]), {"q": reg.pass_code}).status_code == 404
    for kind in ("attendance", "attendance-handoff"):
        assert client.get(reverse("event_export", args=[hack.slug, kind])).status_code == 404
    manage = client.get(reverse("manage")).content.decode()
    assert reverse("door", args=[hack.slug]) not in manage and reverse("door_list", args=[hack.slug]) not in manage
    assert reverse("event_export", args=[hack.slug, "attendance"]) not in manage
    admin_page = client.get(reverse("admin:events_event_change", args=[hack.pk])).content.decode()
    assert "Door-team link" not in admin_page and reverse("door", args=[hack.slug]) not in admin_page
    registrations = sheet(build_export(hack, "registrations", admin_user)[1], "Registrations")
    assert registrations[0][0] == "Registration ID"


def test_normal_events_keep_passes_and_the_door(client, event, admin_user, make_person):
    from mailer.models import OutboxEmail

    student = make_person(email="normal@example.com")
    client.force_login(student)
    response = client.post(reverse("register", args=[event.slug]), {"submit": "1"})
    reg = Registration.objects.get(event=event, person=student)
    assert response["Location"] == reg.get_pass_url()
    page = client.get(reg.get_pass_url())
    assert page.status_code == 200 and reg.pass_code_display.encode() in page.content
    assert b"Show my pass" in client.get(reverse("my_registrations")).content
    assert reg.pass_code_display in OutboxEmail.objects.filter(to_email="normal@example.com").latest("pk").text_body
    client.force_login(admin_user)
    for name in ("door", "door_walk_in", "door_counts", "door_list"):
        assert client.get(reverse(name, args=[event.slug])).status_code == 200, name
    assert client.get(reverse("event_export", args=[event.slug, "attendance"])).status_code == 200
    assert reverse("door", args=[event.slug]) in client.get(reverse("manage")).content.decode()
    assert "Door-team link" in client.get(reverse("admin:events_event_change", args=[event.pk])).content.decode()
    assert sheet(build_export(event, "registrations", admin_user)[1], "Registrations")[0][0] == "Pass code"



# --- Per-supervisor permissions -------------------------------------------------------


def test_permission_labels_and_model_defaults_agree():
    from hackathon.access import PERMISSIONS

    fields = {f.name: f for f in Supervisor._meta.fields}
    for name, (label, help_text, default) in PERMISSIONS.items():
        assert fields[name].default is default and fields[name].verbose_name.lower() == label.lower() and fields[name].help_text == help_text, name


@pytest.fixture
def sup(setup):
    """A supervisor of the setup hackathon with the default switches, plus a way to flip one."""
    person = Person.objects.create_user(email="sup@example.com", full_name="Sup")
    row = services.add_supervisor(setup["event"], person)[0]

    def switch(**values):
        for k, v in values.items():
            setattr(row, k, v)
        row.save()

    return row, switch


def test_default_switches(sup):
    from hackathon.access import DEFAULTS, permissions_for

    row, _ = sup
    assert permissions_for(row.person, row.event) == DEFAULTS
    on = {k for k, v in DEFAULTS.items() if v}
    assert on == {"can_view_marks", "can_edit_teams", "can_edit_allocations", "can_edit_rounds", "can_download",
                  "can_take_attendance", "can_view_floors"}


def test_each_switch_allows_then_forbids(client, setup, sup, admin_user):
    from hackathon.models import FacultyRequest

    event = setup["event"]
    row, switch = sup
    r1 = setup["rooms"][0]
    a, b, _ = setup["faculty"]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, r1)
    assignment = a.assignments.get(team__name="Alpha")
    idea, demo = round1.criteria.all()
    services.save_marks(assignment, {idea: 5, demo: 5}, "", submit=True)
    alpha = setup["teams"]["Alpha"]
    applicant = Person.objects.create_user(email="applicant@example.com", full_name="App")
    FacultyRequest.objects.create(person=applicant, full_name="App", employee_id="X1", department="CSE", designation="AP", phone="9000000000", event=event)
    client.force_login(row.person)
    slug = event.slug
    expiry = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

    actions = {
        "can_view_marks": lambda: client.get(reverse("hackathon_marks", args=[slug])),
        "can_edit_teams": lambda: client.post(reverse("hackathon_move_team", args=[slug, alpha.pk]), {"room": r1.pk}),
        "can_edit_allocations": lambda: client.post(reverse("hackathon_allocate", args=[slug]), {"round": round1.pk, "faculty": b.pk, "room": r1.pk, "action": "allocate"}),
        "can_edit_rounds": lambda: client.post(reverse("hackathon_round_action", args=[slug, round1.pk, "open"])),
        "can_download": lambda: client.get(reverse("event_export", args=[slug, "teams"])),
        "can_change_marks": lambda: client.post(reverse("hackathon_change_marks", args=[slug, assignment.pk]), {f"c_{idea.pk}": "6", f"c_{demo.pk}": "5", "reason": "fix", "action": "change"}),
        "can_reopen_marks": lambda: client.post(reverse("hackathon_change_marks", args=[slug, assignment.pk]), {f"c_{idea.pk}": "6", f"c_{demo.pk}": "5", "reason": "judge asked", "action": "reopen"}),
        "can_publish": lambda: client.post(reverse("hackathon_jury_switch", args=[slug]), {"show": "1"}),
        "can_make_logins": lambda: client.post(reverse("hackathon_logins", args=[slug]), {"which": "all", "expires_at": expiry}),
        "can_import": lambda: wizard(client, slug, "faculty", xlsx([["Name", "Email"], ["Dr. Z", "z@kluniversity.in"]])),
        "can_manage_fields": lambda: client.post(reverse("hackathon_manage_field_add", args=[slug]), {"applies_to": "student", "label": "T-shirt", "kind": "text", "position": 9}),
        "can_approve_requests": lambda: client.get(reverse("faculty_requests")),
        "can_add_faculty": lambda: client.post(reverse("hackathon_add_faculty", args=[slug]), {"who": "applicant@example.com"}),
    }
    for name, act in actions.items():
        switch(**{name: False})
        assert act().status_code == 403, f"{name} off should be 403"
        switch(**{name: True})
        assert act().status_code in (200, 302), f"{name} on should work"
    # The "on" runs really did their thing.
    assert assignment.scores.get(criterion=idea).marks == 6
    assignment.refresh_from_db()
    assert assignment.submitted_at is None  # reopened
    event.refresh_from_db()
    assert event.show_jury_to_students
    assert EventFaculty.objects.filter(event=event, person__email="z@kluniversity.in").exists()
    assert EventFaculty.objects.filter(event=event, person=applicant).exists()


def test_supervisor_password_switch_only_resets_plain_faculty(client, setup, sup, admin_user):
    event = setup["event"]
    row, switch = sup
    switch(can_make_logins=True)
    a = setup["faculty"][0].person
    a.groups.add(Group.objects.get(name="Coordinator"))  # a faculty member with a bigger role
    client.force_login(row.person)
    rows = sheet(client.post(reverse("hackathon_logins", args=[event.slug]), {
        "which": "all", "expires_at": (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
    }).content, "Logins")
    names = {r[0] for r in rows[1:]}
    assert names == {"Dr. B", "Dr. C"}  # not Dr. A (coordinator), not the supervisor, not other supervisors


def test_faculty_request_switch_is_per_hackathon(client, setup, sup, make_event):
    from hackathon.models import FacultyRequest

    row, switch = sup
    switch(can_approve_requests=True)
    other = make_event(kind=Event.Kind.HACKATHON)
    person = Person.objects.create_user(email="elsewhere@example.com", full_name="Elsewhere")
    req = FacultyRequest.objects.create(person=person, full_name="Elsewhere", employee_id="E1", department="X", designation="Y", phone="9000000000", event=other)
    client.force_login(row.person)
    assert b"Elsewhere" not in client.get(reverse("faculty_requests")).content
    assert client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "approve"}).status_code == 404
    req.refresh_from_db()
    assert req.status == FacultyRequest.Status.PENDING


def test_supervisors_cannot_change_their_own_switches(client, setup, sup, admin_user):
    row, _ = sup
    url = reverse("hackathon_manage_supervisor_edit", args=[row.event.slug, row.pk])
    client.force_login(row.person)
    assert client.post(url, {"s-kind": "faculty", "s-is_active": "on", "s-can_change_marks": "on"}).status_code == 403
    row.refresh_from_db()
    assert not row.can_change_marks
    assert client.get(url).status_code == 403
    client.force_login(admin_user)
    assert b'name="s-can_change_marks"' in client.get(url).content
    client.post(url, {"s-kind": "faculty", "s-is_active": "on", "s-can_change_marks": "on"})
    row.refresh_from_db()
    assert row.can_change_marks and not row.can_publish


def test_supervisor_excel_sets_switches(hack):
    report = services.import_staff(hack, xlsx([
        ["Name", "Email", "Type", "Change marks", "Publish results", "Download Excel files"],
        ["Dr. S", "s@kluniversity.in", "Faculty", "Yes", "no", "maybe"],
    ]), "supervisor")
    row = Supervisor.objects.get(person__email="s@kluniversity.in")
    assert row.can_change_marks and not row.can_publish and row.can_download
    assert "isn't yes or no" in report.warnings[0]


def test_coordinators_get_the_default_switches(client, setup, make_person):
    from hackathon.access import DEFAULTS, permissions_for

    coordinator = make_person()
    coordinator.groups.add(Group.objects.get(name="Coordinator"))
    setup["event"].coordinators.add(coordinator)
    assert permissions_for(coordinator, setup["event"]) == DEFAULTS


def test_old_judging_app_is_gone(db):
    from django.apps import apps
    from django.db import connection

    assert not apps.is_installed("judging")
    assert not [t for t in connection.introspection.table_names() if t.startswith("judging")]


# --- Detail fields, import steps and portal editing -----------------------------------------


def test_builtin_fields_come_first_and_name_email_stay_required(client, hack, admin_user):
    from hackathon.fields import ensure_fields
    from hackathon.models import FieldDefinition

    student = ensure_fields(hack, "student")
    assert [d.builtin for d in student] == ["name", "email", "phone", "student_id", "branch", "year"]
    assert [d.builtin for d in ensure_fields(hack, "faculty")] == ["name", "email", "employee_id", "designation", "department", "phone"]
    client.force_login(admin_user)
    email = FieldDefinition.objects.get(event=hack, applies_to="student", builtin="email")
    client.post(reverse("hackathon_manage_field_edit", args=[hack.slug, email.pk]), {"label": "Email ID", "position": 1})
    email.refresh_from_db()
    assert email.label == "Email ID" and email.required  # can't be made optional
    assert client.post(reverse("hackathon_manage_field_delete", args=[hack.slug, email.pk])).status_code == 404
    client.post(reverse("hackathon_manage_field_add", args=[hack.slug]), {
        "applies_to": "student", "label": "T-shirt size", "kind": "choice", "choices_text": "S\nM\nL", "required": "on", "position": 10,
    })
    shirt = FieldDefinition.objects.get(event=hack, key="t-shirt-size")
    assert shirt.choices == ["S", "M", "L"] and shirt.required
    bad = client.post(reverse("hackathon_manage_field_add", args=[hack.slug]), {"applies_to": "student", "label": "X", "kind": "choice", "choices_text": "only", "position": 1})
    assert b"at least two choices" in bad.content


def _shirt_field(event, required=True):
    from hackathon.models import FieldDefinition

    return FieldDefinition.objects.create(event=event, applies_to="student", key="shirt", label="T-shirt size", kind="choice",
                                          choices=["S", "M", "L"], required=required, position=10)


def test_import_auto_matches_by_header_and_shows_columns_by_letter(client, hack, admin_user):
    from hackathon.models import ImportUpload

    _shirt_field(hack)
    client.force_login(admin_user)
    upload = xlsx([["Student Name", "Email ID", "Shoe size", "T-shirt size", "KL ID"], ["Asha", "asha@example.com", "7", "M", "2300011111"]])
    page = wizard(client, hack.slug, "students", upload, action=None)  # just upload, then the matching page
    page = client.get(reverse("hackathon_import", args=[hack.slug, ImportUpload.objects.get().pk]))
    body = page.content.decode()
    assert "What is each column?" in body
    # One row per Excel column, with its letter, name, an example value and the guessed use.
    assert re.search(r"<strong>A</strong></td>\s*<td>Student Name</td>\s*<td[^>]*>Asha</td>", body)
    assert re.search(r'name="col_3".*?<option value="f:shirt" selected>', body, re.S)
    assert [c["target"] for c in page.context["columns"]] == ["name", "email", None, "f:shirt", "student_id"]
    assert "Not matched to any field, so ignored: C (column 3): Shoe size" in body
    item = ImportUpload.objects.get()
    from hackathon import imports

    headers, rows = imports.read(item.content)
    mapping = imports.auto_match(headers, imports.targets(hack, "students"))
    assert (mapping["name"], mapping["email"], mapping["f:shirt"], mapping["student_id"]) == (0, 1, 3, 4)


def test_import_blocks_when_a_required_field_has_no_column(client, hack, admin_user):
    _shirt_field(hack)
    client.force_login(admin_user)
    page = wizard(client, hack.slug, "students", xlsx([["Name", "Email"], ["Asha", "asha@example.com"]]), action="import")
    assert b"\xe2\x80\x9cT-shirt size\xe2\x80\x9d is required" in page.content or b"T-shirt size&quot; is required" in page.content
    assert not Registration.objects.filter(event=hack).exists()


def test_import_row_check_names_row_and_column_then_skips_bad_rows(client, hack, admin_user, make_person):
    from hackathon.models import ImportMapping, ImportUpload, PersonDetails

    _shirt_field(hack)
    make_person(email="one@example.com", student_id="111")
    make_person(email="two@example.com", student_id="222")
    client.force_login(admin_user)
    upload = xlsx([
        ["Name", "Email", "Phone", "KL ID", "T-shirt size"],
        ["Good One", "good@example.com", "9000000001", "2300000101", "M"],
        ["Bad Email", "not-an-email", "9000000002", "2300000102", "S"],
        ["Bad Shirt", "shirt@example.com", "9000000003", "2300000103", "XXL"],
        ["Dupe", "good@example.com", "9000000004", "2300000104", "L"],
        ["Clash", "one@example.com", "9000000005", "333", "L"],
        ["Bad Phone", "phone@example.com", "123", "2300000106", "L"],
        ["", "noname@example.com", "9000000006", "2300000107", "L"],
    ])
    check = wizard(client, hack.slug, "students", upload, action="check").content.decode()
    assert "Row 3, column B (Email): &quot;not-an-email&quot; is not a valid email." in check
    assert "Row 4, column E (T-shirt size): &quot;XXL&quot; is not one of: S, M, L." in check
    assert "Row 5, column B (Email): &quot;good@example.com&quot; is also in row 2." in check
    assert "Row 6: the email one@example.com belongs to" in check and "whose KL ID is 111" in check
    assert "Row 7, column C (Phone)" in check and "Row 8, column A (Name): required, but empty." in check
    assert not Registration.objects.filter(event=hack).exists()  # nothing saved by the check
    assert "Import the 1 valid row and skip the rest" in check

    item = ImportUpload.objects.get()
    done = client.post(reverse("hackathon_import", args=[hack.slug, item.pk]), {"action": "import"}, follow=True).content.decode()
    assert "1 added, 0 updated, 6 rows skipped." in done
    person = Person.objects.get(student_id="2300000101")
    assert person.contact_email == "good@example.com"  # they sign in with their KL email
    assert Registration.objects.filter(event=hack, person=person).exists()
    assert PersonDetails.objects.get(event=hack, person=person).values == {"shirt": "M"}
    skipped = sheet(client.get(reverse("hackathon_import_skipped", args=[hack.slug, item.pk])).content, "Skipped rows")
    assert [r[0] for r in skipped[1:]] == [3, 4, 5, 6, 7, 8] and "not a valid email" in skipped[1][1]
    assert ImportMapping.objects.get(event=hack, kind="students").mapping["f:shirt"] == "T-shirt size"


def test_import_remap_and_remembered_mapping(client, hack, admin_user):
    from hackathon.models import ImportUpload

    client.force_login(admin_user)
    upload = xlsx([["Participant", "Mail", "Roll"], ["Ravi", "ravi@example.com", "2300022222"]])
    first = wizard(client, hack.slug, "students", upload, action="check").content.decode()
    assert "&quot;Name&quot; is required, but no column is matched to it." in first
    item = ImportUpload.objects.get()
    from hackathon import imports

    mapping = {t.key: None for t in imports.targets(hack, "students")}
    mapping.update(name=0, email=1, student_id=2)
    client.post(reverse("hackathon_import", args=[hack.slug, item.pk]), {"action": "import", **{f"map_{k}": "" if v is None else v for k, v in mapping.items()}})
    ravi = Person.objects.get(student_id="2300022222")
    assert (ravi.email, ravi.contact_email) == ("2300022222@kluniversity.in", "ravi@example.com")
    # The next upload with the same headers is matched the same way, without choosing again.
    second = wizard(client, hack.slug, "students", xlsx([["Participant", "Mail", "Roll"], ["Mia", "mia@example.com", "2300033333"]]), action="import")
    assert b"1 added" in second.content and Person.objects.filter(student_id="2300033333", contact_email="mia@example.com").exists()


def test_import_rejects_wrong_files_and_empty_sheets(client, hack, admin_user):
    client.force_login(admin_user)
    wrong = BytesIO(b"name,email\n")
    wrong.name = "list.csv"
    page = client.post(reverse("hackathon_upload", args=[hack.slug, "students"]), {"file": wrong}, follow=True)
    assert b"Upload an .xlsx file" in page.content
    broken = BytesIO(b"not really excel")
    broken.name = "broken.xlsx"
    assert b"could not be read as Excel" in client.post(reverse("hackathon_upload", args=[hack.slug, "students"]), {"file": broken}, follow=True).content
    empty = wizard(client, hack.slug, "students", xlsx([]), action="check")
    assert b"The first sheet is empty." in empty.content


def test_editing_student_details_in_the_portal(client, setup, admin_user):
    from hackathon.models import PersonDetails

    event = setup["event"]
    _shirt_field(event, required=False)
    reg = Registration.objects.filter(event=event, person__full_name="Alpha student").get()
    url = reverse("hackathon_manage_student_edit", args=[event.slug, reg.pk])
    sup = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup"))[0]
    client.force_login(sup.person)
    page = client.get(url)
    assert b'name="d-f_email"' in page.content and b"Only admins can change an email." in page.content
    client.post(url, {"d-f_name": "Alpha Renamed", "d-f_email": "hijack@example.com", "d-f_phone": "9000000111", "d-f_shirt": "L",
                      "t-team": setup["teams"]["Beta"].pk})
    reg.person.refresh_from_db()
    assert reg.person.full_name == "Alpha Renamed" and reg.person.email != "hijack@example.com"
    assert PersonDetails.objects.get(event=event, person=reg.person).values["shirt"] == "L"
    assert reg.person.registrations.get(event=event).team_membership.team.name == "Beta"
    bad = client.post(url, {"d-f_name": "X", "d-f_phone": "12", "t-team": ""})
    assert b"not a phone number" in bad.content


def test_portal_editing_needs_the_right_switch_and_role(client, setup):
    event = setup["event"]
    slug = event.slug
    team = setup["teams"]["Alpha"]
    faculty = setup["faculty"][0].person
    student = Registration.objects.filter(event=event).first().person
    sup = services.add_supervisor(event, Person.objects.create_user(email="sup@example.com", full_name="Sup"))[0]
    sup.can_edit_teams = False
    sup.save()
    for person in (faculty, student):
        client.force_login(person)
        assert client.get(reverse("hackathon_manage_teams", args=[slug])).status_code == 403
        assert client.post(reverse("hackathon_manage_team_delete", args=[slug, team.pk])).status_code == 403
    client.force_login(sup.person)
    assert client.get(reverse("hackathon_manage_teams", args=[slug])).status_code == 200  # can look
    assert client.get(reverse("hackathon_manage_team_edit", args=[slug, team.pk])).status_code == 403
    assert client.post(reverse("hackathon_manage_room_add", args=[slug]), {"name": "R901"}).status_code == 403
    sup.can_edit_teams = True
    sup.save()
    client.post(reverse("hackathon_manage_room_add", args=[slug]), {"name": "r901", "capacity": 4})
    assert Room.objects.filter(event=event, name="R901", floor="9", capacity=4).exists()
    client.post(reverse("hackathon_manage_team_edit", args=[slug, team.pk]), {"name": "Alpha 2", "room": "", "project_title": "", "project_description": "", "mentor": "", "mentor_name": ""})
    team.refresh_from_db()
    assert team.name == "Alpha 2"


def test_rounds_and_rubric_are_edited_in_the_portal(client, setup, admin_user):
    event = setup["event"]
    client.force_login(admin_user)
    data = {"name": "Final", "position": 3, "opens_at": "", "closes_at": "",
            "c-TOTAL_FORMS": "2", "c-INITIAL_FORMS": "0", "c-MIN_NUM_FORMS": "0", "c-MAX_NUM_FORMS": "1000",
            "c-0-position": "1", "c-0-label": "Pitch", "c-0-description": "", "c-0-max_marks": "20",
            "c-1-position": "", "c-1-label": "", "c-1-description": "", "c-1-max_marks": ""}
    response = client.post(reverse("hackathon_manage_round_add", args=[event.slug]), data)
    assert response.status_code == 302, [(f.errors, f.non_field_errors()) for f in response.context["forms"]] + [response.context["formset"].errors]
    final = Round.objects.get(event=event, name="Final")
    assert list(final.criteria.values_list("label", "max_marks")) == [("Pitch", Decimal("20"))]
    dup = client.post(reverse("hackathon_manage_round_add", args=[event.slug]), {**data, "name": "Again"})
    assert b"Another round already has this number." in dup.content
