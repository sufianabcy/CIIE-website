from django import forms


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
        choices=[("new", "Only faculty who have no code yet"), ("all", "Everyone (old codes stop working)")],
        widget=forms.RadioSelect,
        initial="new",
    )
    expires_at = forms.DateTimeField(
        label="Codes stop working at",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )


class AutoAssignForm(forms.Form):
    per_student = forms.IntegerField(
        label="Faculty per student", min_value=1, max_value=10, initial=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "style": "max-width:6rem"}),
    )


class JudgeSignInForm(forms.Form):
    login_id = forms.CharField(
        label="Login ID", max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control form-control-lg", "autocomplete": "username", "autocapitalize": "characters"}),
    )
    code = forms.CharField(
        label="Access code", max_length=20,
        widget=forms.PasswordInput(attrs={"class": "form-control form-control-lg", "autocomplete": "current-password"}),
    )


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

    def require_all(self):
        missing = [c for c in self.criteria if self.cleaned_data.get(f"c_{c.pk}") is None]
        for c in missing:
            self.add_error(f"c_{c.pk}", "Give marks before submitting.")
        return not missing
