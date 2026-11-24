from django import forms

from .models import Person


class SignInForm(forms.Form):
    email = forms.EmailField(
        label="Your email",
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "autofocus": True, "placeholder": "you@example.com", "class": "form-control form-control-lg"}
        ),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["full_name", "phone", "institution", "branch", "year_of_study"]
        labels = {
            "full_name": "Your name, as it should appear on certificates",
            "phone": "Phone number",
            "institution": "College or institution",
            "branch": "Branch or department",
            "year_of_study": "Year of study",
        }
        help_texts = {"phone": "Only the event team sees it."}
        widgets = {
            "full_name": forms.TextInput(attrs={"autocomplete": "name"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel"}),
            "institution": forms.TextInput(attrs={"autocomplete": "organization"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("full_name", "phone", "institution"):
            self.fields[name].required = True
        if self.instance.is_kl_verified:
            if not self.instance.institution:
                self.initial["institution"] = "KL University"
            self.fields["branch"].required = True
            self.fields["year_of_study"].required = True
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_full_name(self):
        name = " ".join(self.cleaned_data["full_name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Please enter your full name.")
        return name

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not 10 <= len(digits) <= 15:
            raise forms.ValidationError("Enter a phone number with 10 to 15 digits.")
        return phone


class StaffSignInForm(forms.Form):
    login = forms.CharField(
        label="Email or login ID", max_length=254,
        widget=forms.TextInput(attrs={"class": "form-control form-control-lg", "autocomplete": "username", "autofocus": True}),
    )
    password = forms.CharField(
        label="Password", max_length=128, strip=False,
        widget=forms.PasswordInput(attrs={"class": "form-control form-control-lg", "autocomplete": "current-password"}),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)
