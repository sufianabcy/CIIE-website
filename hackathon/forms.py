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


class TeamRegistrationForm(forms.Form):
    """A hackathon team, registered by its leader (the person signed in) with the teammates' details."""

    team_name = forms.CharField(label="Team name", max_length=150, required=False)

    def __init__(self, *args, event, leader, **kwargs):
        super().__init__(*args, **kwargs)
        self.event, self.leader = event, leader
        self.slots = list(range(2, (event.team_size_max or 1) + 1))
        for n in self.slots:
            self.fields[f"m{n}_name"] = forms.CharField(label=f"Member {n} name", max_length=150, required=False,
                                                        widget=forms.TextInput(attrs={"placeholder": "Name", "aria-label": f"Member {n} name"}))
            self.fields[f"m{n}_email"] = forms.EmailField(label=f"Member {n} email", required=False,
                                                         widget=forms.EmailInput(attrs={"placeholder": "Email", "aria-label": f"Member {n} email"}))
            self.fields[f"m{n}_student_id"] = forms.CharField(label=f"Member {n} KL ID", max_length=20, required=False,
                                                              widget=forms.TextInput(attrs={"placeholder": "KL ID", "aria-label": f"Member {n} KL ID"}))
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def member_rows(self):
        return [(n, self[f"m{n}_name"], self[f"m{n}_email"], self[f"m{n}_student_id"]) for n in self.slots]

    def clean(self):
        from people.models import normalise_email

        from .models import Team, TeamMember
        from .services import _find_person

        cleaned = super().clean()
        name = " ".join((cleaned.get("team_name") or "").split())
        if name and Team.objects.filter(event=self.event, name__iexact=name).exists():
            self.add_error("team_name", "Another team already has this name.")
        cleaned["team_name"] = name
        members, seen = [], {normalise_email(self.leader.email), (self.leader.student_id or "").lower()} - {None, ""}
        for n in self.slots:
            m = {"name": (cleaned.get(f"m{n}_name") or "").strip(), "email": normalise_email(cleaned.get(f"m{n}_email")) or "",
                 "student_id": (cleaned.get(f"m{n}_student_id") or "").strip()}
            if not any(m.values()):
                continue
            if not (m["email"] or m["student_id"]):
                self.add_error(f"m{n}_email", "Give their email or KL ID.")
                continue
            for key in ("email", "student_id"):
                value = m[key].lower()
                if value and value in seen:
                    self.add_error(f"m{n}_{key}", "This person is already in the team (or is you).")
                if value:
                    seen.add(value)
            person, clash = _find_person(m["email"], m["student_id"])
            if clash:
                self.add_error(f"m{n}_email", f"These details are {clash}.")
            elif person and TeamMember.objects.filter(team__event=self.event, registration__person=person).exists():
                self.add_error(f"m{n}_email", f"{person.display_name} is already in a team for this hackathon.")
            elif not person and not m["name"]:
                self.add_error(f"m{n}_name", "Give their name: they don't have an account yet.")
            members.append(m)
        size = len(members) + 1
        low, high = self.event.team_size_min or 1, self.event.team_size_max or 1
        if not name and (members or low > 1):
            self.add_error("team_name", "Give your team a name.")
        cleaned["solo"] = not name and not members and low <= 1
        if not low <= size <= high:
            raise forms.ValidationError(f"Teams here have {low} to {high} members, including you. This team has {size}.")
        cleaned["members"] = members
        return cleaned
