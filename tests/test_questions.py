import pytest
from django.core.exceptions import ValidationError

from events.models import Question
from registrations.services import register


def test_questions_lock_after_first_registration(event, make_person):
    q = Question.objects.create(event=event, key="team", label="Team name")
    register(event=event, person=make_person(), answers={"team": "Rockets"})

    q.label = "Team name (fixed typo)"
    q.full_clean()  # typo fixes are fine

    q.kind = Question.Kind.NUMBER
    with pytest.raises(ValidationError):
        q.full_clean()


def test_new_question_after_lock_must_be_optional(event, make_person):
    register(event=event, person=make_person())
    required = Question(event=event, key="tshirt", label="T-shirt size", required=True)
    with pytest.raises(ValidationError):
        required.full_clean()
    optional = Question(event=event, key="tshirt", label="T-shirt size")
    optional.full_clean()
    optional.save()
    event.refresh_from_db()
    assert event.form_version == 2
    assert optional.added_in_version == 2


def test_choices_can_be_added_but_not_removed(event, make_person):
    q = Question.objects.create(event=event, key="stage", label="Stage", kind="choice", options=["Idea", "Prototype"])
    register(event=event, person=make_person(), answers={"stage": "Idea"})
    q.options = ["Idea", "Prototype", "Has users"]
    q.full_clean()
    q.options = ["Prototype"]
    with pytest.raises(ValidationError):
        q.full_clean()
