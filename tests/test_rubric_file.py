# ruff: noqa: F811  (fixtures imported from test_hackathon)
"""Rubric files: download a hackathon's rubric as JSON, and import one (preview, confirm, all or nothing).

The rubric used here is tests/rubric_sample.json, with made-up text only.
"""

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from hackathon import rubric, rubric_file, services
from hackathon.models import Round, Score
from tests.test_hackathon import admin_user, hack, make_faculty, setup  # noqa: F401

SAMPLE = Path(__file__).with_name("rubric_sample.json")


def sample() -> dict:
    return json.loads(SAMPLE.read_text())


def as_file(data, name="rubric.json"):
    raw = data if isinstance(data, (str, bytes)) else json.dumps(data)
    return SimpleUploadedFile(name, raw.encode() if isinstance(raw, str) else raw, content_type="application/json")


def import_sample(event, data=None):
    return rubric_file.apply(event, rubric_file.parse(json.dumps(data or sample())))


def preview(client, event, data):
    return client.post(reverse("hackathon_rubric_import", args=[event.slug]), {"action": "preview", "file": as_file(data)})


def confirm(client, event, data, tick=True):
    body = {"action": "import", "content": json.dumps(data)}
    if tick:
        body["confirm"] = "1"
    return client.post(reverse("hackathon_rubric_import", args=[event.slug]), body, follow=True)


def test_preview_then_confirm_imports_the_whole_rubric(client, hack, admin_user):
    client.force_login(admin_user)
    times = {r.position: (r.opens_at, r.closes_at) for r in Round.objects.filter(event=hack)}
    page = preview(client, hack, sample()).content.decode()
    # The preview lists rounds, criteria with their max marks, bands and weights, and warns about the existing rubric.
    assert page.count('data-testid="preview-round"') == 3
    assert "Sample criterion A" in page and ">60<" in page and "Weight 0.2" in page
    assert "Bright</strong> 80–100%" in page
    assert "already has rubric content" in page
    assert "A new round, with no times set" in page
    # Nothing is saved before the confirm, and the confirm box must be ticked.
    assert not Round.objects.filter(event=hack, name="Sample round one").exists()
    confirm(client, hack, sample(), tick=False)
    assert not Round.objects.filter(event=hack, name="Sample round one").exists()
    response = confirm(client, hack, sample())
    assert "Rubric imported: 3 round(s), 3 bands." in response.content.decode()
    rounds = list(Round.objects.filter(event=hack).prefetch_related("criteria"))
    assert [r.name for r in rounds] == ["Sample round one", "Sample round two", "Sample round three"]
    one, two, three = rounds
    # Existing times stay; the new round has none.
    assert (one.opens_at, one.closes_at) == times[1] and (two.opens_at, two.closes_at) == times[2]
    assert three.opens_at is None and three.closes_at is None and three.position == 3
    assert [(c.label, c.max_marks, c.tag) for c in one.criteria.all()] == [
        ("Sample criterion A", Decimal(60), "S1"), ("Sample criterion B", Decimal(40), "")]
    assert one.key_criterion.label == "Sample criterion A" and one.key_min_band == "Fair" and one.min_total == 50
    assert not one.allow_override and two.allow_override and two.key_criterion is None and two.min_total == 60
    assert three.min_total is None and three.key_criterion is None
    assert [r.weight for r in rounds] == [Decimal("0.20"), Decimal("0.30"), Decimal("0.50")] and not rubric.weights_problem(hack)
    assert one.comment_prompts == ["Sample prompt: a good point", "Sample prompt: a next step"]
    assert two.criteria.first().tag == "S2 and a longer sample tag"
    settings_row = rubric.settings_for(hack)
    assert settings_row.guidance.startswith("Sample guidance") and settings_row.self_score_max == 20
    assert settings_row.red_flags == ["Sample flag one.", "Sample flag two."]
    assert [b["name"] for b in rubric.bands(hack)] == ["Bright", "Fair", "Dim"]
    assert rubric.band_for(hack, 45, 60) == "Fair"  # 75%


def test_download_then_import_gives_the_same_rubric(client, hack, admin_user, make_event):
    from events.models import Event

    import_sample(hack)
    client.force_login(admin_user)
    response = client.get(reverse("hackathon_rubric_download", args=[hack.slug]))
    assert response.status_code == 200 and response["Content-Disposition"].endswith('-rubric.json"')
    downloaded = json.loads(response.content)
    assert downloaded == sample()  # the file that was imported comes back the same
    other = make_event(title="Second Hack", kind=Event.Kind.HACKATHON)
    import_sample(other, downloaded)
    assert rubric_file.export_rubric(other) == downloaded


def test_download_of_a_rubric_built_on_the_portal_can_be_imported(hack):
    data = rubric_file.export_rubric(hack)
    assert data["format"] == "kl-ciie-rubric" and data["version"] == 1
    assert [b["name"] for b in data["bands"]] == ["Strong", "Adequate", "Weak", "Missing"]
    assert data["bands"][1]["max_percent"] == 84
    assert [c["max"] for c in data["rounds"][0]["criteria"]] == [10, 20]
    data["rounds"][0]["total"] = 30  # the fixture's round totals don't match its criteria; a file must be consistent
    data["rounds"][1]["total"] = 5
    spec = rubric_file.parse(json.dumps(data))
    assert len(spec["rounds"]) == 2


def test_a_round_with_marks_is_locked_and_nothing_is_imported(client, setup, admin_user):
    event = setup["event"]
    round1, round2 = setup["rounds"]
    a = setup["faculty"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    idea, demo = round1.criteria.all()
    services.save_marks(a.assignments.first(), {idea: 5, demo: 10}, "", False)
    assert Score.objects.exists()
    client.force_login(admin_user)
    page = preview(client, event, sample()).content.decode()
    assert "Marks already exist in Round 1" in page and "Import the rubric" not in page
    response = confirm(client, event, sample())
    assert "Nothing was imported: marks already exist in Round 1" in response.content.decode()
    # All or nothing: round 2 (no marks) and the settings are unchanged too.
    round2.refresh_from_db()
    assert round2.name == "Round 2" and list(round2.criteria.values_list("label", flat=True)) == ["Pitch"]
    assert not rubric.settings_for(event).guidance
    assert Round.objects.filter(event=event).count() == 2


def test_reimporting_the_same_file_after_marks_exist_is_allowed(setup):
    event = setup["event"]
    import_sample(event)
    round1 = Round.objects.get(event=event, position=1)
    a = setup["faculty"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    first = round1.criteria.first()
    services.save_marks(a.assignments.first(), {first: 30}, "", False)
    result = import_sample(event)  # no round changes, so nothing is locked
    assert not result["locked"] and Score.objects.filter(criterion=first).exists()
    changed = sample()
    changed["rounds"][0]["criteria"][0]["strong"] = "Changed sample text"
    with pytest.raises(rubric_file.RubricFileError, match="Sample round one"):
        import_sample(event, changed)


def _problems(data):
    with pytest.raises(rubric_file.RubricFileError) as caught:
        rubric_file.parse(data if isinstance(data, str) else json.dumps(data))
    return " | ".join(caught.value.problems)


def test_clear_errors_for_bad_files():
    assert "couldn't be read as JSON" in _problems("{not json")
    bad = sample()
    bad["format"] = "something-else"
    assert "isn't a KL-CIIE rubric file" in _problems(bad)
    bad = sample()
    bad["version"] = 2
    assert "version 2" in _problems(bad)
    bad = sample()
    bad["rounds"][1]["criteria"][0]["max"] = 45
    assert "Round 2 (Sample round two): the criteria add up to 95, but the round total is 100." in _problems(bad)
    bad = sample()
    bad["round_weights"] = [0.2, 0.3, 0.4]
    assert "The round weights add up to 0.9; they must add up to 1." in _problems(bad)
    bad = sample()
    bad["round_weights"] = [0.5, 0.5]
    assert "give one per round" in _problems(bad)
    bad = sample()
    bad["rounds"][0]["advance"]["key_criterion"] = "No such criterion"
    assert "names the criterion \"No such criterion\", which isn't in this round" in _problems(bad)
    bad = sample()
    bad["rounds"][0]["advance"]["key_min_band"] = "Shiny"
    assert "names the band \"Shiny\", which isn't one of the bands" in _problems(bad)
    bad = sample()
    bad["bands"][1]["max_percent"] = 85
    assert "Bands Fair (50–85%) and Bright (80–100%) overlap." in _problems(bad)
    # Several problems are listed together.
    bad = sample()
    bad["version"] = 1
    bad["round_weights"] = [1, 1, 1]
    bad["bands"][0]["name"] = ""
    text = _problems(bad)
    assert "weights add up to 3" in text and "Band 1 has no name" in text


def test_bad_file_shows_the_errors_on_the_page(client, hack, admin_user):
    client.force_login(admin_user)
    bad = sample()
    bad["round_weights"] = [0.2, 0.3, 0.4]
    page = preview(client, hack, bad).content.decode()
    assert "This file can't be imported" in page and "must add up to 1" in page
    page = client.post(reverse("hackathon_rubric_import", args=[hack.slug]), {"action": "preview"}).content.decode()
    assert "Choose a rubric file" in page


def test_only_admins_and_supervisors_with_the_rubric_switch(client, setup, make_person):
    from hackathon.models import Supervisor

    event = setup["event"]
    person = make_person(full_name="Sup Example")
    sup, _ = services.add_supervisor(event, person)
    client.force_login(person)
    for name in ("hackathon_rubric_import", "hackathon_rubric_download"):
        assert client.get(reverse(name, args=[event.slug])).status_code == 403
    assert preview(client, event, sample()).status_code == 403
    Supervisor.objects.filter(pk=sup.pk).update(can_manage_fields=True)
    assert client.get(reverse("hackathon_rubric_download", args=[event.slug])).status_code == 200
    assert "Download this rubric as a file" in client.get(reverse("hackathon_rubric_settings", args=[event.slug])).content.decode()
    volunteer = make_person(full_name="Vol Example")
    services.add_volunteer(event, volunteer)
    client.force_login(volunteer)
    assert client.get(reverse("hackathon_rubric_download", args=[event.slug])).status_code == 403


def test_guidance_and_band_meanings_show_on_the_marksheet(client, setup):
    event = setup["event"]
    import_sample(event)
    round1 = Round.objects.get(event=event, position=1)
    a = setup["faculty"][0]
    services.allocate_room(round1, a, setup["rooms"][0])
    client.force_login(a.person)
    page = client.get(reverse("hackathon_mark", args=[a.assignments.first().pk])).content.decode()
    assert 'data-testid="marksheet-guidance"' in page and "Sample guidance for judges" in page
    assert "Sample meaning for the top band" in page
    assert "/ 60<" in page  # max marks shown plainly, not as 6E+1


def test_rubric_settings_form_keeps_meanings_and_allows_no_self_score(client, hack, admin_user):
    import_sample(hack)
    client.force_login(admin_user)
    rounds = list(Round.objects.filter(event=hack))
    data = {"guidance": "Changed sample guidance", "bands_text": "Bright 80\nFair 50\nDim 0", "red_flags_text": "",
            "self_score_max": "", "recommendations_text": ""}
    data.update({f"w_{r.pk}": str(r.weight) for r in rounds})
    response = client.post(reverse("hackathon_rubric_settings", args=[hack.slug]), data)
    assert response.status_code == 302
    settings_row = rubric.settings_for(hack)
    assert settings_row.self_score_max is None and settings_row.guidance == "Changed sample guidance"
    assert rubric_file.export_rubric(hack)["bands"] == copy.deepcopy(sample()["bands"])
