"""Student and team data in a hackathon: KL ID as the key, blank team = not in a team, unique team names,
the Teams tab's "Not in a team yet", students forming their own teams, and tidying teams made up by older imports.

All names, IDs and emails here are made up.
"""

from importlib import import_module
from io import BytesIO

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, transaction
from django.urls import NoReverseMatch, reverse
from openpyxl import Workbook

from events.models import Event
from hackathon import services
from hackathon.models import Assignment, Attendance, EventFaculty, ImportUpload, Round, Team, TeamMember
from people.models import Person
from registrations.models import Registration
from registrations.services import register


def xlsx(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
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
    return make_event(title="Team Data Hack", kind=Event.Kind.HACKATHON, team_size_min=2, team_size_max=3)


def student(event, n, name=None, team=None, leader=False):
    """A registered student with a made-up KL ID 23000000nn, optionally in a team."""
    sid = f"23000000{n:02d}"
    person = Person.objects.create_user(email=f"{sid}@kluniversity.in", full_name=name or f"Student {n}", student_id=sid,
                                        profile_completed_at="2026-01-01T00:00Z")
    reg, _ = register(event=event, person=person)
    if team is not None:
        TeamMember.objects.create(team=team, registration=reg, is_leader=leader)
    return reg


def upload(client, event, rows):
    response = client.post(reverse("hackathon_import_start", args=[event.slug]), {"kind": "students", "file": xlsx(rows)})
    assert response.status_code == 302
    return response["Location"]


def choose(client, url, columns, action="check"):
    return client.post(url, {"action": action, **{f"col_{i}": v for i, v in columns.items()}}, follow=True)


def allocate(event, team):
    round_ = Round.objects.create(event=event, name="Round 1")
    judge = Person.objects.create_user(email="judge@example.com", full_name="Dr. Judge Example", employee_id="EMP900")
    faculty = EventFaculty.objects.create(event=event, person=judge)
    return Assignment.objects.create(round=round_, faculty=faculty, team=team)


# --- 1. Student import: Name and KL ID required, the ID is the key ---------------------------------


def test_only_name_and_kl_id_are_required(client, hack, admin_user):
    client.force_login(admin_user)
    url = upload(client, hack, [["Name", "KL ID"], ["Asha Example", "2300000101"]])
    choose(client, url, {0: "name", 1: "student_id"}, action="import")
    person = Person.objects.get(student_id="2300000101")
    assert person.full_name == "Asha Example" and person.email == "2300000101@kluniversity.in"
    assert Registration.objects.filter(event=hack, person=person).exists()
    missing = choose(client, upload(client, hack, [["Name", "Email"], ["Ravi Example", "ravi@example.com"]]), {0: "name", 1: "email"})
    assert "&quot;KL ID&quot; is required, but no column is matched to it." in missing.content.decode()


def test_kl_id_is_trimmed_drops_dot_zero_and_matches_ignoring_case(hack, make_person):
    existing = make_person(email="mixed@example.com", student_id="2300AB01", full_name="Mixed Case")
    numeric = make_person(email="numeric@example.com", student_id="2300000202", full_name="Number Cell")
    people = Person.objects.count()
    report = services.import_students(hack, xlsx([["Name", "KL ID"], ["Mixed Case", "  2300ab01 "], ["Number Cell", "2300000202.0"]]))
    assert report.problems == [] and Person.objects.count() == people
    assert set(Registration.objects.filter(event=hack).values_list("person", flat=True)) == {existing.pk, numeric.pk}
    assert services.clean_student_id(" 2300000303.0 ") == "2300000303" and services.clean_student_id("23.5") == "23.5"


def test_duplicate_kl_ids_in_the_file_are_errors_listed_by_row(client, hack, admin_user):
    client.force_login(admin_user)
    url = upload(client, hack, [["Name", "KL ID"], ["One", "2300000401"], ["Two", "2300000402"], ["Again", "2300000401.0"]])
    check = choose(client, url, {0: "name", 1: "student_id"}).content.decode()
    assert "Row 2: KL ID 2300000401 is also in row 4." in check and "Row 4: KL ID 2300000401 is also in row 2." in check
    choose(client, url, {0: "name", 1: "student_id"}, action="import")
    assert list(Registration.objects.filter(event=hack).values_list("person__student_id", flat=True)) == ["2300000402"]
    direct = services.import_students(hack, xlsx([["Name", "KL ID"], ["A", "2300000501"], ["B", "2300000501"]]))
    assert direct.problems == ["Row 3: KL ID 2300000501 is also in row 2."]


def test_an_id_already_in_the_hackathon_updates_that_student(hack):
    first = services.import_students(hack, xlsx([["Name", "KL ID", "Phone"], ["Kiran Example", "2300000601", ""]]))
    people, regs = Person.objects.count(), Registration.objects.count()
    again = services.import_students(hack, xlsx([["Name", "KL ID", "Phone"], ["Kiran Example", "2300000601", "9000000601"]]))
    assert (first.created, again.created, again.updated) == (1, 0, 1)
    assert Person.objects.count() == people and Registration.objects.count() == regs
    assert Person.objects.get(student_id="2300000601").phone == "9000000601"


# --- 2. A blank team name always means "not in a team" -------------------------------------------------


def test_blank_team_names_are_listed_under_not_in_a_team(client, hack, admin_user):
    client.force_login(admin_user)
    url = upload(client, hack, [["Team", "Name", "KL ID"], ["Rocket", "R One", "2300000701"], ["", "R Two", "2300000702"],
                                ["Rocket", "R Three", "2300000703"], ["", "C One", "2300000704"]])
    page = choose(client, url, {0: "team", 1: "name", 2: "student_id"}).content.decode()
    assert "2 students have no team name and will be listed under Not in a team yet." in page
    assert 'name="blank_teams"' not in page and "row above" not in page and "team of one" not in page.lower()
    choose(client, url, {0: "team", 1: "name", 2: "student_id"}, action="import")
    assert list(Team.objects.filter(event=hack).values_list("name", flat=True)) == ["Rocket"]
    free = client.get(reverse("hackathon_manage_teams", args=[hack.slug])).content.decode().split('id="not-in-a-team"')[1]
    assert "R Two" in free and "C One" in free and "2300000702" in free and "R One" not in free


def test_a_blank_team_name_takes_a_student_out_of_their_team(client, hack, admin_user):
    rocket = Team.objects.create(event=hack, name="Rocket")
    student(hack, 1, "Leaving Example", team=rocket)
    client.force_login(admin_user)
    url = upload(client, hack, [["Team", "Name", "KL ID"], ["", "Leaving Example", "2300000001"]])
    page = choose(client, url, {0: "team", 1: "name", 2: "student_id"}).content.decode()
    assert "1 of them is in a team now and will be taken out of it." in page
    choose(client, url, {0: "team", 1: "name", 2: "student_id"}, action="import")
    assert not TeamMember.objects.filter(team=rocket).exists()
    # Without a team column, teams are left as they are.
    TeamMember.objects.create(team=rocket, registration=Registration.objects.get(event=hack))
    services.import_students(hack, xlsx([["Name", "KL ID", "Phone"], ["Leaving Example", "2300000001", "9000000001"]]))
    assert TeamMember.objects.filter(team=rocket).count() == 1


def test_no_path_makes_a_team_named_after_a_person(client, hack, admin_user, make_person, make_event):
    client.force_login(admin_user)
    # Portal add: "Not in a team" is the empty choice and means exactly that.
    add = client.get(reverse("hackathon_manage_student_add", args=[hack.slug])).content.decode()
    assert "Not in a team" in add and "team of one" not in add
    client.post(reverse("hackathon_manage_student_add", args=[hack.slug]),
                {"d-f_name": "Portal Example", "d-f_student_id": "2300000801", "d-f_phone": "9000000801", "t-team": ""})
    assert Registration.objects.filter(event=hack, person__student_id="2300000801", team_membership__isnull=True).exists()
    # Team import: a row with no team name is refused, never named after its leader.
    report = services.import_teams(hack, xlsx([["Team Name", "Member 1 Name", "Member 1 KL ID", "Member 2 Name", "Member 2 KL ID"],
                                               ["", "Lead Example", "2300000802", "Mate Example", "2300000803"]]))
    assert report.problems == ["Row 2: no team name."]
    # Registration: entering alone (where teams of 1 are allowed) registers without a team.
    open_hack = make_event(title="Open Hack", kind=Event.Kind.HACKATHON, team_size_min=1, team_size_max=3)
    solo = make_person(email="solo@example.com", full_name="Solo Example")
    client.force_login(solo)
    client.post(reverse("register", args=[open_hack.slug]), {"team-team_name": ""})
    solo_reg = Registration.objects.get(event=open_hack, person=solo)
    assert not hasattr(solo_reg, "team_membership") and not Team.objects.filter(event=open_hack).exists()
    assert not Team.objects.filter(event=hack, name__iexact="Portal Example").exists()
    # The dashboard's "Make each a team of one" is gone.
    client.force_login(admin_user)
    dashboard = client.get(reverse("hackathon_dashboard", args=[hack.slug])).content.decode()
    assert "team of one" not in dashboard
    with pytest.raises(NoReverseMatch):
        reverse("hackathon_solo_teams", args=[hack.slug])


# --- 3. Team names unique ignoring case and extra spaces ----------------------------------------------------


def test_team_names_are_unique_ignoring_case_and_spaces(hack):
    team = Team.objects.create(event=hack, name="  Team   Rocket ")
    assert team.name == "Team Rocket"
    with pytest.raises(IntegrityError), transaction.atomic():
        Team.objects.create(event=hack, name="team rocket")
    # The database itself refuses it, even past the model's tidying.
    Team.objects.filter(pk=team.pk).update(name="Team  Comet")
    with pytest.raises(IntegrityError), transaction.atomic():
        Team.objects.bulk_create([Team(event=hack, name="team comet")])
    # The same name in a sheet, however it's spaced or cased, is one team.
    services.import_students(hack, xlsx([["Name", "KL ID", "Team"], ["A Example", "2300000901", "Nova Squad"],
                                         ["B Example", "2300000902", " nova  squad "], ["C Example", "2300000903", "NOVA SQUAD"]]))
    nova = Team.objects.get(event=hack, name="Nova Squad")
    assert nova.members.count() == 3 and Team.objects.filter(event=hack).count() == 2


def test_the_migration_merges_or_reports_existing_duplicates(hack, capsys):
    migration = import_module("hackathon.migrations.0014_tidy_team_names")
    constraint = next(c for c in Team._meta.constraints if c.name == "team_name_unique_per_event_normalised")
    with connection.schema_editor() as editor:
        editor.remove_constraint(Team, constraint)
    judged, spare, attended = Team.objects.bulk_create([Team(event=hack, name="Alpha"), Team(event=hack, name=" alpha "),
                                                         Team(event=hack, name="ALPHA  ")])
    allocate(hack, judged)  # allocations: this one is kept as it is
    moved = student(hack, 2, team=spare, leader=True)  # nothing recorded: merged into the kept one
    Attendance.objects.create(round=Round.objects.get(event=hack), team=attended, present=True,
                              marked_by=Person.objects.get(email="judge@example.com"))  # attendance: never merged
    migration.merge_duplicate_team_names(django_apps, None)
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # as at the end of migration 0014's own transaction
    with connection.schema_editor() as editor:
        editor.add_constraint(Team, constraint)  # no duplicates left, so 0015's rule goes on
    names = {t.pk: t.name for t in Team.objects.filter(event=hack)}
    assert spare.pk not in names
    assert names == {judged.pk: "Alpha", attended.pk: "Alpha (2)"}
    assert TeamMember.objects.get(registration=moved).team_id == judged.pk
    out = capsys.readouterr().out
    assert "merged team" in out and "not merged; renamed" in out


def test_duplicate_team_names_get_a_friendly_error(client, hack, admin_user, make_person):
    Team.objects.create(event=hack, name="Rocket")
    client.force_login(admin_user)
    portal = client.post(reverse("hackathon_manage_team_add", args=[hack.slug]), {"name": "  ROCKET "})
    assert portal.status_code == 200 and "Another team already has this name." in portal.content.decode()
    leader = make_person(email="leader@example.com", full_name="Leader Example", student_id="2300001001")
    client.force_login(leader)
    page = client.post(reverse("register", args=[hack.slug]), {"team-team_name": "rocket", "team-m2_name": "Mate Example",
                                                               "team-m2_student_id": "2300001002"})
    assert "Another team already has this name." in page.content.decode()
    assert Team.objects.filter(event=hack).count() == 1
    from registrations.services import RegistrationError

    with pytest.raises(RegistrationError, match="already has the name"):
        services.register_team(hack, leader, " Rocket ", [])


# --- 4. Teams tab: "Not in a team yet", ticked or one at a time ----------------------------------------------


def test_teams_tab_lists_students_not_in_a_team_and_adds_them(client, hack, admin_user):
    full = Team.objects.create(event=hack, name="Full House")
    for n in (1, 2, 3):
        student(hack, n, team=full)
    a, b, c = student(hack, 4, "Free One"), student(hack, 5, "Free Two"), student(hack, 6, "Free Three")
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_teams", args=[hack.slug])).content.decode()
    section = page.split('id="not-in-a-team"')[1]
    assert "Free One" in section and "2300000004" in section and "2300000004@kluniversity.in" in section
    dashboard = client.get(reverse("hackathon_dashboard", args=[hack.slug])).content.decode()
    assert 'data-testid="not-in-team-count"><div class="stat-value">3</div>' in dashboard

    url = reverse("hackathon_add_to_team", args=[hack.slug])
    too_few = client.post(url, {"r": [a.pk], "new_name": "Lonely"}, follow=True).content.decode()
    assert "A new team needs 2 to 3 members; you chose 1." in too_few and not Team.objects.filter(name="Lonely").exists()
    no_room = client.post(url, {"r": [a.pk], "team": full.pk}, follow=True).content.decode()
    assert "Full House has 3 members; teams here have at most 3" in no_room
    client.post(url, {"r": [a.pk, b.pk], "new_name": "Fresh Pair"})
    pair = Team.objects.get(event=hack, name="Fresh Pair")
    assert sorted(m.registration_id for m in pair.members.all()) == sorted([a.pk, b.pk])
    assert pair.members.filter(is_leader=True).count() == 1

    # One at a time: a page for that student, then into an existing team.
    one = client.get(url + f"?r={c.pk}").content.decode()
    assert "Free Three" in one and "Fresh Pair (2/3)" in one
    client.post(url, {"r": [c.pk], "team": pair.pk, "back": "one"})
    assert pair.members.count() == 3 and not services.not_in_a_team(hack).exists()


def test_only_people_who_edit_teams_can_add_students_to_teams(client, hack, make_person):
    reg = student(hack, 1)
    outsider = make_person(email="outsider@example.com")
    client.force_login(outsider)
    assert client.post(reverse("hackathon_add_to_team", args=[hack.slug]), {"r": [reg.pk], "new_name": "Nope"}).status_code == 403


# --- 5. Students form their own teams until teams lock --------------------------------------------------------


def test_a_student_creates_a_team_and_leads_it(client, hack):
    me, mate, other = student(hack, 1, "Me Example"), student(hack, 2, "Mate Example"), student(hack, 3, "Other Example")
    student(hack, 4, "Spare Example")
    client.force_login(me.person)
    page = client.get(reverse("my_hackathon", args=[hack.slug])).content.decode()
    assert "Create your team" in page and 'name="mate_1"' in page and 'name="mate_2"' in page and 'name="mate_3"' not in page
    url = reverse("my_team", args=[hack.slug])
    alone = client.post(url, {"action": "create", "name": "Solo Try"}, follow=True).content.decode()
    assert "Teams here have 2 to 3 members, including you. This team would have 1." in alone
    unknown = client.post(url, {"action": "create", "name": "Our Team", "mate_1": "2300009999"}, follow=True).content.decode()
    assert "No student with KL ID 2300009999 is registered for this hackathon." in unknown
    assert 'value="Our Team"' in unknown  # what they typed is kept
    client.post(url, {"action": "create", "name": "Our Team", "mate_1": " 2300000002 "})
    team = Team.objects.get(event=hack, name="Our Team")
    assert team.members.get(registration=me).is_leader and team.members.filter(registration=mate).exists()
    # A teammate already in a team can't be taken.
    client.force_login(other.person)
    taken = client.post(url, {"action": "create", "name": "Other Team", "mate_1": "2300000002"}, follow=True).content.decode()
    assert "Mate Example (2300000002) is already in a team." in taken
    dup = client.post(url, {"action": "create", "name": " our  TEAM ", "mate_1": "2300000004"}, follow=True).content.decode()
    assert "Another team already has the name" in dup and Team.objects.filter(event=hack).count() == 1


def test_the_leader_adds_and_removes_teammates_until_teams_lock(client, hack, admin_user):
    team = Team.objects.create(event=hack, name="Leaders")
    lead = student(hack, 1, "Lead Example", team=team, leader=True)
    mate = student(hack, 2, "Mate Example", team=team)
    extra = student(hack, 3, "Extra Example")
    url = reverse("my_team", args=[hack.slug])
    client.force_login(mate.person)
    refused = client.post(url, {"action": "add", "student_id": "2300000003"}, follow=True).content.decode()
    assert "Only your team&#x27;s leader can change its members." in refused or "Only your team's leader" in refused
    client.force_login(lead.person)
    client.post(url, {"action": "add", "student_id": "2300000003"})
    assert team.members.count() == 3
    member = team.members.get(registration=extra)
    client.post(url, {"action": "remove", "member": member.pk})
    assert team.members.count() == 2
    too_small = client.post(url, {"action": "remove", "member": team.members.get(registration=mate).pk}, follow=True).content.decode()
    assert "Teams here need at least 2 members." in too_small and team.members.count() == 2

    # An admin locks teams: no more changes, and the page says so.
    client.force_login(admin_user)
    assert hack.students_form_teams  # on by default
    client.post(reverse("hackathon_teams_lock", args=[hack.slug]), {"open": "0"})
    hack.refresh_from_db()
    assert not hack.students_form_teams
    client.force_login(lead.person)
    locked = client.post(url, {"action": "add", "student_id": "2300000003"}, follow=True).content.decode()
    assert "The organisers have locked teams." in locked and team.members.count() == 2
    assert 'data-testid="add-teammate"' not in locked


def test_teams_lock_by_themselves_once_faculty_are_allocated(client, hack, admin_user, make_person):
    team = Team.objects.create(event=hack, name="Judged")
    student(hack, 1, team=team)
    loner = student(hack, 2, "Loner Example")
    assert services.teams_locked_reason(hack) == ""
    allocate(hack, team)
    assert "faculty have been allocated" in services.teams_locked_reason(hack)
    client.force_login(loner.person)
    page = client.get(reverse("my_hackathon", args=[hack.slug])).content.decode()
    assert 'data-testid="no-team-locked"' in page and "Create your team" not in page
    # Only admins switch it.
    supervisor = make_person(email="sup@example.com")
    services.add_supervisor(hack, supervisor)
    client.force_login(supervisor)
    assert client.post(reverse("hackathon_teams_lock", args=[hack.slug]), {"open": "0"}).status_code == 403


# --- 6. Admins: move one-person teams named after their member to Not in a team ------------------------------


def test_one_person_teams_named_after_their_member_are_moved_after_a_preview(client, hack, admin_user, make_person):
    made_up = Team.objects.create(event=hack, name="Asha Example")
    asha = student(hack, 1, "Asha Example", team=made_up)
    by_id = Team.objects.create(event=hack, name="2300000002")
    student(hack, 2, "Ravi Example", team=by_id)
    real = Team.objects.create(event=hack, name="Real Team")
    student(hack, 3, "Kiran Example", team=real)  # one member, but a real name: left alone
    allocated = Team.objects.create(event=hack, name="Meena Example")
    student(hack, 4, "Meena Example", team=allocated)
    allocate(hack, allocated)
    present = Team.objects.create(event=hack, name="Sai Example")
    student(hack, 5, "Sai Example", team=present)
    Attendance.objects.create(round=Round.objects.get(event=hack), team=present, present=True, marked_by=admin_user)

    url = reverse("hackathon_one_person_teams", args=[hack.slug])
    supervisor = make_person(email="sup@example.com")
    services.add_supervisor(hack, supervisor, switches={"can_edit_teams": True})
    client.force_login(supervisor)
    assert client.get(url).status_code == 403
    client.force_login(admin_user)
    assert "Move one-person teams named after their member" in client.get(reverse("hackathon_manage_teams", args=[hack.slug])).content.decode()
    preview = client.get(url).content.decode()
    to_move = preview.split('data-testid="to-move"')[1].split("</ul>")[0]
    kept = preview.split('data-testid="kept"')[1].split("</ul>")[0]
    assert "Asha Example" in to_move and "2300000002" in to_move and "Real Team" not in to_move
    assert "Meena Example: has allocations" in kept and "Sai Example: has attendance" in kept
    client.post(url, {})  # no confirm tick: nothing changes
    assert Team.objects.filter(pk=made_up.pk).exists()
    done = client.post(url, {"confirm": "1"}, follow=True).content.decode()
    assert "2 students moved to Not in a team" in done and "2 teams kept" in done
    assert not Team.objects.filter(pk__in=[made_up.pk, by_id.pk]).exists()
    assert not hasattr(Registration.objects.get(pk=asha.pk), "team_membership")
    assert Team.objects.filter(pk__in=[real.pk, allocated.pk, present.pk]).count() == 3
