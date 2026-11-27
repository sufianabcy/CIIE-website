# ruff: noqa: F811  (fixtures imported from test_hackathon)
"""Replacing a list on import, ticked-rows actions on the Students, Teams and Faculty lists, and allocating a jury panel
to teams (one round, or the same jury in every round). All names and IDs are made up."""

import re

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.db.models import F
from django.urls import reverse

from hackathon import services
from hackathon.models import Assignment, EventFaculty, Room, Round, Supervisor, Team, TeamMember
from people.models import Person
from registrations.models import Registration
from registrations.services import register
from tests.test_hackathon import admin_user, hack, make_faculty, setup, xlsx  # noqa: F401


def student(event, sid, name, team=None):
    person = Person.objects.create_user(email=f"{sid}@kluniversity.in", full_name=name, student_id=sid)
    reg, _ = register(event=event, person=person)
    if team is not None:
        TeamMember.objects.create(team=team, registration=reg)
    return reg


def start(client, event, kind, rows):
    response = client.post(reverse("hackathon_import_start", args=[event.slug]), {"kind": kind, "file": xlsx(rows)})
    assert response.status_code == 302
    return response["Location"]


def said(response):
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


# --- Replace the old list on import -------------------------------------------------------------------


def test_replace_students_removes_those_not_in_the_sheet_and_keeps_judged_ones(client, hack, admin_user):
    room = Room.objects.create(event=hack, name="L502")
    old_team, judged = Team.objects.create(event=hack, name="Old Team"), Team.objects.create(event=hack, name="Judged", room=room)
    gone = student(hack, "2500070001", "Gone Student", old_team)
    kept = student(hack, "2500070002", "Kept Student", judged)
    stays = student(hack, "2500070003", "Stays Student")
    services.allocate_room(Round.objects.get(event=hack, position=1), make_faculty(hack, "Panel"), room)
    client.force_login(admin_user)
    url = start(client, hack, "students", [["Name", "KL ID", "Team"], ["Stays Student", "2500070003", "New Team"],
                                           ["Fresh Student", "2500070004", "New Team"]])
    page = client.get(url).content.decode()
    assert 'name="old_list" value="replace"' in page
    page = client.post(url, {"action": "check", "old_list": "replace"}).content.decode()
    assert 'data-testid="replace-plan"' in page and "1 student not in this sheet will be removed" in page
    assert "Gone Student (2500070001)" in page and "Kept Student (2500070002): their team Judged has allocations" in page
    # Not ticked: nothing happens.
    response = client.post(url, {"action": "import", "old_list": "replace"})
    assert "Tick the box to confirm" in said(response) and not Registration.objects.filter(person__student_id="2500070004").exists()
    client.post(url, {"action": "import", "old_list": "replace", "confirm_replace": "1"})
    for reg, status in ((gone, Registration.Status.CANCELLED), (kept, Registration.Status.CONFIRMED),
                        (stays, Registration.Status.CONFIRMED)):
        reg.refresh_from_db()
        assert reg.status == status
    assert not Team.objects.filter(pk=old_team.pk).exists()  # emptied by the replace and never judged
    assert Team.objects.filter(pk=judged.pk).exists() and kept.team_membership.team == judged
    done = client.get(url).content.decode()
    assert 'data-testid="removed"' in done and "Gone Student" in done and "Not in the new sheet, but kept (1)" in done


def test_add_mode_is_the_default_and_removes_nothing(client, hack, admin_user):
    other = student(hack, "2500070011", "Other Student")
    client.force_login(admin_user)
    url = start(client, hack, "students", [["Name", "KL ID"], ["New Student", "2500070012"]])
    client.post(url, {"action": "import"})
    other.refresh_from_db()
    assert other.status == Registration.Status.CONFIRMED


def test_replace_faculty_keeps_those_with_allocations(client, setup, admin_user):
    event = setup["event"]
    a, b, c = setup["faculty"]
    services.allocate_room(setup["rounds"][0], b, setup["rooms"][0])
    Person.objects.filter(pk=a.person_id).update(employee_id="KLF7001")
    client.force_login(admin_user)
    url = start(client, event, "faculty", [["Name", "Email", "Employee ID"], ["Dr. A", "a@kluniversity.in", "KLF7001"],
                                           ["Dr. New", "new.f@kluniversity.in", "KLF7009"]])
    client.post(url, {"action": "import", "old_list": "replace", "confirm_replace": "1"})
    assert EventFaculty.objects.filter(pk=a.pk).exists() and EventFaculty.objects.filter(pk=b.pk).exists()
    assert not EventFaculty.objects.filter(pk=c.pk).exists()
    assert EventFaculty.objects.filter(event=event, person__employee_id="KLF7009").exists()


def test_replace_teams_makes_them_again_and_keeps_judged_ones(client, setup, admin_user):
    event = setup["event"]
    services.allocate_room(setup["rounds"][0], setup["faculty"][0], setup["rooms"][0])  # Alpha and Beta are judged
    client.force_login(admin_user)
    url = start(client, event, "teams", [["Team", "Member 1 email", "Member 1 name"],
                                         ["Gamma", "g.new@kluniversity.in", "Gamma New"], ["Delta", "d.new@kluniversity.in", "Delta New"]])
    page = client.post(url, {"action": "check", "old_list": "replace"}).content.decode()
    assert "1 current team will be deleted" in page and "Alpha: it has allocations" in page
    client.post(url, {"action": "import", "old_list": "replace", "confirm_replace": "1"})
    names = set(Team.objects.filter(event=event).values_list("name", flat=True))
    assert names == {"Alpha", "Beta", "Gamma", "Delta"}
    assert [m.registration.person.full_name for m in Team.objects.get(event=event, name="Gamma").members.all()] == ["Gamma New"]


def test_replace_needs_the_switch_to_remove(client, setup, make_person):
    event = setup["event"]
    person = make_person(full_name="Sup Example")
    sup, _ = services.add_supervisor(event, person)
    Supervisor.objects.filter(pk=sup.pk).update(can_import=True, can_edit_teams=False)
    client.force_login(person)
    url = start(client, event, "students", [["Name", "KL ID"], ["New Student", "2500070021"]])
    assert 'name="old_list"' not in client.get(url).content.decode()
    client.post(url, {"action": "import", "old_list": "replace", "confirm_replace": "1"})
    assert Registration.objects.filter(event=event, status=Registration.Status.CONFIRMED).count() == 4  # nobody removed


# --- Ticked rows ----------------------------------------------------------------------------------------


def test_students_put_in_a_team_remove_and_email(client, hack, admin_user, django_capture_on_commit_callbacks):
    regs = [student(hack, f"25000710{n:02d}", f"Tick Student {n}") for n in range(1, 5)]
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_students", args=[hack.slug])).content.decode()
    assert 'data-testid="bulk-bar"' in page and f'name="ids" value="{regs[0].pk}"' in page
    url = reverse("hackathon_manage_students_bulk", args=[hack.slug])
    client.post(url, {"do": "team", "ids": [regs[0].pk, regs[1].pk], "new_name": "Ticked Team"})
    team = Team.objects.get(event=hack, name="Ticked Team")
    assert team.members.count() == 2 and team.members.get(is_leader=True).registration == regs[0]
    client.post(url, {"do": "team", "ids": [regs[2].pk], "team": team.pk})
    assert team.members.count() == 3
    response = client.post(url, {"do": "team", "ids": [regs[3].pk], "team": team.pk, "new_name": "Both"})
    assert "not both" in said(response)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {"do": "email", "ids": [regs[0].pk, regs[3].pk]})
    assert sorted(m.to[0] for m in mail.outbox[-2:]) == ["2500071001@kluniversity.in", "2500071004@kluniversity.in"]
    client.post(url, {"do": "remove", "ids": [regs[2].pk, regs[3].pk]})
    statuses = [Registration.objects.get(pk=r.pk).status for r in regs]
    assert statuses == ["confirmed", "confirmed", "cancelled", "cancelled"] and team.members.count() == 2


def test_teams_move_to_a_room_and_delete(client, setup, admin_user):
    event = setup["event"]
    alpha, beta, gamma = (setup["teams"][n] for n in ("Alpha", "Beta", "Gamma"))
    r1, r2 = setup["rooms"]
    services.allocate_room(setup["rounds"][0], setup["faculty"][0], r1)  # Dr. A judges Alpha and Beta in R1
    client.force_login(admin_user)
    url = reverse("hackathon_manage_teams_bulk", args=[event.slug])
    assert 'data-testid="bulk-bar"' in client.get(reverse("hackathon_manage_teams", args=[event.slug])).content.decode()
    response = client.post(url, {"do": "room", "ids": [alpha.pk], "room": r2.pk})
    assert "two rooms" in said(response)
    alpha.refresh_from_db()
    assert alpha.room == r1
    client.post(url, {"do": "room", "ids": [alpha.pk, beta.pk], "room": r2.pk})  # the whole group moves together
    assert set(Team.objects.filter(room=r2).values_list("name", flat=True)) == {"Alpha", "Beta", "Gamma"}
    Room.objects.filter(pk=r1.pk).update(capacity=1)
    response = client.post(url, {"do": "room", "ids": [gamma.pk, alpha.pk, beta.pk], "room": r1.pk})
    assert "holds 1 teams" in said(response)
    response = client.post(url, {"do": "delete", "ids": [alpha.pk, gamma.pk]})
    assert "Alpha has faculty allocated, so it was kept" in said(response)
    assert Team.objects.filter(pk=alpha.pk).exists() and not Team.objects.filter(pk=gamma.pk).exists()


def test_faculty_remove_and_email(client, setup, admin_user, django_capture_on_commit_callbacks):
    event = setup["event"]
    a, b, c = setup["faculty"]
    services.allocate_room(setup["rounds"][0], a, setup["rooms"][0])
    client.force_login(admin_user)
    url = reverse("hackathon_manage_faculty_bulk", args=[event.slug])
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {"do": "email", "ids": [b.pk, c.pk]})
    assert sorted(m.to[0] for m in mail.outbox[-2:]) == ["b@kluniversity.in", "c@kluniversity.in"]
    assert re.search(r"Temporary password: \S+", mail.outbox[-1].body)
    client.post(url, {"do": "remove", "ids": [a.pk, b.pk]})
    a.refresh_from_db()
    assert not a.is_active and not EventFaculty.objects.filter(pk=b.pk).exists()  # A has allocations: switched off


def test_ticked_actions_need_their_switches(client, setup, make_person):
    event = setup["event"]
    person = make_person(full_name="Sup Example")
    sup, _ = services.add_supervisor(event, person)
    Supervisor.objects.filter(pk=sup.pk).update(can_edit_teams=False, can_add_faculty=False, can_make_logins=False)
    client.force_login(person)
    for page in ("hackathon_manage_students", "hackathon_manage_teams", "hackathon_manage_faculty"):
        assert 'data-testid="bulk-bar"' not in client.get(reverse(page, args=[event.slug])).content.decode()
    alpha = setup["teams"]["Alpha"]
    assert client.post(reverse("hackathon_manage_teams_bulk", args=[event.slug]), {"do": "delete", "ids": [alpha.pk]}).status_code == 403
    role = setup["faculty"][2]
    assert client.post(reverse("hackathon_manage_faculty_bulk", args=[event.slug]), {"do": "remove", "ids": [role.pk]}).status_code == 403
    reg = TeamMember.objects.filter(team=alpha).first().registration
    assert client.post(reverse("hackathon_manage_students_bulk", args=[event.slug]), {"do": "remove", "ids": [reg.pk]}).status_code == 403
    assert Team.objects.filter(pk=alpha.pk).exists() and EventFaculty.objects.filter(pk=role.pk).exists()


# --- Jury panels ---------------------------------------------------------------------------------------


def panel(client, event, faculty, teams, round_=None):
    data = {"faculty": [f.pk for f in faculty], "teams": [t.pk for t in teams]}
    data.update({"rounds": "one", "round": round_.pk} if round_ else {"rounds": "all"})
    return client.post(reverse("hackathon_allocate_teams", args=[event.slug]), data)


def test_a_jury_panel_judges_the_ticked_teams_in_every_round(client, setup, admin_user):
    event = setup["event"]
    a, b, c = setup["faculty"]
    alpha, beta = setup["teams"]["Alpha"], setup["teams"]["Beta"]
    client.force_login(admin_user)
    assert 'data-testid="allocate-teams"' in client.get(reverse("hackathon_manage_allocations", args=[event.slug])).content.decode()
    response = panel(client, event, [a, b], [alpha, beta])
    assert "8 allocations made in every round" in said(response)  # 2 faculty x 2 teams x 2 rounds
    for r in setup["rounds"]:
        assert set(Assignment.objects.filter(round=r).values_list("faculty__person__full_name", "team__name")) == {
            ("Dr. A", "Alpha"), ("Dr. A", "Beta"), ("Dr. B", "Alpha"), ("Dr. B", "Beta")}


def test_a_jury_panel_for_one_round_and_the_one_room_rule(client, setup, admin_user):
    event = setup["event"]
    a, b, c = setup["faculty"]
    alpha, gamma = setup["teams"]["Alpha"], setup["teams"]["Gamma"]
    round1, round2 = setup["rounds"]
    client.force_login(admin_user)
    response = panel(client, event, [a], [alpha, gamma], round1)
    assert "Pick teams from one room" in said(response) and not Assignment.objects.exists()
    panel(client, event, [a], [alpha], round1)
    assert list(Assignment.objects.values_list("round", flat=True)) == [round1.pk]  # only that round
    # Dr. A already judges in R1 in Round 1: R2 in every round is refused, and nothing is made in Round 2 either.
    response = panel(client, event, [a], [gamma])
    assert "Nothing was allocated" in said(response) and Assignment.objects.count() == 1
    # Dr. C mentors Gamma: skipped with a note; Dr. B still gets it.
    response = panel(client, event, [b, c], [gamma], round2)
    assert "they mentor it" in said(response)
    assert list(Assignment.objects.filter(round=round2).values_list("faculty__person__full_name", flat=True)) == ["Dr. B"]


def test_jury_panels_need_the_allocations_switch(client, setup, make_person):
    event = setup["event"]
    person = make_person(full_name="Sup Example")
    sup, _ = services.add_supervisor(event, person)
    Supervisor.objects.filter(pk=sup.pk).update(can_edit_allocations=False)
    client.force_login(person)
    assert 'data-testid="allocate-teams"' not in client.get(reverse("hackathon_manage_allocations", args=[event.slug])).content.decode()
    assert panel(client, event, [setup["faculty"][0]], [setup["teams"]["Alpha"]]).status_code == 403
    assert not Assignment.objects.exists()


@pytest.mark.parametrize("kind", ["students", "teams", "faculty"])
def test_the_replace_choice_is_offered_on_each_import(client, hack, admin_user, kind):
    client.force_login(admin_user)
    url = start(client, hack, kind, [["Name", "KL ID", "Team", "Email"], ["Some One", "2500079999", "Some Team", "s.one@kluniversity.in"]])
    assert "The list you have now" in client.get(url).content.decode()


def auto(client, event, round_, **data):
    body = {"round": round_.pk, "per_team": 1, "only_unallocated": "on", "back": "allocations", **data}
    return client.post(reverse("hackathon_random_allocate", args=[event.slug]), body)


def test_automatic_juries_get_the_chosen_number_of_teams_from_one_room(client, setup, admin_user):
    event = setup["event"]
    round1, round2 = setup["rounds"]
    client.force_login(admin_user)
    assert 'data-testid="allocate-auto"' in client.get(reverse("hackathon_manage_allocations", args=[event.slug])).content.decode()
    response = auto(client, event, round1, teams_per_jury=2, every_round="on")
    assert response["Location"] == reverse("hackathon_manage_allocations", args=[event.slug])
    for r in (round1, round2):
        by_faculty = {}
        for a in Assignment.objects.filter(round=r).select_related("team"):
            by_faculty.setdefault(a.faculty_id, set()).add(a.team.room_id)
        assert all(len(rooms) == 1 for rooms in by_faculty.values())  # each jury in one room
    alpha_judges = set(Assignment.objects.filter(round=round1, team__name="Alpha").values_list("faculty", flat=True))
    beta_judges = set(Assignment.objects.filter(round=round1, team__name="Beta").values_list("faculty", flat=True))
    assert alpha_judges == beta_judges and len(alpha_judges) == 1  # one jury of one took both R1 teams
    assert not Assignment.objects.filter(team__mentor=F("faculty__person")).exists()  # never their own mentee
    # The same juries and teams in round 2.
    assert set(Assignment.objects.filter(round=round2).values_list("faculty", "team")) == set(
        Assignment.objects.filter(round=round1).values_list("faculty", "team"))


def test_teams_left_over_when_there_are_not_enough_juries(client, setup, admin_user):
    event = setup["event"]
    round1 = setup["rounds"][0]
    client.force_login(admin_user)
    response = auto(client, event, round1, per_team=2, teams_per_jury=2)  # 3 faculty: one jury of 2
    assert "1 team(s) not allocated" in said(response) and "Gamma" in said(response)
    assert set(Assignment.objects.values_list("team__name", flat=True)) == {"Alpha", "Beta"}
    assert Assignment.objects.values("faculty").distinct().count() == 2
