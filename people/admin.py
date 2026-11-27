from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .models import Person, is_kl_email, normalise_email

# Roles that open the admin. Faculty is a type of person (and a role), but faculty only use the judging pages.
ROLE_GROUPS = {"Coordinator", "Admin"}  # supervisors and volunteers work on the hackathon pages, not in the admin

# What each type of person has. Department is stored in `branch`.
STUDENT_FIELDS = ["kind", "email", "full_name", "phone", "institution", "branch", "year_of_study", "kl_email", "student_id"]
FACULTY_FIELDS = ["kind", "email", "full_name", "designation", "phone", "branch", "employee_id", "kl_email"]


def _group(name):
    return Group.objects.get_or_create(name=name)[0]


class PersonForm(forms.ModelForm):
    """Type first (student or faculty), their details, then Coordinator and Admin as roles on top."""

    is_coordinator = forms.BooleanField(
        label="Coordinator", required=False,
        help_text="Runs the events they're added to (on each event's page). A student or a faculty member can be one.",
    )
    is_admin = forms.BooleanField(label="Admin", required=False, help_text="Can see and change everything. Only superusers grant this.")

    class Meta:
        model = Person
        fields = "__all__"
        labels = {"full_name": "Name"}

    def __init__(self, *args, kind=None, can_grant_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind or self.instance.kind
        names = set(self.instance.groups.values_list("name", flat=True)) if self.instance.pk else set()
        self.fields["is_coordinator"].initial = "Coordinator" in names
        self.fields["is_admin"].initial = "Admin" in names
        if not can_grant_admin:
            self.fields["is_admin"].disabled = True
        if "kind" in self.fields:
            self.fields["kind"].widget = forms.RadioSelect(choices=Person.Kind.choices)
            if self.instance.pk:
                self.fields["kind"].help_text = "Changing the type changes which details are kept here. Details of the old type stay hidden, not deleted."
        if "branch" in self.fields and self.kind == Person.Kind.FACULTY:
            self.fields["branch"].label = "Department"
        if "employee_id" in self.fields:
            self.fields["employee_id"].help_text = "Unique. Used to match faculty in Excel uploads."

    def _taken(self, what, other):
        url = reverse("admin:people_person_change", args=[other.pk])
        return forms.ValidationError(format_html(
            'Someone already uses this {}: {} ({}). <a href="{}">Open their page</a> to change their details or give them a role.',
            what, other.display_name, other.get_kind_display(), url,
        ))

    def _unique_email(self, name):
        value = normalise_email(self.cleaned_data.get(name))
        if not value:
            return None
        other = Person.objects.filter(Q(email__iexact=value) | Q(kl_email__iexact=value)).exclude(pk=self.instance.pk).first()
        if other:
            raise self._taken("email", other)
        return value

    def clean_student_id(self):
        value = (self.cleaned_data.get("student_id") or "").strip()
        other = Person.objects.filter(student_id__iexact=value).exclude(pk=self.instance.pk).first() if value else None
        if other:
            raise self._taken("KL ID", other)
        return value

    def clean_email(self):
        return self._unique_email("email")

    def clean_kl_email(self):
        return self._unique_email("kl_email")

    def clean_employee_id(self):
        value = " ".join((self.cleaned_data.get("employee_id") or "").split())
        other = Person.objects.filter(employee_id__iexact=value).exclude(pk=self.instance.pk).first() if value else None
        if other:
            raise self._taken("employee ID", other)
        return value

    def clean(self):
        cleaned = super().clean()
        if self.kind == Person.Kind.FACULTY and "employee_id" in self.fields and not cleaned.get("employee_id"):
            self.add_error("employee_id", "Faculty need an employee ID.")
        return cleaned


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    form = PersonForm
    list_display = ["display_name", "kind", "email", "id_number", "role_badges", "is_active", "date_joined"]
    list_filter = ["kind", "is_active", "groups", "source"]
    search_fields = ["full_name", "email", "kl_email", "student_id", "phone", "employee_id", "login_id"]
    ordering = ["-date_joined"]
    readonly_fields = ["date_joined", "last_login", "profile_completed_at", "source", "login_id", "failed_password_signins"]
    filter_horizontal = ["groups"]
    actions = ["deactivate", "reactivate", "unlock_password"]

    # --- Which fields, for which type ---------------------------------------------------------

    def _kind(self, request, obj=None):
        chosen = request.POST.get("kind") or request.GET.get("type")
        if chosen in Person.Kind.values:
            return chosen
        return obj.kind if obj else None

    def get_fieldsets(self, request, obj=None):
        kind = self._kind(request, obj) or Person.Kind.STUDENT
        details = FACULTY_FIELDS if kind == Person.Kind.FACULTY else STUDENT_FIELDS
        fieldsets = [
            ("Faculty member" if kind == Person.Kind.FACULTY else "Student", {"fields": details}),
            ("Roles", {"fields": ["is_coordinator", "is_admin", "is_active"],
                       "description": "Supervisors and volunteers are chosen per hackathon on its page. Keep two admins at all times."}),
        ]
        if obj:
            fieldsets += [
                ("Staff sign-in", {"classes": ["collapse"],
                                   "fields": ["login_id", "must_change_password", "temp_password_expires_at", "failed_password_signins"],
                                   "description": "Temporary passwords are made on each hackathon's page and shown once, in an Excel file."}),
                ("Dates", {"classes": ["collapse"], "fields": ["date_joined", "last_login", "profile_completed_at", "source"]}),
            ]
        if request.user.is_superuser:
            fieldsets.append(("Advanced (superusers)", {"classes": ["collapse"], "fields": ["kl_verified_at", "groups", "is_superuser"],
                                                        "description": "Set automatically when a KL email is saved, and the raw role groups."}))
        return fieldsets

    def get_form(self, request, obj=None, change=False, **kwargs):
        form_class = super().get_form(request, obj, change=change, **kwargs)
        kind = self._kind(request, obj) or Person.Kind.STUDENT
        can_grant_admin = request.user.is_superuser

        class Form(form_class):
            def __init__(self, *args, **kw):
                kw.setdefault("kind", kind)
                kw.setdefault("can_grant_admin", can_grant_admin)
                super().__init__(*args, **kw)

        return Form

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        if request.GET.get("type") in Person.Kind.values:
            initial["kind"] = request.GET["type"]
        return initial

    def add_view(self, request, form_url="", extra_context=None):
        """Ask "Student or faculty?" first; the form that follows has only that type's details."""
        if request.method == "GET" and request.GET.get("type") not in Person.Kind.values:
            return render(request, "admin/people/person/choose_type.html", {
                **self.admin_site.each_context(request), "title": "Add a person", "opts": self.model._meta,
            })
        return self._safely(request, lambda: super(PersonAdmin, self).add_view(request, form_url, extra_context))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return self._safely(request, lambda: super(PersonAdmin, self).change_view(request, object_id, form_url, extra_context))

    def _safely(self, request, view):
        """Safety net: if two saves race (say, a double-clicked Save) the database refuses the second; say so, never a 500."""
        try:
            return view()
        except IntegrityError:
            self.message_user(
                request, "Not saved: someone else already uses that email, KL ID or employee ID (it may have just been saved twice). "
                "Search the people list for them.", level="error",
            )
            return redirect(request.get_full_path())

    # --- Saving -----------------------------------------------------------------------------------

    def save_model(self, request, obj, form, change):
        if obj.kl_email and is_kl_email(obj.kl_email) and not obj.kl_verified_at:
            obj.kl_verified_at = timezone.now()  # an admin entering it counts as verified
        if not obj.pk:
            obj.set_unusable_password()  # students sign in by emailed code; staff get temporary passwords
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        person = form.instance
        if not (request.user.is_superuser and "groups" in form.changed_data):
            if person.kind == Person.Kind.FACULTY:
                person.groups.add(_group("Faculty"))  # can be added to hackathons as faculty straight away
            elif not person.faculty_roles.exists():
                person.groups.remove(*Group.objects.filter(name="Faculty"))
            for name, field in (("Coordinator", "is_coordinator"), ("Admin", "is_admin")):
                if form.fields[field].disabled:
                    continue
                if form.cleaned_data.get(field):
                    person.groups.add(_group(name))
                else:
                    person.groups.remove(*Group.objects.filter(name=name))
        staff = person.is_superuser or person.groups.filter(name__in=ROLE_GROUPS).exists()
        if person.is_staff != staff:
            person.is_staff = staff
            person.save(update_fields=["is_staff"])

    # --- List -------------------------------------------------------------------------------------

    @admin.display(description="KL ID / employee ID")
    def id_number(self, obj):
        return obj.employee_id if obj.kind == Person.Kind.FACULTY else obj.student_id

    @admin.display(description="Roles")
    def role_badges(self, obj):
        names = {g.name for g in obj.groups.all()}
        badges = [n for n in ("Admin", "Coordinator") if n in names or (n == "Admin" and obj.is_superuser)]
        if obj.supervisor_roles.exists():
            badges.append("Supervisor")
        if obj.volunteer_roles.exists():
            badges.append("Volunteer")
        return format_html_join(" ", '<span class="admin-tag-small">{}</span>', ((b,) for b in badges)) or "—"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups", "supervisor_roles", "volunteer_roles")

    def has_delete_permission(self, request, obj=None):
        return False  # people are deactivated, never deleted

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            fields.append("is_superuser")
        return fields

    @admin.action(description="Deactivate (they can no longer sign in)")
    def deactivate(self, request, queryset):
        n = queryset.exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(request, f"{n} person(s) deactivated.")

    @admin.action(description="Unlock password sign-in after too many wrong tries")
    def unlock_password(self, request, queryset):
        n = queryset.update(failed_password_signins=0)
        self.message_user(request, f"{n} person(s) unlocked.")

    @admin.action(description="Reactivate")
    def reactivate(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"{n} person(s) reactivated.")


admin.site.unregister(Group)
