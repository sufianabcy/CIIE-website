# ruff: noqa: F811  (fixtures imported from test_hackathon)
from decimal import Decimal

from django.contrib.messages import get_messages
from django.urls import reverse

from events.exports import build_export
from hackathon import rubric, services
from hackathon.forms import MarksForm
from hackathon.models import Assignment, Round, RoundOutcome, Score
from tests.test_hackathon import admin_user, hack, setup, sheet  # noqa: F401


def round_post(round_, criteria, **extra):
    """POST data for the round page: the round's fields plus the criteria formset."""
    data = {"name": round_.name, "position": round_.position, "total_marks": "100", "prompts_text": "One strength\nOne test",
            "c-TOTAL_FORMS": str(len(criteria)), "c-INITIAL_FORMS": "0", "c-MIN_NUM_FORMS": "0", "c-MAX_NUM_FORMS": "1000"}
    for i, (label, max_marks) in enumerate(criteria):
        data.update({f"c-{i}-position": i + 1, f"c-{i}-label": label, f"c-{i}-max_marks": max_marks, f"c-{i}-tag": "Q3",
                     f"c-{i}-strong_text": "Clear evidence", f"c-{i}-weak_text": "No evidence"})
    data.update(extra)
    return data


def submit(assignment, marks, **extras):
    services.save_marks(assignment, marks, "", True, extras or None)


# --- Builder ---------------------------------------------------------------------------------------


def test_rubric_builder_warns_when_criteria_do_not_add_up_and_saves_descriptors(client, hack, admin_user):
    r3 = Round.objects.create(event=hack, name="Round 3", position=3)
    client.force_login(admin_user)
    url = reverse("hackathon_manage_round_edit", args=[hack.slug, r3.pk])
    response = client.post(url, round_post(r3, [("Problem", "30"), ("Evidence", "40")]), follow=True)
    warnings = [str(m) for m in get_messages(response.wsgi_request)]
    assert any("add up to 70, but Round 3 is out of 100" in w for w in warnings)
    problem = r3.criteria.get(label="Problem")
    assert (problem.tag, problem.strong_text, problem.weak_text) == ("Q3", "Clear evidence", "No evidence")
    r3.refresh_from_db()
    assert r3.comment_prompts == ["One strength", "One test"]
    assert rubric.total_problem(r3)


def test_default_bands_and_band_labels(hack):
    assert [b["name"] for b in rubric.bands(hack)] == ["Strong", "Adequate", "Weak", "Missing"]
    assert rubric.band_for(hack, Decimal("26"), Decimal("30")) == "Strong"  # 87%
    assert rubric.band_for(hack, 18, 30) == "Adequate"  # 60%
    assert rubric.band_for(hack, 10, 30) == "Weak"
    assert rubric.band_for(hack, 0, 30) == "Missing"
    assert rubric.band_for(hack, None, 30) == ""


def test_rubric_settings_bands_and_weights_must_add_up(client, setup, admin_user):
    event = setup["event"]
    r1, r2 = setup["rounds"]
    client.force_login(admin_user)
    url = reverse("hackathon_rubric_settings", args=[event.slug])
    base = {"bands_text": "Top 80\nLow 0", "red_flags_text": "Copied idea", "self_score_max": "50",
            "recommendations_text": "Pursue as a startup"}
    bad = client.post(url, {**base, f"w_{r1.pk}": "0.5", f"w_{r2.pk}": "0.4"})
    assert bad.status_code == 200 and "must add up to 1" in bad.content.decode()
    client.post(url, {**base, f"w_{r1.pk}": "0.4", f"w_{r2.pk}": "0.6"})
    assert [b["name"] for b in rubric.bands(event)] == ["Top", "Low"]
    assert rubric.settings_for(event).red_flags == ["Copied idea"]
    r1.refresh_from_db()
    assert r1.weight == Decimal("0.40") and not rubric.weights_problem(event)


def test_rubric_locks_once_marks_exist_and_only_an_admin_unlocks(client, setup, admin_user):
    event = setup["event"]
    a = setup["faculty"][0]
    round1 = setup["rounds"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    idea, demo = round1.criteria.all()
    services.save_marks(a.assignments.first(), {idea: 5, demo: None}, "", False)
    assert rubric.is_locked(round1)
    client.force_login(admin_user)
    url = reverse("hackathon_manage_round_edit", args=[event.slug, round1.pk])
    client.post(url, round_post(round1, [("Changed", "100")]))
    assert list(round1.criteria.values_list("label", flat=True)) == ["Idea", "Demo"]
    client.post(url, {"action": "unlock"})
    round1.refresh_from_db()
    assert not round1.rubric_unlocked
    client.post(url, {"action": "unlock", "confirm": "1"})
    round1.refresh_from_db()
    assert round1.rubric_unlocked and not rubric.is_locked(round1)


def test_copy_rubric_from_another_hackathon(setup, make_event):
    from events.models import Event

    source = setup["event"]
    settings_row = rubric.settings_for(source)
    settings_row.red_flags = ["No user tested"]
    settings_row.save()
    target = make_event(title="Second Hack", kind=Event.Kind.HACKATHON)
    assert rubric.copy_rubric(source, target) == 2
    copied = Round.objects.get(event=target, position=1)
    assert list(copied.criteria.values_list("label", "max_marks")) == [("Idea", 10), ("Demo", 20)]
    assert rubric.settings_for(target).red_flags == ["No user tested"]


# --- Marksheet -------------------------------------------------------------------------------------


def test_marksheet_checks_range_and_carries_prompts_flags_self_score(client, setup):
    event = setup["event"]
    a = setup["faculty"][0]
    round1 = setup["rounds"][0]
    settings_row = rubric.settings_for(event)
    settings_row.red_flags = ["Copied idea", "No users met"]
    settings_row.save()
    services.allocate_room(round1, a, setup["rooms"][0])
    assignment = a.assignments.get(team__name="Alpha")
    idea, demo = round1.criteria.all()
    client.force_login(a.person)
    url = reverse("hackathon_mark", args=[assignment.pk])
    page = client.get(url).content.decode()
    assert "One strength to tell the team" in page and "Copied idea" in page and "out of 50" in page
    assert "Recommendation" not in page  # only in the final round
    too_high = client.post(url, {f"c_{idea.pk}": "11", f"c_{demo.pk}": "5", "save": "1"})
    assert too_high.status_code == 200 and not Score.objects.filter(assignment=assignment).exists()
    client.post(url, {f"c_{idea.pk}": "9", f"c_{demo.pk}": "18", "comment_0": "Great problem", "comment_1": "Test pricing",
                      "red_flags": ["No users met"], "self_score": "40", "submit": "1"})
    assignment.refresh_from_db()
    assert assignment.is_submitted and assignment.total() == 27
    assert assignment.comments == {"One strength to tell the team": "Great problem", "One thing the team must test next": "Test pricing"}
    assert assignment.red_flags == ["No users met"] and assignment.self_score == 40
    assert rubric.band_for(event, 9, idea.max_marks) == "Strong"


def test_recommendation_only_in_the_final_round(setup):
    round2 = setup["rounds"][1]
    a = setup["faculty"][0]
    assignment = Assignment(round=round2, faculty=a, team=setup["teams"]["Alpha"])
    form = MarksForm(criteria=round2.criteria.all(), scores={}, assignment=assignment,
                     extras={"recommendations": rubric.settings_for(setup["event"]).recommendations})
    assert [c for c, _ in form.fields["recommendation"].choices][1:] == ["Pursue as a startup", "Refine and re-test", "Pivot to a new problem"]


# --- Scores and advancement ------------------------------------------------------------------------


def test_team_score_averages_two_judges(setup):
    a, b, _ = setup["faculty"]
    round1 = setup["rounds"][0]
    idea, demo = round1.criteria.all()
    for f in (a, b):
        services.allocate_room(round1, f, setup["rooms"][0])
    submit(a.assignments.get(team__name="Alpha"), {idea: 8, demo: 12})
    submit(b.assignments.get(team__name="Alpha"), {idea: 6, demo: 16})
    score = rubric.team_score(round1, setup["teams"]["Alpha"])
    assert score["total"] == 21 and score["judges"] == 2
    assert score["criteria"] == {idea.pk: 7, demo.pk: 14}
    assert rubric.percent(round1, score["total"]) == 70


def test_advancement_rule_with_key_criterion_band_and_override(client, setup, admin_user):
    event = setup["event"]
    a = setup["faculty"][0]
    round1, round2 = setup["rounds"]
    idea, demo = round1.criteria.all()
    round1.min_total, round1.key_criterion, round1.key_min_band, round1.allow_override = 15, idea, "Adequate", True
    round1.save()
    services.allocate_room(round1, a, setup["rooms"][0])
    alpha, beta = setup["teams"]["Alpha"], setup["teams"]["Beta"]
    submit(a.assignments.get(team=alpha), {idea: 7, demo: 15})
    submit(a.assignments.get(team=beta), {idea: 3, demo: 19}, advance_anyway=True)  # total 22 but Idea is Weak
    assert rubric.meets_rule(round1, rubric.team_score(round1, alpha)) == (True, [])
    meets, why = rubric.meets_rule(round1, rubric.team_score(round1, beta))
    assert not meets and "Idea is Weak, needs at least Adequate" in why[0]
    client.force_login(admin_user)
    url = reverse("hackathon_advance", args=[event.slug, round1.pk])
    page = client.get(url).content.decode()
    assert "Alpha" in page and "Beta" in page and "anyway" in page.lower()
    client.post(url, {"advance": [alpha.pk, beta.pk]})  # no confirm: nothing decided
    assert not RoundOutcome.objects.exists()
    client.post(url, {"advance": [alpha.pk], "confirm": "1"})
    assert RoundOutcome.objects.get(round=round1, team=alpha).advanced
    assert not RoundOutcome.objects.get(round=round1, team=beta).advanced
    assert rubric.eligible_team_ids(round2) == {alpha.pk}


def test_advance_page_is_admins_only(client, setup):
    a = setup["faculty"][0]
    client.force_login(a.person)
    url = reverse("hackathon_advance", args=[setup["event"].slug, setup["rounds"][0].pk])
    assert client.get(url).status_code in (403, 404)


def test_only_advanced_teams_reach_the_next_round(client, setup, admin_user, make_person):
    event = setup["event"]
    a = setup["faculty"][0]
    round1, round2 = setup["rounds"]
    alpha, beta = setup["teams"]["Alpha"], setup["teams"]["Beta"]
    services.allocate_room(round1, a, setup["rooms"][0])
    for team in (alpha, beta):
        RoundOutcome.objects.create(round=round1, team=team, advanced=team == alpha, decided_by=admin_user)
    made, notes = services.allocate_room(round2, a, setup["rooms"][0])
    assert made == 1 and any("Beta" in n and "did not advance" in n for n in notes)
    assert list(Assignment.objects.filter(round=round2).values_list("team__name", flat=True)) == ["Alpha"]
    # A leftover allocation for a team that did not go on is hidden from the faculty member.
    stale = Assignment.objects.create(round=round2, faculty=a, team=beta)
    client.force_login(a.person)
    assert client.get(reverse("hackathon_mark", args=[stale.pk])).status_code == 404
    page = client.get(reverse("hackathon_judge_event", args=[event.slug])).content.decode()
    assert reverse("hackathon_mark", args=[stale.pk]) not in page
    # The floor list for round 2 leaves Beta out.
    from hackathon.rooms import floors

    data, _ = floors(event, round2)
    names = [t["team"].name for _, rooms in data for r in rooms for t in r["teams"]]
    assert "Alpha" in names and "Beta" not in names
    # Students see the outcome.
    for team, text in ((alpha, "Advanced to Round 2"), (beta, "Not advanced")):
        student = team.members.first().registration.person
        client.force_login(student)
        assert text in client.get(reverse("my_hackathon", args=[event.slug])).content.decode()


# --- Final marks Excel -----------------------------------------------------------------------------


def test_final_sheet_weighted_total_rank_and_round_sheets(setup, admin_user):
    event = setup["event"]
    a = setup["faculty"][0]
    round1, round2 = setup["rounds"]
    round2.total_marks = 5
    round2.save()
    idea, demo = round1.criteria.all()
    pitch = round2.criteria.get()
    settings_row = rubric.settings_for(event)
    settings_row.red_flags = ["Copied idea"]
    settings_row.save()
    services.allocate_room(round1, a, setup["rooms"][0])
    alpha, beta = setup["teams"]["Alpha"], setup["teams"]["Beta"]
    submit(a.assignments.get(team=alpha), {idea: 9, demo: 18}, self_score=Decimal(40),
           comments={"One strength to tell the team": "Clear", "One thing the team must test next": "Price"})
    submit(a.assignments.get(team=beta), {idea: 5, demo: 10}, red_flags=["Copied idea"])
    for team in (alpha, beta):
        RoundOutcome.objects.create(round=round1, team=team, advanced=team == alpha, decided_by=admin_user)
    Round.objects.filter(pk=round2.pk).update(opens_at=round1.opens_at, closes_at=round1.closes_at)
    round2.refresh_from_db()
    services.allocate_room(round2, a, setup["rooms"][0])
    submit(a.assignments.get(round=round2, team=alpha), {pitch: 4}, recommendation="Pursue as a startup")
    _, content = build_export(event, "marks", admin_user)
    final = sheet(content, "Final")
    header = final[0]
    assert header[:4] == ("Team", "Members", "Members' KL IDs", "Room")
    rows = {r[0]: r for r in final[1:]}
    # Alpha: Round 1 = 27/30 = 90, Round 2 = 4/5 = 80; weights default to half each with two rounds.
    assert rows["Alpha"][header.index("Round 1 (out of 100)")] == 90
    assert rows["Alpha"][header.index("Round 2 (out of 100)")] == 80
    assert rows["Alpha"][header.index("Weighted total (out of 100)")] == 85
    assert rows["Alpha"][header.index("Rank")] == 1 and rows["Beta"][header.index("Rank")] == 2
    assert rows["Alpha"][header.index("Furthest round reached")] == "Round 2"
    assert rows["Beta"][header.index("Furthest round reached")] == "Round 1"
    assert rows["Beta"][header.index("Weighted total (out of 100)")] is None
    assert rows["Alpha"][header.index("Self-score gap (faculty % minus self %)")] == 10  # 90 - 80
    assert rows["Alpha"][header.index("Recommendation")] == "Pursue as a startup"
    assert rows["Beta"][header.index("Red flags")] == 1
    r1 = sheet(content, "R1 Round 1")
    h = r1[0]
    for col in ("Idea (/10)", "Idea band", "Total", "One strength to tell the team", "Red flags", "Self-score", "Evaluator",
                "Employee ID", "Submitted at"):
        assert col in h
    alpha_row = next(r for r in r1[1:] if r[0] == "Alpha")
    assert alpha_row[h.index("Idea band")] == "Strong" and alpha_row[h.index("One strength to tell the team")] == "Clear"
    rub = sheet(content, "Rubric")
    assert (None, "Idea", None, 10) == rub[2][:4] and any(r[0] == "Strong" and r[1] == 85 for r in rub)


def test_volunteers_cannot_download_marks(client, setup, make_person):
    volunteer = make_person(full_name="Vol One")
    services.add_volunteer(setup["event"], volunteer)
    client.force_login(volunteer)
    assert client.get(reverse("event_export", args=[setup["event"].slug, "marks"])).status_code == 403
