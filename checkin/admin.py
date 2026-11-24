from django.contrib import admin

from .models import CheckIn, ScanLog
from .services import undo


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_portal_admin else qs.filter(event__coordinators=request.user)


@admin.register(CheckIn)
class CheckInAdmin(ReadOnlyAdmin):
    list_display = ["checked_in_at", "person", "event", "method", "gate", "checked_in_by", "undone_at"]
    list_filter = ["event", "method", "gate", ("undone_at", admin.EmptyFieldListFilter)]
    search_fields = ["registration__person__full_name", "registration__person__student_id", "registration__pass_code"]
    list_select_related = ["registration__person", "event", "checked_in_by"]
    actions = ["undo_selected"]

    @admin.display(description="Person")
    def person(self, obj):
        return obj.registration.person

    def has_change_permission(self, request, obj=None):
        # Needed for the undo action; the form itself stays read-only.
        return request.user.is_portal_admin and obj is None

    @admin.action(description="Undo selected check-ins (recorded, not deleted)")
    def undo_selected(self, request, queryset):
        n = 0
        for checkin in queryset.filter(undone_at__isnull=True):
            undo(checkin, by=request.user, reason="Undone in the admin")
            n += 1
        self.message_user(request, f"{n} check-in(s) undone.")


@admin.register(ScanLog)
class ScanLogAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "event", "outcome", "raw_input", "gate", "scanned_by"]
    list_filter = ["event", "outcome", "gate"]
    search_fields = ["raw_input"]
    list_select_related = ["event", "scanned_by"]
