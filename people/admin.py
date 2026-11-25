from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group

from .models import Person

# Roles that open the admin. Faculty is a role too, but faculty only use the judging pages.
ROLE_GROUPS = {"Coordinator", "Admin"}  # supervisors work on the hackathon pages, not in the admin


ROLES = [
    ("student", "Student"),
    ("faculty", "Faculty (judges at hackathons)"),
    ("coordinator", "Coordinator (runs the events they're added to)"),
    ("admin", "Admin (everything)"),
]
ROLE_GROUP = {"faculty": "Faculty", "coordinator": "Coordinator", "admin": "Admin"}


def role_of(person) -> str:
    names = set(person.groups.values_list("name", flat=True)) if person.pk else set()
    if person.is_superuser or "Admin" in names:
        return "admin"
    if "Coordinator" in names:
        return "coordinator"
    return "faculty" if "Faculty" in names else "student"


class PersonForm(forms.ModelForm):
    role = forms.ChoiceField(
        choices=ROLES, widget=forms.RadioSelect,
        help_text="Supervisors are chosen per hackathon on its page. Coordinators also need adding to each event they run.",
    )

    class Meta:
        model = Person
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].initial = role_of(self.instance)


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    form = PersonForm
    list_display = ["display_name", "email", "student_id", "kl_verified", "roles", "is_active", "date_joined"]
    list_filter = ["is_active", "groups", "source"]
    search_fields = ["full_name", "email", "kl_email", "student_id", "phone", "employee_id", "login_id"]
    ordering = ["-date_joined"]
    readonly_fields = ["date_joined", "last_login", "profile_completed_at", "source", "login_id", "failed_password_signins"]
    filter_horizontal = ["groups"]
    actions = ["deactivate", "reactivate", "unlock_password"]
    fieldsets = [
        (None, {"fields": ["email", "full_name", "phone", "institution", "branch", "year_of_study"]}),
        ("KL status", {"fields": ["kl_email", "student_id", "kl_verified_at"]}),
        (
            "Staff sign-in (faculty and supervisors)",
            {
                "fields": ["employee_id", "login_id", "must_change_password", "temp_password_expires_at", "failed_password_signins"],
                "description": "Temporary passwords are made on each hackathon's page and shown once, in an Excel file.",
            },
        ),
        ("Role", {"fields": ["role", "is_active"], "description": "Keep two admins at all times."}),
        ("Dates", {"classes": ["collapse"], "fields": ["date_joined", "last_login", "profile_completed_at", "source"]}),
    ]
    ADVANCED = ("Advanced (superusers)", {"classes": ["collapse"], "fields": ["groups", "is_superuser"],
                                          "description": "The raw role groups. Usually the Role choice above is enough."})

    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        return fieldsets + [self.ADVANCED] if request.user.is_superuser else fieldsets

    @admin.display(boolean=True, description="KL verified")
    def kl_verified(self, obj):
        return obj.is_kl_verified

    @admin.display(description="Roles")
    def roles(self, obj):
        return ", ".join(g.name for g in obj.groups.all()) or "Student"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    def has_delete_permission(self, request, obj=None):
        return False  # people are deactivated, never deleted

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            fields.append("is_superuser")
        return fields

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        person = form.instance
        role = form.cleaned_data.get("role")
        if role and not (request.user.is_superuser and "groups" in form.changed_data):
            groups = set(person.groups.values_list("name", flat=True))
            keep_faculty = "Faculty" in groups and person.faculty_roles.exists()  # still judges somewhere
            person.groups.remove(*Group.objects.filter(name__in=["Faculty", "Coordinator", "Admin"]))
            if role in ROLE_GROUP:
                person.groups.add(Group.objects.get_or_create(name=ROLE_GROUP[role])[0])
            if keep_faculty and role != "faculty":
                person.groups.add(Group.objects.get_or_create(name="Faculty")[0])
        staff = person.is_superuser or person.groups.filter(name__in=ROLE_GROUPS).exists()
        if person.is_staff != staff:
            person.is_staff = staff
            person.save(update_fields=["is_staff"])

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
