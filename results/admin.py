from django.contrib import admin, messages
from django.db.models import Q
from django.utils import timezone

from events.models import Event

from .models import Result


@admin.register(Result)
class ResultAdmin(admin.ModelAdmin):
    list_display = ["event", "rank", "position", "person", "status", "drafted_by", "checked_by", "published_at"]
    list_filter = ["event", "status"]
    search_fields = ["position", "registration__person__full_name", "registration__person__student_id"]
    list_select_related = ["event", "registration__person", "drafted_by", "checked_by"]
    autocomplete_fields = ["registration"]
    fields = ["event", "registration", "position", "rank", "note", "status", "drafted_by", "checked_by", "checked_at", "published_at"]
    readonly_fields = ["status", "drafted_by", "checked_by", "checked_at", "published_at"]
    actions = ["mark_checked", "publish", "back_to_draft"]

    @admin.display(description="Person")
    def person(self, obj):
        return obj.registration.person

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_portal_admin else qs.filter(event__coordinators=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "event" and not request.user.is_portal_admin:
            kwargs["queryset"] = Event.objects.filter(coordinators=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status == Result.Status.DRAFT

    def save_model(self, request, obj, form, change):
        if not change:
            obj.drafted_by = request.user
        elif form.changed_data and obj.status != Result.Status.DRAFT:
            # A changed result must be checked again before anyone sees it.
            obj.status = Result.Status.DRAFT
            obj.checked_by = None
            obj.checked_at = None
            obj.published_at = None
            messages.warning(request, "The result changed, so it is back to draft and needs a second check.")
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark as checked by me")
    def mark_checked(self, request, queryset):
        drafts = queryset.filter(status=Result.Status.DRAFT)
        mine = drafts.filter(drafted_by=request.user).count()
        n = drafts.filter(~Q(drafted_by=request.user)).update(
            status=Result.Status.CHECKED, checked_by=request.user, checked_at=timezone.now()
        )
        self.message_user(request, f"{n} result(s) checked.")
        if mine:
            self.message_user(request, f"{mine} result(s) were drafted by you, so another member must check them.", messages.WARNING)

    @admin.action(description="Publish checked results")
    def publish(self, request, queryset):
        n = queryset.filter(status=Result.Status.CHECKED).update(status=Result.Status.PUBLISHED, published_at=timezone.now())
        waiting = queryset.filter(status=Result.Status.DRAFT).count()
        self.message_user(request, f"{n} result(s) published.")
        if waiting:
            self.message_user(request, f"{waiting} draft result(s) need a second check first.", messages.WARNING)

    @admin.action(description="Move back to draft (unpublish)")
    def back_to_draft(self, request, queryset):
        n = queryset.exclude(status=Result.Status.DRAFT).update(
            status=Result.Status.DRAFT, checked_by=None, checked_at=None, published_at=None
        )
        self.message_user(request, f"{n} result(s) moved back to draft.")
