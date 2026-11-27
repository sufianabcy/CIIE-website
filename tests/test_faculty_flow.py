"""A faculty member marks from start to finish: added by Excel and by Add, rubric imported from a file, round opened,
teams formed by team number, faculty allocated, login details emailed, first sign-in with a temporary password,
marking (draft, then submit, then locked), and the admin's marks, advancing and final Excel.

Also: a faculty member who registered themselves and was approved, one who is also a student, and what faculty
can never reach. All people, IDs and text are made up; the rubric is tests/rubric_sample.json.
"""

import re
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from events.models import Event
from hackathon import rubric_file, services
from hackathon.models import Assignment, EventFaculty, FacultyRequest, Room, Round, RoundOutcome, Score, Team, TeamMember
from people.models import Person
from registrations.services import register

SAMPLE = Path(__file__).with_name("rubric_sample.json")
NEW_PASSWORD = "Quiet-Harbour-Lamp-71"
PROMPTS = ["Sample prompt: a good point", "Sample prompt: a next step"]


def xlsx(rows, name="upload.xlsx"):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = name
    return buffer


def run_import(client, event, kind, file, **extra):
    response = client.post(reverse("hackathon_import_start", args=[event.slug]), {"kind": kind, "file": file})
    assert response.status_code == 302, response.content[:300]
    return client.post(response["Location"], {"action": "import", **extra}, follow=True)


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin Example", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def event(make_event):
    return make_event(title="Flow Hack", kind=Event.Kind.HACKATHON, team_size_min=1, team_size_max=4)


def emailed_passwords(capture, client, url, data):
    before = len(mail.outbox)
    with capture(execute=True):
        client.post(url, data)
    return {m.to[0]: re.search(r"Temporary password: (\S+)", m.body).group(1) for m in mail.outbox[before:]}


def first_sign_in(client, email, password):
    """Sign in with the emailed temporary password, choose a new one, and return the page landed on."""
    client.logout()
    response = client.post(reverse("signin"), {"login": email, "password": password})
    assert response["Location"].startswith(reverse("password_change"))
    response = client.post(reverse("password_change"), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    return response


def marks_post(assignment, values, submit=False, **extra):
    criteria = list(assignment.round.criteria.all())
    data = {f"c_{c.pk}": v for c, v in zip(criteria, values)}
    data.update({"comment_0": "A clear sample point", "comment_1": "A sample next step", "remarks": ""})
    data.update(extra)
    data["submit" if submit else "save"] = "1"
    return data


def test_faculty_mark_from_start_to_finish(client, event, admin_user, django_capture_on_commit_callbacks):
    client.force_login(admin_user)

    # 1. Faculty by Excel and by Add; the rubric from the made-up file; Round 1 opened.
    run_import(client, event, "faculty", xlsx([
        ["Name", "Email", "Employee ID", "Department", "Phone"],
        ["Dr. Asha Judge", "asha.judge@kluniversity.in", "KLF1001", "CSE", "9000001001"],
    ]))
    client.post(reverse("hackathon_manage_faculty_add", args=[event.slug]), {
        "d-f_name": "Dr. Bala Judge", "d-f_email": "bala.judge@kluniversity.in", "d-f_employee_id": "KLF1002", "a-is_active": "on",
    })
    asha, bala = (EventFaculty.objects.get(event=event, person__email=e) for e in ("asha.judge@kluniversity.in", "bala.judge@kluniversity.in"))
    assert asha.person.employee_id == "KLF1001" and bala.person.employee_id == "KLF1002"
    page = client.post(reverse("hackathon_rubric_import", args=[event.slug]),
                       {"action": "preview", "file": SAMPLE.open("rb")}).content.decode()
    assert "Import the rubric" in page
    client.post(reverse("hackathon_rubric_import", args=[event.slug]),
                {"action": "import", "content": SAMPLE.read_text(), "confirm": "1"})
    round1, round2, round3 = Round.objects.filter(event=event)
    assert round1.name == "Sample round one" and not round1.is_open()
    client.post(reverse("hackathon_round_action", args=[event.slug, round1.pk, "open"]))
    round1.refresh_from_db()
    assert round1.is_open() and not round2.is_open()

    # 2. Teams by team number, with rooms; faculty allocated to rooms (and so to teams).
    run_import(client, event, "students", xlsx([
        ["Id", "Name", "Team No", "Name of the Team", "Title of the Project", "Category", "Room"],
        [2500040001.0, "Student One", 1, "Rockets", "Sample bins", "Green", "L502"],
        [2500040002.0, "Student Two", 1, "", "", "", "L502"],
        [2500040003.0, "Student Three", 2, "", "Sample booking", "Campus", "L503"],
        [2500040004.0, "Student Four", 2, "", "", "", "L503"],
    ]))
    rockets, team2 = Team.objects.get(event=event, number="1"), Team.objects.get(event=event, number="2")
    assert rockets.name == "Rockets" and team2.name == "Team 2" and rockets.room.name == "L502"
    l502, l503 = Room.objects.get(event=event, name="L502"), Room.objects.get(event=event, name="L503")
    for faculty, room in ((asha, l502), (bala, l503)):
        client.post(reverse("hackathon_allocate", args=[event.slug]), {"round": round1.pk, "faculty": faculty.pk, "room": room.pk})
    assert list(asha.assignments.values_list("team__name", flat=True)) == ["Rockets"]
    assert list(bala.assignments.values_list("team__name", flat=True)) == ["Team 2"]

    # 3. Login details are emailed; the first sign-in asks for a new password, then lands on the faculty home.
    passwords = emailed_passwords(django_capture_on_commit_callbacks, client,
                                  reverse("hackathon_login_details", args=[event.slug, "faculty"]), {"scope": "remaining"})
    assert set(passwords) == {"asha.judge@kluniversity.in", "bala.judge@kluniversity.in"}
    response = first_sign_in(client, "asha.judge@kluniversity.in", passwords["asha.judge@kluniversity.in"])
    assert response["Location"] == reverse("staff_home")
    home = client.get(response["Location"]).content.decode()
    assert "Judging" in home and "Flow Hack" in home

    # 4. Only their own teams; the marksheet shows everything it should.
    judge_page = client.get(reverse("hackathon_judge_event", args=[event.slug])).content.decode()
    assert "Rockets" in judge_page and "Team 2" not in judge_page
    assignment = asha.assignments.get()
    url = reverse("hackathon_mark", args=[assignment.pk])
    sheet = client.get(url).content.decode()
    for text in ("Sample guidance for judges", "Sample strong A", "Sample weak B", "Sample focus one", "Sample question one?",
                 *PROMPTS, "Sample flag one.", "Student self-score (out of 20)", "Sample meaning for the top band", "data-band"):
        assert text in sheet, text
    # The live bands script runs in the page body, not inside the <title>.
    assert sheet.index('id="bands"') > sheet.index("</title>") and "function band(" in sheet.split("</title>", 1)[1]
    assert "Sample: keep going" not in sheet  # recommendations are only for the final round

    client.post(url, marks_post(assignment, ["48", "30"], red_flags="Sample flag one.", self_score="15"))
    assignment.refresh_from_db()
    assert assignment.submitted_at is None and assignment.self_score == 15 and assignment.red_flags == ["Sample flag one."]
    assert sorted(Score.objects.filter(assignment=assignment).values_list("marks", flat=True)) == [30, 48]
    assert assignment.comments == {PROMPTS[0]: "A clear sample point", PROMPTS[1]: "A sample next step"}
    response = client.post(url, marks_post(assignment, ["50", "30"], submit=True, self_score="15"), follow=True)
    assert "submitted" in response.content.decode()
    assignment.refresh_from_db()
    assert assignment.submitted_at is not None and assignment.total() == 80
    locked = client.get(url).content.decode()
    assert "Submitted" in locked and "<fieldset disabled>" in locked
    client.post(url, marks_post(assignment, ["10", "10"], submit=True))
    assert assignment.total() == 80  # locked: unchanged

    # Bala signs in the same way and marks Team 2.
    first_sign_in(client, "bala.judge@kluniversity.in", passwords["bala.judge@kluniversity.in"])
    other = bala.assignments.get()
    client.post(reverse("hackathon_mark", args=[other.pk]), marks_post(other, ["30", "20"], submit=True))
    other.refresh_from_db()
    assert other.is_submitted

    # 5. The admin sees the marks, advances teams and downloads the final Excel with the marks in it.
    client.force_login(admin_user)
    marks_page = client.get(reverse("hackathon_marks", args=[event.slug])).content.decode()
    assert "Dr. Asha Judge" in marks_page and "Rockets" in marks_page
    from hackathon import rubric
    from hackathon.models import RubricSettings

    assert not rubric.eliminating(event)  # by default every team goes through; this hackathon eliminates
    RubricSettings.objects.update_or_create(event=event, defaults={"eliminate_teams": True})
    advance_url = reverse("hackathon_advance", args=[event.slug, round1.pk])
    advance_page = client.get(advance_url).content.decode()
    assert "Rockets" in advance_page and "Team 2" in advance_page
    client.post(advance_url, {"confirm": "1", "advance": [rockets.pk]})
    assert RoundOutcome.objects.get(round=round1, team=rockets).advanced
    assert not RoundOutcome.objects.get(round=round1, team=team2).advanced
    response = client.get(reverse("event_export", args=[event.slug, "marks"]))
    assert response.status_code == 200
    final = list(load_workbook(BytesIO(response.content))["Final"].iter_rows(values_only=True))
    header = final[0]
    by_team = {r[header.index("Team")]: r for r in final[1:]}
    assert by_team["Rockets"][header.index("Round 1 (out of 100)")] == 80
    assert by_team["Team 2"][header.index("Round 1 (out of 100)")] == 50
    assert by_team["Rockets"][header.index("Rank")] == 1 and by_team["Rockets"][header.index("Team no")] == "1"
    assert by_team["Rockets"][header.index("Red flags")] == 0  # the draft's flag was unticked before submitting


@pytest.fixture
def ready(event, admin_user):
    """The made-up rubric, Round 1 open, two rooms with one numbered team each."""
    rubric_file.apply(event, rubric_file.parse(SAMPLE.read_text()))
    round1 = Round.objects.filter(event=event).first()
    services.open_round_now(round1)
    rooms, teams = [], []
    for n in (1, 2):
        room = Room.objects.create(event=event, name=f"L50{n + 1}")
        team = Team.objects.create(event=event, name=f"Team {n}", number=str(n), room=room)
        person = Person.objects.create_user(email=f"25000500{n:02d}@kluniversity.in", full_name=f"Student {n}",
                                            student_id=f"25000500{n:02d}")
        TeamMember.objects.create(team=team, registration=register(event=event, person=person)[0], is_leader=True)
        rooms.append(room)
        teams.append(team)
    return {"round": round1, "rooms": rooms, "teams": teams}


def test_self_registered_faculty_is_approved_then_marks(client, event, ready, admin_user, make_person):
    applicant = make_person(email="new.judge@kluniversity.in", full_name="Dr. New Judge")
    client.force_login(applicant)
    client.post(reverse("faculty_register"), {"full_name": "Dr. New Judge", "employee_id": "KLF2001", "department": "ECE",
                                              "designation": "Assistant Professor", "phone": "9000002001", "event": event.pk})
    req = FacultyRequest.objects.get(person=applicant)
    assert client.get(reverse("hackathon_judge_event", args=[event.slug])).status_code == 404  # not yet
    client.force_login(admin_user)
    client.post(reverse("faculty_requests"), {"request": req.pk, "decision": "approve"})
    role = EventFaculty.objects.get(event=event, person=applicant)
    services.allocate_room(ready["round"], role, ready["rooms"][0])
    client.force_login(applicant)
    assert "Flow Hack" in client.get(reverse("staff_home")).content.decode()
    assignment = role.assignments.get()
    client.post(reverse("hackathon_mark", args=[assignment.pk]), marks_post(assignment, ["40", "30"], submit=True))
    assignment.refresh_from_db()
    assert assignment.is_submitted and assignment.total() == 70


def test_faculty_who_is_also_a_student(client, event, ready, django_capture_on_commit_callbacks):
    # A student (KL ID, kind student) who is also on this hackathon's faculty, and a member of Team 2.
    person = Person.objects.create_user(email="2500059999@kluniversity.in", full_name="Scholar Example", student_id="2500059999",
                                        kind=Person.Kind.STUDENT)
    reg, _ = register(event=event, person=person)
    TeamMember.objects.create(team=ready["teams"][1], registration=reg)
    role, _ = services.add_faculty(event, person)
    # They never judge their own team.
    made, notes = services.allocate_room(ready["round"], role, ready["rooms"][1])
    assert made == 0 and any("is one of its members" in n for n in notes)
    with pytest.raises(ValidationError):
        Assignment(round=ready["round"], faculty=role, team=ready["teams"][1], source=Assignment.Source.IMPORT).full_clean()
    services.allocate_room(ready["round"], role, ready["rooms"][0])
    # Login details, first sign-in: their faculty home, which also links to the hackathon they take part in.
    from people.logins import send_login_details

    with django_capture_on_commit_callbacks(execute=True):
        send_login_details(person, event=event, role="faculty")
    password = re.search(r"Temporary password: (\S+)", mail.outbox[-1].body).group(1)
    response = first_sign_in(client, "2500059999@kluniversity.in", password)
    assert response["Location"] == reverse("staff_home")
    home = client.get(reverse("staff_home")).content.decode()
    assert "Judging" in home and "Taking part as a student" in home and reverse("my_hackathon", args=[event.slug]) in home
    assert client.get(reverse("my_hackathon", args=[event.slug])).status_code == 200
    judge_page = client.get(reverse("hackathon_judge_event", args=[event.slug])).content.decode()
    assert "Team 1" in judge_page and "Team 2" not in judge_page
    assignment = role.assignments.get()
    client.post(reverse("hackathon_mark", args=[assignment.pk]), marks_post(assignment, ["30", "20"], submit=True))
    assignment.refresh_from_db()
    assert assignment.is_submitted


def test_faculty_never_reach_admin_pages_other_teams_other_hackathons_or_closed_rounds(client, event, ready, make_event):
    a = services.add_faculty(event, Person.objects.create_user(email="a.judge@kluniversity.in", full_name="Dr. A Judge"))[0]
    b = services.add_faculty(event, Person.objects.create_user(email="b.judge@kluniversity.in", full_name="Dr. B Judge"))[0]
    round1, round2 = Round.objects.filter(event=event)[:2]
    services.allocate_room(round1, a, ready["rooms"][0])
    services.allocate_room(round1, b, ready["rooms"][1])
    client.force_login(a.person)
    slug = event.slug
    for name, args in (("hackathon_dashboard", [slug]), ("hackathon_marks", [slug]), ("hackathon_manage_teams", [slug]),
                       ("hackathon_manage_faculty", [slug]), ("hackathon_rubric_settings", [slug]),
                       ("hackathon_rubric_download", [slug]), ("hackathon_import_start", [slug]),
                       ("hackathon_advance", [slug, round1.pk]), ("event_export", [slug, "marks"]),
                       ("hackathon_login_details", [slug, "faculty"]), ("faculty_requests", [])):
        assert client.get(reverse(name, args=args)).status_code == 403, name
    assert client.get("/admin/").status_code in (302, 403)
    # Another faculty member's team: not found, and marks can't be posted to it.
    theirs = b.assignments.get()
    assert client.get(reverse("hackathon_mark", args=[theirs.pk])).status_code == 404
    assert client.post(reverse("hackathon_mark", args=[theirs.pk]), marks_post(theirs, ["1", "1"])).status_code == 404
    assert not Score.objects.filter(assignment=theirs).exists()
    # Another hackathon: nothing.
    other = make_event(title="Other Hack", kind=Event.Kind.HACKATHON)
    assert client.get(reverse("hackathon_judge_event", args=[other.slug])).status_code == 404
    # A round that isn't open: the marksheet is read-only and the server refuses marks.
    Round.objects.filter(pk=round2.pk).update(opens_at=timezone.now() + timedelta(days=1))
    later = Assignment.objects.create(round=round2, faculty=a, team=ready["teams"][0], source=Assignment.Source.IMPORT)
    url = reverse("hackathon_mark", args=[later.pk])
    page = client.get(url).content.decode()
    assert "<fieldset disabled>" in page and "opens" in page
    response = client.post(url, marks_post(later, ["10", "10"]), follow=True)
    assert "is closed" in response.content.decode()
    assert not Score.objects.filter(assignment=later).exists()
    # A faculty member made inactive loses access too.
    EventFaculty.objects.filter(pk=a.pk).update(is_active=False)
    assert client.get(reverse("hackathon_mark", args=[a.assignments.filter(round=round1).get().pk])).status_code == 404
