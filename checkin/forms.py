from django import forms

from .models import CheckIn


class WalkInForm(forms.Form):
    kind = forms.ChoiceField(
        label="Who is this?",
        choices=[
            (CheckIn.Method.WALK_IN, "Never registered (walk-in)"),
            (CheckIn.Method.MANUAL_ADD, "Says they registered, but their pass won't scan or can't be found"),
        ],
        widget=forms.RadioSelect,
        initial=CheckIn.Method.WALK_IN,
    )
    name = forms.CharField(label="Full name", max_length=150)
    kl_id_or_email = forms.CharField(
        label="KL ID or email",
        max_length=200,
        help_text="A KL ID links them to their KL account when they sign in later.",
    )
    phone = forms.CharField(label="Phone", max_length=20, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("name", "kl_id_or_email", "phone"):
            self.fields[name].widget.attrs["class"] = "form-control form-control-lg"

    def split_identity(self) -> tuple[str, str]:
        value = self.cleaned_data["kl_id_or_email"].strip()
        if "@" in value:
            forms.EmailField().clean(value)
            return value, ""
        return "", value


class UndoForm(forms.Form):
    reason = forms.CharField(max_length=200, initial="Scanned by mistake")
