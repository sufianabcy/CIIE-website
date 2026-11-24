from django.contrib import admin, messages

from .models import Registration
from .services import RegistrationError, cancel


@admin.register(Registration)
class RegistrationAdmin(admin.ModelAdmin):
    list_display = ["person", "event", "status", "source", "pass_code_display", "created_at", "checked_in"]
    list_filter = ["event", "status", "source"]
    search_fields = ["person__full_name", "person__email", "person__student_id", "person__phone", "pass_code"]
    list_select_related = ["person", "event"]
    readonly_fields = [
        "event", "person", "status", "pass_code", "source", "form_version", "created_at", "created_by", "cancelled_at", "cancelled_by",
    ]
    fields = readonly_fields[:4] + ["answers"] + readonly_fields[4:]
    actions = ["cancel_selected"]

    def get_queryset(self, request):
        qs = super().get_queryset(request).prefetch_related("checkins")
        return qs if request.user.is_portal_admin else qs.filter(event__coordinators=request.user)

    def has_add_permission(self, request):
        return False  # registrations come from the event page or the walk-in desk

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description="Checked in")
    def checked_in(self, obj):
        return any(c.undone_at is None for c in obj.checkins.all())

    @admin.display(description="Pass code")
    def pass_code_display(self, obj):
        return obj.pass_code_display

    @admin.action(description="Cancel selected registrations (frees their seats)")
    def cancel_selected(self, request, queryset):
        done = 0
        for registration in queryset:
            try:
                cancel(registration, by=request.user)
                done += 1
            except RegistrationError as exc:
                self.message_user(request, f"{registration}: {exc}", messages.WARNING)
        self.message_user(request, f"{done} registration(s) cancelled.")
