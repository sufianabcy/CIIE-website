from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from events.models import Event

from .models import Assignment, Criterion, Judge, Score


class CriterionInline(admin.TabularInline):
    """Shown on the event's setup page, under the registration questions."""

    model = Criterion
    extra = 1
    fields = ["position", "label", "description", "max_marks"]
    verbose_name = "rubric criterion"
    verbose_name_plural = "Rubric criteria (what faculty give marks for)"

    def has_delete_permission(self, request, obj=None):
        # Once marks exist, a criterion stays; it would take marks with it.
        return obj is None or not Score.objects.filter(criterion__event=obj).exists()


def _limit_events(request, kwargs):
    if not request.user.is_portal_admin:
        kwargs["queryset"] = Event.objects.filter(coordinators=request.user)


@admin.register(Judge)
class JudgeAdmin(admin.ModelAdmin):
    list_display = ["name", "event", "email", "employee_id", "department", "login_id", "has_code", "is_active"]
    list_filter = ["event", "is_active", "department"]
    search_fields = ["name", "email", "employee_id", "login_id"]
    list_select_related = ["event"]
    readonly_fields = ["login_id", "credentials_issued_at", "last_signin_at", "failed_signins"]
    fields = ["event", "name", "email", "employee_id", "department", "phone", "is_active", "extra",
              "login_id", "credentials_issued_at", "credentials_expire_at", "last_signin_at", "failed_signins"]
    actions = ["revoke", "unlock"]

    @admin.display(boolean=True, description="Has code")
    def has_code(self, obj):
        return obj.has_credentials

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_portal_admin else qs.filter(event__coordinators=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "event":
            _limit_events(request, kwargs)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_delete_permission(self, request, obj=None):
        return obj is None or not obj.assignments.exists()

    @admin.action(description="Stop their access codes working")
    def revoke(self, request, queryset):
        n = queryset.exclude(access_code_hash="").update(access_code_hash="")
        self.message_user(request, f"{n} access code(s) stopped. Make new ones on the event's judging page.")

    @admin.action(description="Unlock after too many wrong tries")
    def unlock(self, request, queryset):
        n = queryset.update(failed_signins=0)
        self.message_user(request, f"{n} faculty unlocked.")


class ScoreInline(admin.TabularInline):
    model = Score
    extra = 0
    fields = ["criterion", "marks", "updated_at"]
    readonly_fields = ["updated_at"]

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj:
            formset.form.base_fields["criterion"].queryset = Criterion.objects.filter(event=obj.judge.event)
        return formset


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ["student", "pass_code", "judge", "event", "total_marks", "submitted_at"]
    list_filter = ["judge__event", ("submitted_at", admin.EmptyFieldListFilter), "judge"]
    search_fields = ["registration__person__full_name", "registration__person__email", "registration__pass_code", "judge__name", "judge__email"]
    list_select_related = ["registration__person", "judge__event"]
    autocomplete_fields = ["judge", "registration"]
    fields = ["judge", "registration", "remarks", "submitted_at"]
    inlines = [ScoreInline]
    actions = ["reopen"]

    def get_queryset(self, request):
        qs = super().get_queryset(request).prefetch_related("scores")
        return qs if request.user.is_portal_admin else qs.filter(judge__event__coordinators=request.user)

    @admin.display(description="Student", ordering="registration__person__full_name")
    def student(self, obj):
        return obj.registration.person

    @admin.display(description="Pass code")
    def pass_code(self, obj):
        return obj.registration.pass_code_display

    @admin.display(description="Event")
    def event(self, obj):
        url = reverse("judging_dashboard", args=[obj.judge.event.slug])
        return format_html('<a href="{}">{}</a>', url, obj.judge.event)

    @admin.display(description="Total")
    def total_marks(self, obj):
        scores = list(obj.scores.all())
        return sum(s.marks for s in scores).normalize() if scores else "-"

    @admin.action(description="Let faculty change these marks again")
    def reopen(self, request, queryset):
        n = queryset.exclude(submitted_at__isnull=True).update(submitted_at=None)
        self.message_user(request, f"{n} mapping(s) reopened. The faculty member must submit again.")
