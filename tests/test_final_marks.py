"""The final marks Excel: a Final sheet (one row per team), one sheet per round (one row per evaluator per team) and
the Rubric sheet, with a three-round rubric imported from tests/rubric_sample.json and two made-up evaluators."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from events.exports import build_export
from events.models import Event
from hackathon import rubric_file, services
from hackathon.models import Room, Round, RoundOutcome, Supervisor, Team, TeamMember
from people.models import Person
from registrations.services import register

SAMPLE = Path(__file__).with_name("rubric_sample.json")
PROMPTS = ["Sample prompt: a good point", "Sample prompt: a next step"]


def rows(content, name):
    return list(load_workbook(BytesIO(content))[name].iter_rows(values_only=True))


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin Example", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def marked(make_event, admin_user):
    """Two numbered teams in L502, judged in all three rounds by two evaluators; every marksheet submitted."""
    event = make_event(title="Final Marks Hack", kind=Event.Kind.HACKATHON)
    rubric_file.apply(event, rubric_file.parse(SAMPLE.read_text()))
    Round.objects.filter(event=event).update(opens_at=timezone.now() - timedelta(hours=1))
    room = Room.objects.create(event=event, name="L502")
    teams = {}
    for number, name, title in (("1", "Rockets", "Sample bins"), ("2", "Team 2", "Sample booking")):
        team = Team.objects.create(event=event, name=name, number=number, project_title=title, category="Sample category", room=room)
        for k in (1, 2):
            sid = f"25000{number}000{k}"
            person = Person.objects.create_user(email=f"{sid}@kluniversity.in", full_name=f"Student {number}{k}", student_id=sid)
            reg, _ = register(event=event, person=person)
            TeamMember.objects.create(team=team, registration=reg, is_leader=k == 1)
        teams[number] = team
    judges = []
    for n, emp in ((1, "KLF0001"), (2, "KLF0002")):
        person = Person.objects.create_user(email=f"evaluator{n}@kluniversity.in", full_name=f"Dr. Evaluator {n}", employee_id=emp)
        judges.append(services.add_faculty(event, person)[0])
    x, y = judges
    rounds = list(Round.objects.filter(event=event).prefetch_related("criteria"))
    # (round index, team number, judge) -> (marks per criterion, extras)
    plan = {
        (0, "1", x): ((48, 32), {}), (0, "1", y): ((42, 28), {}),
        (0, "2", x): ((30, 20), {"red_flags": ["Sample flag one."]}), (0, "2", y): ((36, 24), {}),
        (1, "1", x): ((40, 40), {}), (1, "1", y): ((45, 45), {}),
        (1, "2", x): ((30, 30), {}), (1, "2", y): ((35, 35), {"red_flags": ["Sample flag one.", "Sample flag two."]}),
        (2, "1", x): ((63, 27), {"self_score": Decimal(18), "recommendation": "Sample: keep going"}),
        (2, "1", y): ((56, 24), {"recommendation": "Sample: keep going"}),
        (2, "2", x): ((35, 15), {"recommendation": "Sample: try again"}), (2, "2", y): ((42, 18), {}),
    }
    for i, round_ in enumerate(rounds):
        for judge in judges:
            services.allocate_room(round_, judge, room)
        criteria = list(round_.criteria.all())
        for (ri, number, judge), (marks, extras) in plan.items():
            if ri != i:
                continue
            assignment = judge.assignments.get(round=round_, team=teams[number])
            comments = {PROMPTS[0]: f"Good point {number}", PROMPTS[1]: f"Next step {number}"}
            services.save_marks(assignment, dict(zip(criteria, marks)), "", True, {"comments": comments, **extras})
        for team in teams.values():
            RoundOutcome.objects.create(round=round_, team=team, advanced=True, decided_by=admin_user)
    return {"event": event, "teams": teams, "judges": judges, "rounds": rounds}


def test_final_sheet_one_row_per_team(marked, admin_user):
    _, content = build_export(marked["event"], "marks", admin_user)
    assert load_workbook(BytesIO(content)).sheetnames[:6] == [
        "Final", "R1 Sample round one", "R2 Sample round two", "R3 Sample round three", "Rubric", "Mark changes"]
    final = rows(content, "Final")
    header = final[0]
    assert list(header) == [
        "Team no", "Team", "Project title", "Category", "Members", "Members' KL IDs", "Room",
        "Round 1 (out of 100)", "Round 2 (out of 100)", "Round 3 (out of 100)", "Weighted total (out of 100)", "Rank",
        "Furthest round reached", "Self-score gap (faculty % minus self-score %)", "Recommendation", "Red flags"]
    first, second = final[1], final[2]
    get = lambda row, h: row[header.index(h)]  # noqa: E731
    assert (get(first, "Team no"), get(first, "Team"), get(first, "Project title"), get(first, "Category")) == (
        "1", "Rockets", "Sample bins", "Sample category")
    assert get(first, "Members") == "Student 11, Student 12" and get(first, "Members' KL IDs") == "2500010001, 2500010002"
    assert get(first, "Room") == "L502"
    # Each round is the average of the two evaluators' totals, out of 100.
    assert [get(first, f"Round {n} (out of 100)") for n in (1, 2, 3)] == [75, 85, 85]
    assert [get(second, f"Round {n} (out of 100)") for n in (1, 2, 3)] == [55, 65, 55]
    # Weighted with the rubric's weights 0.2, 0.3, 0.5.
    assert get(first, "Weighted total (out of 100)") == 83 and get(second, "Weighted total (out of 100)") == 58
    assert (get(first, "Rank"), get(second, "Rank")) == (1, 2)
    assert get(first, "Furthest round reached") == "Sample round three"
    # Self-score 18 of 20 is 90%; the faculty gave 85%.
    assert get(first, "Self-score gap (faculty % minus self-score %)") == -5
    assert get(first, "Recommendation") == "Sample: keep going" and get(second, "Recommendation") == "Sample: try again"
    assert (get(first, "Red flags"), get(second, "Red flags")) == (0, 3)


def test_one_sheet_per_round_with_a_row_per_evaluator(marked, admin_user):
    _, content = build_export(marked["event"], "marks", admin_user)
    sheet = rows(content, "R1 Sample round one")
    header = sheet[0]
    for column in ("Team no", "Team", "Sample criterion A (/60)", "Sample criterion A band", "Sample criterion B (/40)",
                   "Sample criterion B band", "Total", *PROMPTS, "Red flags", "Self-score", "Evaluator", "Employee ID", "Submitted at"):
        assert column in header
    assert len(sheet) == 5  # 2 teams x 2 evaluators
    row = next(r for r in sheet[1:] if r[1] == "Rockets" and r[header.index("Evaluator")] == "Dr. Evaluator 1")
    assert row[header.index("Sample criterion A (/60)")] == 48 and row[header.index("Sample criterion A band")] == "Bright"
    assert row[header.index("Total")] == 80 and row[header.index("Employee ID")] == "KLF0001"
    assert row[header.index(PROMPTS[0])] == "Good point 1" and row[header.index(PROMPTS[1])] == "Next step 1"
    assert row[header.index("Submitted at")] is not None and row[header.index("Status")] == "Submitted"
    flagged = next(r for r in sheet[1:] if r[1] == "Team 2" and r[header.index("Evaluator")] == "Dr. Evaluator 1")
    assert flagged[header.index("Red flags")] == "Sample flag one."
    r3 = rows(content, "R3 Sample round three")
    h3 = r3[0]
    rockets_x = next(r for r in r3[1:] if r[1] == "Rockets" and r[h3.index("Evaluator")] == "Dr. Evaluator 1")
    assert rockets_x[h3.index("Self-score")] == 18 and rockets_x[h3.index("Recommendation")] == "Sample: keep going"


def test_rubric_sheet(marked, admin_user):
    _, content = build_export(marked["event"], "marks", admin_user)
    rub = rows(content, "Rubric")
    flat = [c for r in rub for c in r if c is not None]
    assert "Sample criterion F" in flat and "Sample strong A" in flat and "Sample focus two" in flat
    assert any(r[0] == "Bright" and r[1] == 80 and r[2] == 100 and r[3] == "Sample meaning for the top band" for r in rub)
    assert "Sample criterion A at least Fair" in " ".join(str(c) for c in flat)
    assert any(r[0] == "Guidance for faculty" for r in rub)


def test_download_button_for_admins_and_supervisors_with_both_switches(client, marked, admin_user, make_person):
    event = marked["event"]
    url = reverse("event_export", args=[event.slug, "marks"])
    for page in ("hackathon_dashboard", "hackathon_marks"):
        client.force_login(admin_user)
        assert 'data-testid="final-marks"' in client.get(reverse(page, args=[event.slug])).content.decode()
    assert client.get(url).status_code == 200
    person = make_person(full_name="Sup Example")
    sup, _ = services.add_supervisor(event, person)
    Supervisor.objects.filter(pk=sup.pk).update(can_download=True, can_view_marks=True)
    client.force_login(person)
    assert 'data-testid="final-marks"' in client.get(reverse("hackathon_dashboard", args=[event.slug])).content.decode()
    assert client.get(url).status_code == 200
    Supervisor.objects.filter(pk=sup.pk).update(can_download=True, can_view_marks=False)
    assert 'data-testid="final-marks"' not in client.get(reverse("hackathon_dashboard", args=[event.slug])).content.decode()
    assert client.get(url).status_code == 403
    Supervisor.objects.filter(pk=sup.pk).update(can_download=False, can_view_marks=True)
    assert 'data-testid="final-marks"' not in client.get(reverse("hackathon_marks", args=[event.slug])).content.decode()
    assert client.get(url).status_code == 403
    volunteer = make_person(full_name="Vol Example")
    services.add_volunteer(event, volunteer)
    client.force_login(volunteer)
    assert client.get(url).status_code == 403
    assert client.get(reverse("hackathon_dashboard", args=[event.slug])).status_code == 403
