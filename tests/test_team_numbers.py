"""Forming teams from the students Excel by team name, by team number, or by number named from the team-name column.

The sheet shape: Sno, Id, Name, Branch, Team No, Name of the Team, Title of the Project, Category, Problem Statement,
one row per student, Team No merged over each team's rows, names on only some teams, and empty rows below the data.
All names, IDs and text here are made up.
"""

from datetime import timedelta
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from events.exports import build_export
from events.models import Event
from hackathon import services
from hackathon.models import Round, Team, TeamMember
from people.models import Person

HEADERS = ["Sno", "Id", "Name", "Branch", "Team No", "Name of the Team", "Title of the Project", "Category", "Problem Statement"]


def sample_rows(team2_names=("", "")):
    """Sheet rows 2-10. Team 1 is named (merged), team 2 has no name, team 3 is named on its second row only.
    Rows 9 and 10 have no team number and no team name."""
    return [
        [1, 2500032001.0, "Asha Example", "CSE", 1, "Rockets", "Smart bins", "Green", "Bins that sort waste"],
        [2, 2500032002.0, "Bala Example", "ECE", None, None, None, None, None],
        [3, 2500032003.0, "Chitra Example", "CSE", None, None, None, None, None],
        [4, 2500032004.0, "Dev Example", "IT", 2, team2_names[0], None, "Campus", None],
        [5, "2500032005.0", "Esha Example", "CSE", None, team2_names[1], "Lab booking", None, "Booking lab slots"],
        [6, 2500032006.0, "Farid Example", "ME", 3, "", "Water meter", "Utilities", "Metering water use"],
        [7, 2500032007.0, "Gita Example", "CSE", None, "Comets", "Water meter app", None, None],
        [8, 2500032008.0, "Hari Example", "CSE", "", "", "", "", ""],
        [9, 2500032009.0, "Indu Example", "EEE", "", "", "", "", ""],
    ]


def sheet_file(rows=None, merge_numbers=True, blank_rows=1000):
    """The made-up sheet as an upload, with Team No (and team 1's name) merged over each team's rows, and
    formatted but empty rows below the data."""
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for row in rows or sample_rows():
        ws.append(row)
    if merge_numbers:
        ws.merge_cells("E2:E4")
        ws.merge_cells("F2:F4")
        ws.merge_cells("E5:E6")
        ws.merge_cells("E7:E8")
    grey = PatternFill("solid", start_color="EEEEEE")
    last = ws.max_row
    for r in range(last + 1, last + 1 + blank_rows):
        for c in range(1, len(HEADERS) + 1):
            ws.cell(row=r, column=c).fill = grey
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "students.xlsx"
    return buffer


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin Example", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event):
    return make_event(title="Number Hack", kind=Event.Kind.HACKATHON, team_size_min=1, team_size_max=4)


def start(client, event, file):
    response = client.post(reverse("hackathon_import_start", args=[event.slug]), {"kind": "students", "file": file})
    assert response.status_code == 302, response.content[:500]
    return response["Location"]


def step(client, url, action="check", mode=None):
    data = {"action": action}
    if mode:
        data["team_mode"] = mode
    return client.post(url, data, follow=True)


def members(team):
    return sorted(m.registration.person.full_name for m in team.members.select_related("registration__person"))


def test_columns_are_matched_and_the_default_is_number_named_from_the_name_column(client, hack, admin_user):
    client.force_login(admin_user)
    url = start(client, hack, sheet_file())
    page = client.get(url).content.decode()
    assert "Form teams by:" in page
    assert 'value="number_named" checked' in page
    page = step(client, url).content.decode()
    assert "All 9 rows look right" in page  # the 1,000 empty rows are ignored
    assert ("3 teams by Team No: 2 named from Name of the Team, 1 will be called Team &lt;number&gt;. "
            "2 students have no team number and will be listed under Not in a team yet.") in page
    # Team 3's rows give different project titles: a warning, not an error.
    assert "The rows of team number 3 (rows 7, 8) give different project titles; the one in row 7 is used." in page


def test_form_by_number_named_from_the_team_name_column(client, hack, admin_user):
    client.force_login(admin_user)
    url = start(client, hack, sheet_file())
    step(client, url, "import")
    teams = {t.number: t for t in Team.objects.filter(event=hack)}
    assert set(teams) == {"1", "2", "3"}
    assert teams["1"].name == "Rockets" and teams["2"].name == "Team 2" and teams["3"].name == "Comets"
    assert members(teams["1"]) == ["Asha Example", "Bala Example", "Chitra Example"]
    assert members(teams["2"]) == ["Dev Example", "Esha Example"]
    # A team's values come from the first non-blank value among its rows.
    assert (teams["2"].project_title, teams["2"].category, teams["2"].project_description) == ("Lab booking", "Campus", "Booking lab slots")
    assert teams["3"].project_title == "Water meter"
    # The first row of each numbered team is its leader.
    assert TeamMember.objects.get(team=teams["2"], is_leader=True).registration.person.full_name == "Dev Example"
    # IDs like 2500032001.0 are cleaned; Branch goes to the student; no number means Not in a team yet.
    asha = Person.objects.get(student_id="2500032001")
    assert asha.branch == "CSE" and Person.objects.filter(student_id="2500032005").exists()
    assert sorted(r.person.full_name for r in services.not_in_a_team(hack)) == ["Hari Example", "Indu Example"]
    assert not Team.objects.filter(event=hack, name__in=["Hari Example", "Indu Example"]).exists()


def test_form_by_number_only_ignores_the_names(client, hack, admin_user):
    client.force_login(admin_user)
    url = start(client, hack, sheet_file())
    page = step(client, url, mode="number").content.decode()
    assert 'value="number" checked' in page
    assert "3 teams by Team No: 3 will be called Team &lt;number&gt;." in page
    step(client, url, "import")
    assert sorted(Team.objects.filter(event=hack).values_list("name", flat=True)) == ["Team 1", "Team 2", "Team 3"]


def test_form_by_team_name(client, hack, admin_user):
    client.force_login(admin_user)
    url = start(client, hack, sheet_file())
    page = step(client, url, mode="name").content.decode()
    assert "2 teams by Name of the Team. 5 students have no team name" in page
    step(client, url, "import")
    teams = {t.name: t for t in Team.objects.filter(event=hack)}
    assert set(teams) == {"Rockets", "Comets"}
    assert members(teams["Rockets"]) == ["Asha Example", "Bala Example", "Chitra Example"]
    assert members(teams["Comets"]) == ["Gita Example"]
    assert teams["Rockets"].number == ""
    assert len(services.not_in_a_team(hack)) == 5


def test_two_names_for_one_number_is_an_error_on_each_of_its_rows(client, hack, admin_user):
    client.force_login(admin_user)
    url = start(client, hack, sheet_file(sample_rows(team2_names=("Alpha", "Beta"))))
    page = step(client, url).content.decode()
    assert "2 of 9 rows have problems" in page
    for row in (5, 6):
        assert f"Row {row}: team number 2 has different team names: &quot;Alpha&quot; (row 5) and &quot;Beta&quot; (row 6)." in page
    step(client, url, "import")
    assert not Team.objects.filter(event=hack, number="2").exists()
    assert Team.objects.filter(event=hack, number__in=["1", "3"]).count() == 2


def test_a_name_that_clashes_with_another_team_is_an_error(client, hack, admin_user):
    Team.objects.create(event=hack, name="Team 2")  # made by hand, no number
    client.force_login(admin_user)
    url = start(client, hack, sheet_file())
    page = step(client, url).content.decode()
    assert "Row 5: team number 2 would be called &quot;Team 2&quot;, but another team already has that name." in page
    assert "Row 6: team number 2 would be called" in page


def test_a_reimport_finds_the_team_by_number_even_after_it_got_a_name(client, hack, admin_user):
    client.force_login(admin_user)
    step(client, start(client, hack, sheet_file()), "import")
    team2 = Team.objects.get(event=hack, number="2")
    leader = TeamMember.objects.get(team=team2, is_leader=True).registration
    services.leader_rename_team(hack, leader, "Bolts")
    step(client, start(client, hack, sheet_file()), "import")
    assert Team.objects.filter(event=hack).count() == 3
    team2.refresh_from_db()
    assert team2.name == "Bolts" and members(team2) == ["Dev Example", "Esha Example"]


def test_the_leader_renames_a_numbered_team_until_teams_lock(client, hack, admin_user):
    client.force_login(admin_user)
    step(client, start(client, hack, sheet_file()), "import")
    team2 = Team.objects.get(event=hack, number="2")
    leader = TeamMember.objects.get(team=team2, is_leader=True).registration.person
    other = TeamMember.objects.get(team=team2, is_leader=False).registration.person
    url = reverse("my_hackathon", args=[hack.slug])
    team_url = reverse("my_team", args=[hack.slug])
    # Not the leader: no form, and the server refuses.
    client.force_login(other)
    assert 'data-testid="rename-team"' not in client.get(url).content.decode()
    client.post(team_url, {"action": "rename", "name": "Sneaky"})
    team2.refresh_from_db()
    assert team2.name == "Team 2"
    client.force_login(leader)
    page = client.get(url).content.decode()
    assert 'data-testid="rename-team"' in page and "Team number" in page and "Campus" in page
    # A member's own name or another team's name is refused with a message.
    response = client.post(team_url, {"action": "rename", "name": "Esha Example"}, follow=True)
    assert "not a person&#x27;s name" in response.content.decode()
    response = client.post(team_url, {"action": "rename", "name": "rockets"}, follow=True)
    assert "Another team already has the name" in response.content.decode()
    client.post(team_url, {"action": "rename", "name": "Bolts"})
    team2.refresh_from_db()
    assert team2.name == "Bolts" and team2.number == "2"
    # Named now: no more renaming here.
    assert 'data-testid="rename-team"' not in client.get(url).content.decode()
    # A "Team <number>" team in a hackathon where teams are locked: refused.
    team1 = Team.objects.get(event=hack, number="1")
    team1.name = "Team 1"
    team1.save()
    hack.students_form_teams = False
    hack.save()
    lead1 = TeamMember.objects.get(team=team1, is_leader=True).registration.person
    client.force_login(lead1)
    assert 'data-testid="rename-team"' not in client.get(url).content.decode()
    client.post(team_url, {"action": "rename", "name": "Moons"})
    team1.refresh_from_db()
    assert team1.name == "Team 1"


def test_teams_tab_and_exports_show_number_and_category(client, hack, admin_user):
    client.force_login(admin_user)
    step(client, start(client, hack, sheet_file()), "import")
    Team.objects.create(event=hack, name="Zeta", number="10")
    page = client.get(reverse("hackathon_manage_teams", args=[hack.slug])).content.decode()
    assert "Campus" in page
    assert page.index("Rockets") < page.index("Team 2") < page.index("Comets") < page.index("Zeta")  # 1, 2, 3, 10
    teams = list(load_workbook(BytesIO(build_export(hack, "teams", admin_user)[1]))["Teams"].iter_rows(values_only=True))
    header = teams[0]
    assert header[:2] == ("Team no", "Team") and "Category" in header
    assert [r[0] for r in teams[1:]] == ["1", "2", "3", "10"]
    assert teams[2][header.index("Category")] == "Campus"
    students = list(load_workbook(BytesIO(build_export(hack, "teams", admin_user)[1]))["Students"].iter_rows(values_only=True))
    assert students[0][:4] == ("Team no", "Team", "Project title", "Category")


def test_team_numbers_are_unique_per_hackathon_and_cleaned(hack, make_event):
    from django.db import IntegrityError, transaction

    Team.objects.create(event=hack, name="One", number=" 7.0 ")
    assert Team.objects.get(event=hack, name="One").number == "7"
    with pytest.raises(IntegrityError), transaction.atomic():
        Team.objects.create(event=hack, name="Two", number="7")
    other = make_event(title="Other Hack", kind=Event.Kind.HACKATHON)
    Team.objects.create(event=other, name="One", number="7")  # another hackathon: fine
    Team.objects.create(event=hack, name="Three")
    Team.objects.create(event=hack, name="Four")  # blank numbers don't clash


def test_choosing_team_number_without_a_number_column_is_explained(client, hack, admin_user):
    client.force_login(admin_user)
    rows = [["Name", "KL ID", "Team"], ["Asha Example", "2500032001", "Rockets"]]
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    buffer.name = "students.xlsx"
    url = start(client, hack, buffer)
    page = step(client, url, mode="number").content.decode()
    assert "To form teams by team number, use one column as &quot;Team number&quot;" in page


def test_teams_lock_once_faculty_are_allocated(client, hack, admin_user):
    """Allocations lock teams, so the leader can no longer rename."""
    from hackathon.models import Room

    client.force_login(admin_user)
    step(client, start(client, hack, sheet_file()), "import")
    team2 = Team.objects.get(event=hack, number="2")
    room = Room.objects.create(event=hack, name="L502")
    team2.room = room
    team2.save()
    now = timezone.now()
    round_ = Round.objects.create(event=hack, name="Round 1", position=1, opens_at=now - timedelta(hours=1))
    judge = Person.objects.create_user(email="judge.example@kluniversity.in", full_name="Dr. Judge Example")
    services.allocate_room(round_, services.add_faculty(hack, judge)[0], room)
    leader = TeamMember.objects.get(team=team2, is_leader=True).registration
    with pytest.raises(services.TeamError):
        services.leader_rename_team(hack, leader, "Bolts")
