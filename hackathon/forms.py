from django import forms

from .models import EventFaculty, Room, Round


class UploadForm(forms.Form):
    file = forms.FileField(
        label="Excel file (.xlsx)",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".xlsx"}),
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Upload an .xlsx file (Excel). Save older .xls or .csv files as .xlsx first.")
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError("The file is over 5 MB.")
        return f


class CredentialsForm(forms.Form):
    which = forms.ChoiceField(
        choices=[("new", "Only people who have no password yet"), ("all", "Everyone (old passwords stop working)")],
        widget=forms.RadioSelect,
        initial="new",
    )
    expires_at = forms.DateTimeField(
        label="Temporary passwords stop working at",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )


class AllocateForm(forms.Form):
    round = forms.ModelChoiceField(queryset=Round.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    faculty = forms.ModelChoiceField(queryset=EventFaculty.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    room = forms.ModelChoiceField(queryset=Room.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["round"].queryset = Round.objects.filter(event=event)
        self.fields["faculty"].queryset = EventFaculty.objects.filter(event=event, is_active=True).select_related("person")
        self.fields["room"].queryset = Room.objects.filter(event=event)


class MoveTeamForm(forms.Form):
    room = forms.ModelChoiceField(queryset=Room.objects.none(), required=False, empty_label="No room")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].queryset = Room.objects.filter(event=event)


class MarksForm(forms.Form):
    def __init__(self, *args, criteria, scores, assignment, **kwargs):
        super().__init__(*args, **kwargs)
        self.criteria = list(criteria)
        for c in self.criteria:
            self.fields[f"c_{c.pk}"] = forms.DecimalField(
                label=c.label, help_text=c.description, required=False, min_value=0, max_value=c.max_marks,
                max_digits=6, decimal_places=2, initial=scores.get(c.pk),
                widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.5", "inputmode": "decimal", "style": "max-width:8rem"}),
            )
        self.fields["remarks"] = forms.CharField(
            required=False, max_length=2000, initial=assignment.remarks,
            widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        )

    def rows(self):
        return [(c, self[f"c_{c.pk}"]) for c in self.criteria]

    def marks(self):
        return {c: self.cleaned_data.get(f"c_{c.pk}") for c in self.criteria}

    def require_all(self):
        missing = [c for c in self.criteria if self.cleaned_data.get(f"c_{c.pk}") is None]
        for c in missing:
            self.add_error(f"c_{c.pk}", "Give marks before submitting.")
        return not missing


class FacultyRequestForm(forms.Form):
    full_name = forms.CharField(label="Your name", max_length=150)
    employee_id = forms.CharField(label="Employee ID", max_length=50)
    department = forms.CharField(max_length=150)
    designation = forms.CharField(max_length=100, help_text="For example Assistant Professor.")
    phone = forms.CharField(max_length=20)
    event = forms.ModelChoiceField(
        label="Hackathon you'd like to judge (optional)", queryset=None, required=False, empty_label="Not yet",
    )

    def __init__(self, *args, **kwargs):
        from django.utils import timezone

        from events.models import Event

        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = Event.objects.filter(
            kind=Event.Kind.HACKATHON, status=Event.Status.PUBLISHED, ends_at__gte=timezone.now()
        ).order_by("starts_at")
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-select" if isinstance(field, forms.ModelChoiceField) else "form-control")

    def clean_phone(self):
        digits = "".join(ch for ch in self.cleaned_data["phone"] if ch.isdigit())
        if not 10 <= len(digits) <= 15:
            raise forms.ValidationError("Enter a phone number with 10 to 15 digits.")
        return self.cleaned_data["phone"].strip()


class DecisionForm(forms.Form):
    note = forms.CharField(required=False, max_length=500, widget=forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Note to them (optional)"}))



class ChangeMarksForm(MarksForm):
    """MarksForm plus a required reason (for admins and permitted supervisors)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"] = forms.CharField(
            label="Reason (recorded, and shown in the marks Excel)", max_length=500,
            widget=forms.TextInput(attrs={"class": "form-control"}),
        )
