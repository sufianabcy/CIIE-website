"""Student and faculty detail fields for a hackathon: the built-in ones (stored on the person) and custom ones.

One definition drives the edit forms, the import's column matching and row checks, and the exports.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from people.models import normalise_email

from .models import FieldDefinition, PersonDetails

K = FieldDefinition.Kind
# key, label, type, person attribute
BUILTINS = {
    "student": [
        ("name", "Name", K.TEXT, "full_name"),
        ("email", "Email", K.EMAIL, "email"),
        ("phone", "Phone", K.PHONE, "phone"),
        ("student_id", "KL ID", K.TEXT, "student_id"),
        ("branch", "Branch", K.TEXT, "branch"),
        ("year", "Year", K.TEXT, "year_of_study"),
    ],
    "faculty": [
        ("name", "Name", K.TEXT, "full_name"),
        ("email", "Email", K.EMAIL, "email"),
        ("employee_id", "Employee ID", K.TEXT, "employee_id"),
        ("department", "Department", K.TEXT, "branch"),
        ("phone", "Phone", K.PHONE, "phone"),
    ],
}
ATTR = {applies: {key: attr for key, _, _, attr in rows} for applies, rows in BUILTINS.items()}


def ensure_fields(event, applies_to):
    """The fields for students or faculty at this hackathon, creating the built-in ones the first time."""
    existing = set(FieldDefinition.objects.filter(event=event, applies_to=applies_to, builtin__gt="").values_list("builtin", flat=True))
    for i, (key, label, kind, _) in enumerate(BUILTINS[applies_to]):
        if key not in existing:
            FieldDefinition.objects.get_or_create(
                event=event, applies_to=applies_to, key=key,
                defaults=dict(label=label, kind=kind, builtin=key, position=i, required=key in ("name", "email")),
            )
    return list(FieldDefinition.objects.filter(event=event, applies_to=applies_to))


def custom_values(event, person) -> dict:
    if person is None or person.pk is None:
        return {}  # someone being added: nothing saved yet
    row = PersonDetails.objects.filter(event=event, person=person).first()
    return row.values if row else {}


def value_of(defn, person, values):
    if defn.builtin:
        return getattr(person, ATTR[defn.applies_to][defn.builtin], "") or ""
    return values.get(defn.key, "")


def display(defn, value) -> str:
    if value in (None, ""):
        return ""
    if defn.kind == K.YES_NO:
        return "Yes" if value in (True, "yes") else "No"
    return str(value)


# --- Checking one value (forms and imports share this) ---------------------------------


def clean_value(defn, raw):
    """(value, error). raw is a string, number, bool or date from a form or an Excel cell."""
    if isinstance(raw, str):
        raw = raw.strip()
    if raw in (None, ""):
        if defn.required or defn.always_required:
            return None, "required, but empty"
        return "", ""
    text = str(raw).strip()
    if isinstance(raw, float) and raw.is_integer():
        text = str(int(raw))
    if defn.kind == K.EMAIL:
        try:
            validate_email(text)
        except ValidationError:
            return None, f"\"{text}\" is not a valid email"
        return normalise_email(text), ""
    if defn.kind == K.PHONE:
        digits = re.sub(r"\D", "", text)
        if not 10 <= len(digits) <= 15:
            return None, f"\"{text}\" is not a phone number (10 to 15 digits)"
        return text, ""
    if defn.kind == K.NUMBER:
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None, f"\"{text}\" is not a number"
        return float(number) if number != number.to_integral_value() else int(number), ""
    if defn.kind == K.DATE:
        if isinstance(raw, (datetime.date, datetime.datetime)):
            return (raw.date() if isinstance(raw, datetime.datetime) else raw).isoformat(), ""
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
            try:
                return datetime.datetime.strptime(text, fmt).date().isoformat(), ""
            except ValueError:
                pass
        return None, f"\"{text}\" is not a date (use 2026-12-04 or 04/12/2026)"
    if defn.kind == K.YES_NO:
        word = text.lower()
        if word in ("yes", "y", "true", "1"):
            return "yes", ""
        if word in ("no", "n", "false", "0"):
            return "no", ""
        return None, f"\"{text}\" is not yes or no"
    if defn.kind == K.CHOICE:
        match = next((c for c in defn.choices if c.lower() == text.lower()), None)
        if match is None:
            return None, f"\"{text}\" is not one of: {', '.join(defn.choices)}"
        return match, ""
    return text, ""


# --- Forms --------------------------------------------------------------------------------


def form_field(defn):
    common = {"label": defn.label, "required": defn.required or defn.always_required}
    if defn.kind == K.LONG_TEXT:
        return forms.CharField(widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}), **common)
    if defn.kind == K.NUMBER:
        return forms.DecimalField(widget=forms.NumberInput(attrs={"class": "form-control"}), **common)
    if defn.kind == K.EMAIL:
        return forms.EmailField(widget=forms.EmailInput(attrs={"class": "form-control"}), **common)
    if defn.kind == K.DATE:
        return forms.DateField(widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}), **common)
    if defn.kind == K.YES_NO:
        return forms.ChoiceField(choices=[("", "—"), ("yes", "Yes"), ("no", "No")], widget=forms.Select(attrs={"class": "form-select"}), **common)
    if defn.kind == K.CHOICE:
        return forms.ChoiceField(choices=[("", "—")] + [(c, c) for c in defn.choices], widget=forms.Select(attrs={"class": "form-select"}), **common)
    return forms.CharField(max_length=300, widget=forms.TextInput(attrs={"class": "form-control"}), **common)


class DetailsForm(forms.Form):
    """Edit one person's details for one hackathon, as the field definitions say."""

    def __init__(self, *args, event, applies_to, person=None, email_editable=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.event, self.applies_to, self.person = event, applies_to, person
        self.definitions = ensure_fields(event, applies_to)
        values = custom_values(event, person) if person else {}
        for d in self.definitions:
            field = form_field(d)
            if d.builtin == "email" and person is not None and not email_editable:
                field.disabled = True  # changing an account's email changes who can sign in to it: admins only
                field.help_text = "Only admins can change an email."
            if person is not None:
                field.initial = value_of(d, person, values)
            if applies_to == "student" and d.builtin == "email" and person is None:
                field.required = False
                field.help_text = "Optional with a KL ID: they then sign in with ID@kluniversity.in."
            self.fields[f"f_{d.key}"] = field

    def clean(self):
        cleaned = super().clean()
        if self.applies_to == "student" and self.person is None and not cleaned.get("f_email"):
            # A student added by KL ID signs in with their KL email.
            from .services import kl_email_for

            if cleaned.get("f_student_id"):
                cleaned["f_email"] = kl_email_for(cleaned["f_student_id"])
            else:
                self.add_error("f_email", "Give their email or their KL ID.")
        for d in self.definitions:
            name = f"f_{d.key}"
            if name in self.errors or self.fields[name].disabled:
                continue
            value, error = clean_value(d, cleaned.get(name))
            if error:
                self.add_error(name, error[0].upper() + error[1:] + ".")
                continue
            cleaned[name] = value
            other = self._taken_by(d.builtin, value)
            if other:
                self.add_error(name, f"Someone already uses this {d.label}: {other.display_name} ({other.get_kind_display()}).")
        return cleaned

    def _taken_by(self, builtin, value):
        """Another person who already has this email, KL ID or employee ID (these are unique)."""
        from django.db.models import Q

        from people.models import Person

        if not value or builtin not in ("email", "student_id", "employee_id"):
            return None
        if builtin == "email":
            q = Q(email__iexact=value) | Q(kl_email__iexact=value)
        else:
            q = Q(**{f"{builtin}__iexact": value})
        others = Person.objects.filter(q)
        if self.person is not None and self.person.pk:
            others = others.exclude(pk=self.person.pk)
        elif builtin in ("email", "student_id"):
            return None  # adding someone by an email or KL ID already on the portal reuses that person
        return others.first()

    def save_to(self, person):
        values = custom_values(self.event, person)
        for d in self.definitions:
            name = f"f_{d.key}"
            if self.fields[name].disabled:
                continue
            value = self.cleaned_data.get(name, "")
            if d.builtin:
                attr = ATTR[d.applies_to][d.builtin]
                setattr(person, attr, (value or None) if attr == "email" else (value or ""))
            else:
                values[d.key] = value
        person.full_clean(exclude=["password", "kl_email"])
        person.save()
        PersonDetails.objects.update_or_create(event=self.event, person=person, defaults={"values": values})
        return person
