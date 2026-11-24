from django import forms

from events.models import Question


def _field_for(question: Question) -> forms.Field:
    common = {"label": question.label, "help_text": question.help_text, "required": question.required}
    kind = question.kind
    if kind == Question.Kind.LONG_TEXT:
        return forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 3}), **common)
    if kind == Question.Kind.CHOICE:
        choices = [("", "Choose one")] + [(o, o) for o in question.options]
        return forms.ChoiceField(choices=choices, **common)
    if kind == Question.Kind.MULTI_CHOICE:
        return forms.MultipleChoiceField(
            choices=[(o, o) for o in question.options], widget=forms.CheckboxSelectMultiple, **common
        )
    if kind == Question.Kind.YES_NO:
        return forms.TypedChoiceField(
            choices=[("", "Choose one"), ("yes", "Yes"), ("no", "No")], **common
        )
    if kind == Question.Kind.NUMBER:
        return forms.DecimalField(max_digits=12, decimal_places=2, **common)
    if kind == Question.Kind.LINK:
        return forms.URLField(max_length=500, assume_scheme="https", **common)
    return forms.CharField(max_length=300, **common)


class RegistrationForm(forms.Form):
    """Built from the event's visible questions. Answers are saved by permanent ID."""

    def __init__(self, *args, questions, **kwargs):
        super().__init__(*args, **kwargs)
        self.questions = list(questions)
        for question in self.questions:
            field = _field_for(question)
            if not isinstance(field.widget, (forms.CheckboxSelectMultiple,)):
                css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
                field.widget.attrs.setdefault("class", css)
            self.fields[f"q_{question.key}"] = field

    def answers(self) -> dict:
        result = {}
        for question in self.questions:
            value = self.cleaned_data.get(f"q_{question.key}")
            if value in (None, "", []):
                continue
            result[question.key] = str(value) if question.kind == Question.Kind.NUMBER else value
        return result
