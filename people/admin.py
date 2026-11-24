from django.contrib import admin
from django.contrib.auth.models import Group

from .models import Person

ROLE_GROUPS = {"Coordinator", "Admin"}


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["display_name", "email", "student_id", "kl_verified", "roles", "source", "is_active", "date_joined"]
    list_filter = ["is_active", "groups", "source", "is_staff"]
    search_fields = ["full_name", "email", "kl_email", "student_id", "phone"]
    ordering = ["-date_joined"]
    readonly_fields = ["date_joined", "last_login", "profile_completed_at", "source"]
    filter_horizontal = ["groups"]
    actions = ["deactivate", "reactivate"]
    fieldsets = [
        (None, {"fields": ["email", "full_name", "phone", "institution", "branch", "year_of_study"]}),
        ("KL status", {"fields": ["kl_email", "student_id", "kl_verified_at"]}),
        (
            "Roles",
            {
                "fields": ["is_active", "groups", "is_superuser"],
                "description": "Give the Coordinator or Admin role here. Keep two admins at all times. "
                "Coordinators also need to be added to each event they run.",
            },
        ),
        ("Dates", {"fields": ["date_joined", "last_login", "profile_completed_at", "source"]}),
    ]

    @admin.display(boolean=True, description="KL verified")
    def kl_verified(self, obj):
        return obj.is_kl_verified

    @admin.display(description="Roles")
    def roles(self, obj):
        return ", ".join(g.name for g in obj.groups.all()) or "Participant"

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
        staff = person.is_superuser or person.groups.filter(name__in=ROLE_GROUPS).exists()
        if person.is_staff != staff:
            person.is_staff = staff
            person.save(update_fields=["is_staff"])

    @admin.action(description="Deactivate (they can no longer sign in)")
    def deactivate(self, request, queryset):
        n = queryset.exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(request, f"{n} person(s) deactivated.")

    @admin.action(description="Reactivate")
    def reactivate(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"{n} person(s) reactivated.")


admin.site.unregister(Group)
