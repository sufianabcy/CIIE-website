from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from events.models import Event
from hackathon.models import FieldDefinition, ImportMapping, ImportUpload, Team, TeamMember
from people.models import Person
from registrations.models import Registration
from registrations.services import register


def xlsx(rows, sheets=None):
    wb = Workbook()
    wb.active.title = "Sheet1"
    for row in rows:
        wb.active.append(row)
    for name, more in (sheets or {}).items():
        ws = wb.create_sheet(name)
        for row in more:
            ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "upload.xlsx"
    return buffer


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin One", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event):
    return make_event(title="Team Hack", kind=Event.Kind.HACKATHON, team_size_min=2, team_size_max=3)


def upload(client, event, kind, file):
    response = client.post(reverse("hackathon_import_start", args=[event.slug]), {"kind": kind, "file": file})
    assert response.status_code == 302, response.content[-1500:]
    return response["Location"]


def choose(client, url, columns, action="check"):
    """columns: {column index: field key or "" to skip}"""
    return client.post(url, {"action": action, **{f"col_{i}": v for i, v in columns.items()}}, follow=True)


def test_import_page_is_easy_to_find(client, hack, admin_user):
    client.force_login(admin_user)
    for section, kind in (("students", "students"), ("teams", "teams"), ("faculty", "faculty")):
        page = client.get(reverse(f"hackathon_manage_{section}", args=[hack.slug])).content.decode()
        assert f'/import/?kind={kind}' in page and "Import from Excel" in page
    start = client.get(reverse("hackathon_import_start", args=[hack.slug]) + "?kind=teams").content.decode()
    assert "one row per team" in start and 'value="teams" checked' in start


def test_each_column_is_shown_with_samples_and_any_mapping_can_be_chosen(client, hack, admin_user):
    client.force_login(admin_user)
    url = upload(client, hack, "students", xlsx([["Full Name", "Mail", "Roll", "Shirt"], ["Asha", "asha@example.com", "2300011111", "M"]]))
    page = client.get(url)
    cols = page.context["columns"]
    assert [(c["letter"], c["header"], c["samples"]) for c in cols] == [("A", "Full Name", ["Asha"]), ("B", "Mail", ["asha@example.com"]),
                                                                     ("C", "Roll", ["2300011111"]), ("D", "Shirt", ["M"])]
    assert [c["target"] for c in cols] == ["name", "email", None, None]  # guessed from the headers
    assert "+ New field" in page.content.decode()
    # Choose every column, making "Shirt" a new custom field on the spot.
    choose(client, url, {0: "name", 1: "email", 2: "student_id", 3: "__new__"}, action="import")
    field = FieldDefinition.objects.get(event=hack, applies_to="student", label="Shirt")
    person = Person.objects.get(student_id="2300011111")
    assert person.email == "2300011111@kluniversity.in" and person.contact_email == "asha@example.com"
    assert person.hackathon_details.get(event=hack).values == {field.key: "M"}
    assert ImportMapping.objects.get(event=hack, kind="students").mapping == {"name": "Full Name", "email": "Mail", "student_id": "Roll", f"f:{field.key}": "Shirt"}
    # The next sheet with the same headers is matched the same way.
    url2 = upload(client, hack, "students", xlsx([["Full Name", "Mail", "Roll", "Shirt"], ["Bo", "bo@example.com", "2300022222", "L"]]))
    assert [c["target"] for c in client.get(url2).context["columns"]] == ["name", "email", "student_id", f"f:{field.key}"]


def test_every_kind_of_problem_is_listed_before_saving(client, hack, admin_user, make_person):
    make_person(email="one@example.com", student_id="111")
    make_person(email="two@example.com", student_id="222")
    client.force_login(admin_user)
    url = upload(client, hack, "students", xlsx([
        ["Name", "Email", "KL ID", "Email again"],
        ["Good", "good@example.com", "2300000201", ""],
        ["", "blank@example.com", "2300000202", ""],
        ["Bad", "nope", "2300000203", ""],
        ["Dupe", "good@example.com", "2300000204", ""],
        ["Clash", "one@example.com", "333", ""],
    ]))
    twice = choose(client, url, {0: "name", 1: "email", 2: "student_id", 3: "email"}).content.decode()
    assert "Columns B and D are both used as &quot;Email (optional)&quot;" in twice
    missing = choose(client, url, {0: "", 1: "email", 2: "", 3: ""}).content.decode()
    assert "&quot;Name&quot; is required, but no column is matched to it." in missing
    assert "&quot;KL ID&quot; is required, but no column is matched to it." in missing
    check = choose(client, url, {0: "name", 1: "email", 2: "student_id", 3: ""}).content.decode()
    assert "Row 3, column A (Name): required, but empty." in check
    assert "Row 4, column B (Email): &quot;nope&quot; is not a valid email." in check
    assert "Row 5, column B (Email): &quot;good@example.com&quot; is also in row 2." in check
    assert "Row 6: the email one@example.com belongs to" in check
    assert not Registration.objects.filter(event=hack).exists()  # nothing saved by checking


def test_sheet_and_header_row_can_be_chosen_and_rows_are_numbered_as_in_excel(client, hack, admin_user):
    client.force_login(admin_user)
    file = xlsx([["ignore me"]], sheets={"Real": [["Hackathon list"], [], ["Name", "KL ID"], ["Asha", "2300000301"], ["", "2300000302"]]})
    url = upload(client, hack, "students", file)
    client.post(url, {"action": "sheet", "sheet": "Real", "header_row": "3"})
    item = ImportUpload.objects.get()
    assert (item.sheet_name, item.header_row) == ("Real", 3)
    check = choose(client, url, {0: "name", 1: "student_id"}).content.decode()
    assert "Row 5, column A (Name): required, but empty." in check  # the row number in the person's Excel


def test_team_import_one_row_per_team(client, hack, admin_user, make_person):
    make_person(email="existing@example.com", student_id="2300099999", full_name="Already Here")
    client.force_login(admin_user)
    url = upload(client, hack, "teams", xlsx([
        ["Team Name", "Problem Statement", "Member 1 Name", "Member 1 Email", "Member 2 Name", "Member 2 Email", "Member 2 KL ID", "Member 3 Name", "Member 3 Email"],
        ["Rocket", "Smart bins", "Asha", "asha@example.com", "Ravi", "ravi@example.com", "2300011111", "", ""],
        ["Comet", "Bus tracker", "Mia", "mia@example.com", "", "", "2300099999", "", ""],
        ["rocket", "Copy", "Zed", "zed@example.com", "Yan", "yan@example.com", "", "", ""],
        ["Solo", "Alone", "Lone", "lone@example.com", "", "", "", "", ""],
        ["Big", "Too many", "A", "a@example.com", "B", "b@example.com", "", "C", "c@example.com"],
        ["Twice", "Again", "Asha", "asha@example.com", "Kai", "kai@example.com", "", "", ""],
    ]))
    page = client.get(url)
    targets = [c["target"] for c in page.context["columns"]]
    assert targets == ["team", "project_title", "m1_name", "m1_email", "m2_name", "m2_email", "m2_student_id", "m3_name", "m3_email"]
    check = choose(client, url, dict(enumerate(targets))).content.decode()
    assert "Row 4: the team name &quot;rocket&quot; is also in row 2." in check
    assert "Row 5: Solo has 1 member(s); teams here have 2 to 3." in check
    assert "Row 7, column D (Member 1 Email): &quot;asha@example.com&quot; is also in row 2." in check
    done = choose(client, url, dict(enumerate(targets)), action="import").content.decode()
    assert "3 team(s) added" in done  # Rocket, Comet and Big (3 members is within 2 to 3)
    rocket = Team.objects.get(event=hack, name="Rocket")
    assert rocket.project_title == "Smart bins"
    members = {(m.registration.person.email, m.is_leader) for m in rocket.members.all()}
    assert members == {("asha@example.com", True), ("2300011111@kluniversity.in", False)}  # Ravi has a KL ID: he signs in with it
    ravi = Person.objects.get(student_id="2300011111")
    assert ravi.kind == "student" and ravi.contact_email == "ravi@example.com"
    comet = Team.objects.get(event=hack, name="Comet")
    assert {m.registration.person.full_name for m in comet.members.all()} == {"Mia", "Already Here"}  # found by KL ID
    # Importing again: those students are already in teams, so nothing changes.
    url2 = upload(client, hack, "teams", xlsx([["Team Name", "Member 1 Email", "Member 2 Email"], ["Nova", "asha@example.com", "new@example.com"]]))
    again = choose(client, url2, {0: "team", 1: "m1_email", 2: "m2_email"}).content.decode()
    assert "is already in team Rocket" in again


def test_a_student_is_in_one_team_per_hackathon(hack, make_person):
    reg, _ = register(event=hack, person=make_person())
    a, b = Team.objects.create(event=hack, name="A"), Team.objects.create(event=hack, name="B")
    TeamMember.objects.create(team=a, registration=reg)
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamMember.objects.create(team=b, registration=reg)
    first = Registration.objects.create(event=hack, person=make_person())
    TeamMember.objects.create(team=b, registration=first, is_leader=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamMember.objects.create(team=b, registration=Registration.objects.create(event=hack, person=make_person()), is_leader=True)


def test_teams_and_students_tabs_show_the_links(client, hack, admin_user, make_person):
    team = Team.objects.create(event=hack, name="Rocket")
    lead, _ = register(event=hack, person=make_person(full_name="Lead Person"))
    other, _ = register(event=hack, person=make_person(full_name="Other Person"))
    loner, _ = register(event=hack, person=make_person(full_name="Loner Person"))
    TeamMember.objects.create(team=team, registration=lead, is_leader=True)
    member = TeamMember.objects.create(team=team, registration=other)
    client.force_login(admin_user)
    teams = client.get(reverse("hackathon_manage_teams", args=[hack.slug])).content.decode()
    assert "★ Lead Person, Other Person" in teams
    no_team = client.get(reverse("hackathon_manage_students", args=[hack.slug]) + "?no_team=1").content.decode()
    assert "Loner Person" in no_team and "Lead Person" not in no_team
    client.post(reverse("hackathon_manage_team_edit", args=[hack.slug, team.pk]), {"member_action": "leader", "member": member.pk})
    member.refresh_from_db()
    assert member.is_leader and not TeamMember.objects.get(registration=lead).is_leader
    from events.exports import build_export

    rows = list(load_workbook(BytesIO(build_export(hack, "teams", admin_user)[1]))["Teams"].iter_rows(values_only=True))
    first = rows[0].index("Member 1 name (leader)")
    assert rows[0][first:first + 3] == ("Member 1 name (leader)", "Member 1 email", "Member 1 KL ID") and rows[1][first] == "Other Person"


def test_a_leader_registers_the_whole_team(client, hack, make_person, django_capture_on_commit_callbacks):
    from mailer.models import OutboxEmail

    leader = make_person(email="lead@example.com", full_name="Lead")
    known = make_person(email="known@example.com", full_name="Known")
    client.force_login(leader)
    url = reverse("register", args=[hack.slug])
    too_small = client.post(url, {"team-team_name": "Solo Act"})
    assert b"Teams here have 2 to 3 members, including you. This team has 1." in too_small.content
    needs_name = client.post(url, {"team-team_name": "Rocket", "team-m2_email": "fresh@example.com"})
    assert b"Give their name" in needs_name.content
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"team-team_name": "Rocket", "team-m2_email": "known@example.com", "team-m3_name": "Fresh", "team-m3_email": "fresh@example.com"})
    assert response["Location"] == reverse("my_hackathon", args=[hack.slug])
    team = Team.objects.get(event=hack, name="Rocket")
    assert {(m.registration.person.email, m.is_leader) for m in team.members.all()} == {
        ("lead@example.com", True), ("known@example.com", False), ("fresh@example.com", False)}
    assert Person.objects.get(email="fresh@example.com").kind == "student"
    assert {e.to_email for e in OutboxEmail.objects.all()} >= {"lead@example.com", "known@example.com", "fresh@example.com"}
    # Everyone sees the team on their My hackathon page.
    client.force_login(known)
    assert b"Rocket" in client.get(reverse("my_hackathon", args=[hack.slug])).content
    # A second team can't take someone already in one.
    other = make_person(email="other@example.com")
    client.force_login(other)
    clash = client.post(url, {"team-team_name": "Comet", "team-m2_email": "known@example.com"})
    assert b"already in a team for this hackathon" in clash.content


def test_faculty_profile_asks_only_name_department_and_phone(client, make_person):
    faculty = make_person(email="fac@kluniversity.in", kind="faculty", full_name="Dr. F")
    client.force_login(faculty)
    page = client.get(reverse("profile")).content.decode()
    assert 'name="institution"' not in page and 'name="year_of_study"' not in page
    assert ">Name" in page and "Department" in page and "KL faculty email" in page and "printed on certificates" not in page
    client.post(reverse("profile"), {"full_name": "Dr. Faculty", "phone": "9000000001", "branch": "CSE"})
    faculty.refresh_from_db()
    assert (faculty.full_name, faculty.branch) == ("Dr. Faculty", "CSE")
    student = make_person(email="stu@example.com")
    client.force_login(student)
    page = client.get(reverse("profile")).content.decode()
    assert 'name="institution"' in page and 'name="year_of_study"' in page and "KL student status" in page


# --- Team names left blank or written once per team ---------------------------------------------


def merged_xlsx():
    """Team names typed once per team in a merged cell, the way many sheets are made."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Team", "Name", "KL ID"])
    for row in (["Alpha", "A1", "2300000001"], ["", "A2", "2300000002"], ["", "A3", "2300000003"],
                ["Beta", "B1", "2300000004"], ["", "B2", "2300000005"]):
        ws.append(row)
    ws.merge_cells("A2:A4")
    ws.merge_cells("A5:A6")
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "merged.xlsx"
    return buffer


def test_team_names_in_merged_cells_go_to_every_member(client, hack, admin_user):
    client.force_login(admin_user)
    url = upload(client, hack, "students", merged_xlsx())
    page = client.get(url).content.decode()
    assert "no team name" not in page  # merged cells are read for every row they cover
    choose(client, url, {0: "team", 1: "name", 2: "student_id"}, action="import")
    assert sorted(Team.objects.filter(event=hack).values_list("name", flat=True)) == ["Alpha", "Beta"]
    assert Team.objects.get(event=hack, name="Alpha").members.count() == 3
    assert Team.objects.get(event=hack, name="Beta").members.count() == 2


def test_reimport_with_team_names_clears_the_teams_of_one_made_before(client, hack, admin_user):
    from hackathon import services

    services.import_students(hack, xlsx([["Name", "KL ID", "Team"], ["Solo Sam", "2300000021", "Solo Sam"], ["Lead", "2300000022", "Nova"]]))
    assert Team.objects.filter(event=hack, name="Solo Sam").exists()  # a team named after its only member, as older imports made
    report = services.import_students(hack, xlsx([["Name", "KL ID", "Team"], ["Solo Sam", "2300000021", "Nova"]]))
    assert not Team.objects.filter(event=hack, name="Solo Sam").exists()
    assert Team.objects.get(event=hack, name="Nova").members.count() == 2
    assert any("removed the empty team" in w for w in report.warnings)
    # A named team that empties out is kept: only a team of one named after its student is removed.
    services.import_students(hack, xlsx([["Name", "KL ID", "Team"], ["Lead", "2300000022", "Other"], ["Solo Sam", "2300000021", "Other"]]))
    nova = Team.objects.get(event=hack, name="Nova")
    assert not nova.members.exists()
