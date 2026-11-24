from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from events.exports import build_export
from events.models import Question
from judging.models import Assignment, Criterion, Judge, Score
from judging.services import auto_assign, import_faculty, import_mapping
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


@pytest.fixture
def hackathon(event, make_person):
    Question.objects.create(event=event, key="team", label="Team name", required=True)
    Criterion.objects.create(event=event, label="Innovation", max_marks=10, position=1)
    Criterion.objects.create(event=event, label="Execution", max_marks=20, position=2)
    regs = [register(event=event, person=make_person(), answers={"team": f"Team {i}"})[0] for i in range(4)]
    return event, regs


def test_faculty_import_matches_columns_by_name_and_updates(hackathon):
    event, _ = hackathon
    report = import_faculty(event, xlsx([
        ["S.No", "Faculty Name", "Email ID", "Emp ID", "Dept", "Cabin"],
        [1, "Dr. A", "A@KLUniversity.in", 1001, "CSE", "C-101"],
        [2, "Dr. B", "b@kluniversity.in", 1002, "ECE", ""],
        [3, "", "nobody@kluniversity.in", "", "", ""],
    ]))
    assert (report.created, report.updated, len(report.problems)) == (2, 0, 1)
    a = Judge.objects.get(event=event, email="a@kluniversity.in")
    assert (a.name, a.employee_id, a.department, a.extra) == ("Dr. A", "1001", "CSE", {"S.No": "1", "Cabin": "C-101"})

    report = import_faculty(event, xlsx([["Name", "Email", "Department"], ["Dr. A", "a@kluniversity.in", "AI&DS"]]))
    assert (report.created, report.updated) == (0, 1)
    assert Judge.objects.get(pk=a.pk).department == "AI&DS"


def test_auto_assign_is_even_and_keeps_existing(hackathon):
    event, regs = hackathon
    judges = [Judge.objects.create(event=event, name=n) for n in ("A", "B", "C")]
    Assignment.objects.create(judge=judges[0], registration=regs[0])
    assert auto_assign(event, per_student=2) == 7
    loads = sorted(j.assignments.count() for j in judges)
    assert loads == [2, 3, 3]
    for r in regs:
        assert r.judge_assignments.count() == 2
    assert auto_assign(event, per_student=2) == 0


def test_mapping_import(hackathon):
    event, regs = hackathon
    Judge.objects.create(event=event, name="Dr. A", email="a@kluniversity.in")
    report = import_mapping(event, xlsx([
        ["Pass code", "Faculty email"],
        [regs[0].pass_code_display, "a@kluniversity.in"],
        [regs[1].person.email, "A@kluniversity.in"],
        ["NOPE-NOPE", "a@kluniversity.in"],
        [regs[2].pass_code, "missing@kluniversity.in"],
    ]))
    assert report.created == 2 and len(report.problems) == 2


def test_faculty_signs_in_marks_and_submits(client, hackathon, coordinator):
    event, regs = hackathon
    judge = Judge.objects.create(event=event, name="Dr. A")
    assignment = Assignment.objects.create(judge=judge, registration=regs[0])
    Assignment.objects.create(judge=Judge.objects.create(event=event, name="Dr. B"), registration=regs[1])

    client.force_login(coordinator)
    response = client.post(
        reverse("judging_issue_credentials", args=[event.slug]),
        {"which": "new", "expires_at": (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")},
    )
    sheet = load_workbook(BytesIO(response.content))["Faculty logins"]
    rows = {r[0]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    login_id, code = rows["Dr. A"][4], rows["Dr. A"][5]
    client.logout()

    assert client.get(reverse("judge_home"))["Location"] == reverse("judge_signin")
    bad = client.post(reverse("judge_signin"), {"login_id": login_id, "code": "WRONG"})
    assert b"don&#x27;t match" in bad.content
    client.post(reverse("judge_signin"), {"login_id": login_id.lower(), "code": code})
    home = client.get(reverse("judge_home"))
    assert regs[0].person.full_name.encode() in home.content
    assert regs[1].person.full_name.encode() not in home.content

    other = Assignment.objects.exclude(judge=judge).get()
    assert client.get(reverse("judge_mark", args=[other.pk])).status_code == 404

    url = reverse("judge_mark", args=[assignment.pk])
    page = client.get(url)
    assert b"Team 0" in page.content
    too_high = client.post(url, {"c_%d" % event.criteria.all()[0].pk: "11", "save": "1"})
    assert b"less than or equal to 10" in too_high.content
    c1, c2 = event.criteria.all()
    client.post(url, {f"c_{c1.pk}": "8", "save": "1"})
    assert Score.objects.get(assignment=assignment).marks == Decimal("8")
    incomplete = client.post(url, {f"c_{c1.pk}": "8", "submit": "1"})
    assert b"Give marks before submitting" in incomplete.content
    client.post(url, {f"c_{c1.pk}": "8", f"c_{c2.pk}": "15.5", "remarks": "Good", "submit": "1"})
    assignment.refresh_from_db()
    assert assignment.submitted_at and assignment.remarks == "Good"
    client.post(url, {f"c_{c1.pk}": "1", f"c_{c2.pk}": "1", "submit": "1"})
    assert assignment.total() == Decimal("23.5")

    # A new code ends the old session.
    judge.refresh_from_db()
    judge.issue_credentials(timezone.now() + timedelta(days=1))
    assert client.get(reverse("judge_home")).status_code == 302

    _, content = build_export(event, "marks", coordinator)
    wb = load_workbook(BytesIO(content))
    marks = list(wb["Marks by faculty"].iter_rows(values_only=True))
    header = marks[0]
    row = next(r for r in marks[1:] if r[0] == "Dr. A")
    assert row[header.index("Innovation (/10)")] == 8
    assert row[header.index("Total")] == 23.5
    assert row[header.index("Team name")] == "Team 0"
    assert row[header.index("Status")] == "Submitted"
    summary = list(wb["Summary (submitted only)"].iter_rows(values_only=True))
    assert len(summary) == 2 and summary[1][0] == 1


def test_expired_code_cannot_sign_in(client, hackathon):
    event, _ = hackathon
    judge = Judge.objects.create(event=event, name="Dr. A")
    code = judge.issue_credentials(timezone.now() - timedelta(minutes=1))
    response = client.post(reverse("judge_signin"), {"login_id": judge.login_id, "code": code})
    assert b"expired" in response.content


def test_marks_cannot_be_negative(hackathon):
    event, regs = hackathon
    a = Assignment.objects.create(judge=Judge.objects.create(event=event, name="A"), registration=regs[0])
    with pytest.raises(IntegrityError):
        Score.objects.create(assignment=a, criterion=event.criteria.first(), marks=-1)


def test_dashboard_needs_coordinator(client, hackathon, make_person, coordinator):
    event, _ = hackathon
    url = reverse("judging_dashboard", args=[event.slug])
    client.force_login(make_person())
    assert client.get(url).status_code == 403
    client.force_login(coordinator)
    assert client.get(url).status_code == 200
    assert client.get(reverse("judging_template", args=[event.slug, "faculty"])).status_code == 200
