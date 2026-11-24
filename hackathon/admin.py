from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html

from events.models import Event

from .access import PERMISSIONS, allowed, events_allowing
from .models import Assignment, Criterion, EventFaculty, FacultyRequest, MarkChange, Room, Round, Score, Supervisor, Team, TeamMember
from .services import room_change_warnings


def overseen_q(request, path="event"):
    """Rows of hackathons this person runs: all for admins; their events for coordinators and supervisors."""
    user = request.user
    return Q(**{f"{path}__coordinators": user}) | Q(
        **{f"{path}__supervisors__person": user, f"{path}__supervisors__is_active": True}
    )


class ScopedAdmin(admin.ModelAdmin):
    """Lists only the hackathons this person oversees; editing needs `perm` on that hackathon (see access.allowed)."""

    event_path = "event"
    perm = None  # the access.PERMISSIONS switch needed to add, change or delete; None means admins only

    def event_of(self, obj):
        target = obj
        for part in self.event_path.split("__"):
            target = getattr(target, part)
        return target

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_portal_admin:
            return qs
        return qs.filter(overseen_q(request, self.event_path)).distinct()

    def _may_edit(self, request, obj=None):
        user = request.user
        if user.is_portal_admin:
            return True
        if self.perm is None:
            return False
        if obj is None:
            return bool(events_allowing(user, self.perm))
        return allowed(user, self.event_of(obj), self.perm)

    def has_add_permission(self, request):
        return super().has_add_permission(request) and self._may_edit(request)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and self._may_edit(request, obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and self._may_edit(request, obj)

    def save_model(self, request, obj, form, change):
        if not request.user.is_portal_admin and (self.perm is None or not allowed(request.user, self.event_of(obj), self.perm)):
            raise PermissionDenied
        super().save_model(request, obj, form, change)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        model = db_field.related_model
        if not request.user.is_portal_admin:
            if model is Event:
                kwargs["queryset"] = Event.objects.filter(pk__in=[e.pk for e in events_allowing(request.user, self.perm)]) if self.perm else Event.objects.none()
            elif model in (Round, Room, Team, EventFaculty):
                kwargs["queryset"] = model.objects.filter(overseen_q(request)).distinct()
        elif model is Event:
            kwargs["queryset"] = Event.objects.filter(kind=Event.Kind.HACKATHON)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class CriterionInline(admin.TabularInline):
    model = Criterion
    extra = 1
    fields = ["position", "label", "description", "max_marks"]
    verbose_name_plural = "Rubric for this round (what faculty give marks for)"

    def has_delete_permission(self, request, obj=None):
        # Once marks exist, criteria stay; deleting one would take marks with it.
        return super().has_delete_permission(request, obj) and (obj is None or not Score.objects.filter(criterion__round=obj).exists())


@admin.register(Round)
class RoundAdmin(ScopedAdmin):
    perm = "can_edit_rounds"
    list_display = ["name", "event", "position", "opens_at", "closes_at", "state", "results_published_at"]
    list_filter = ["event"]
    fields = ["event", "name", "position", ("opens_at", "closes_at"), "results_published_at"]
    inlines = [CriterionInline]

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        if request.GET.get("event"):
            initial["event"] = request.GET["event"]
        return initial

    def get_readonly_fields(self, request, obj=None):
        if request.user.is_portal_admin or (obj and allowed(request.user, obj.event, "can_publish")):
            return []
        return ["results_published_at"]


@admin.register(Room)
class RoomAdmin(ScopedAdmin):
    perm = "can_edit_teams"
    list_display = ["name", "building", "capacity", "event", "team_count"]
    list_filter = ["event"]
    search_fields = ["name", "building"]

    @admin.display(description="Teams")
    def team_count(self, obj):
        return obj.teams.count()


class TeamMemberInline(admin.TabularInline):
    model = TeamMember
    extra = 0
    autocomplete_fields = ["registration"]
    fields = ["registration", "extra"]

    def _may_edit(self, request, obj):
        return request.user.is_portal_admin or (obj is not None and allowed(request.user, obj.event, "can_edit_teams"))

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and self._may_edit(request, obj)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and self._may_edit(request, obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and self._may_edit(request, obj)


@admin.register(Team)
class TeamAdmin(ScopedAdmin):
    perm = "can_edit_teams"
    list_display = ["name", "project_title", "room", "mentor_display", "event"]
    list_filter = ["event", "room"]
    search_fields = ["name", "project_title", "members__registration__person__full_name", "members__registration__person__email"]
    list_select_related = ["room", "mentor", "event"]
    autocomplete_fields = ["mentor"]
    fields = ["event", "name", "project_title", "project_description", "room", "mentor", "mentor_name", "extra"]
    inlines = [TeamMemberInline]

    def save_model(self, request, obj, form, change):
        if change and "room" in form.changed_data:
            before = Team.objects.get(pk=obj.pk)
            for warning in room_change_warnings(before, obj.room):
                self.message_user(request, warning, level="warning")
        if obj.mentor_id and obj.pk and obj.assignments.filter(faculty__person_id=obj.mentor_id).exists():
            self.message_user(request, f"{obj.mentor} is allocated to judge this team; a mentor shouldn't judge their own team. Remove that allocation.", level="warning")
        super().save_model(request, obj, form, change)


@admin.register(EventFaculty)
class EventFacultyAdmin(ScopedAdmin):
    perm = "can_add_faculty"
    list_display = ["person", "employee_id", "department", "event", "is_active"]
    list_filter = ["event", "is_active"]
    search_fields = ["person__full_name", "person__email", "person__employee_id"]
    autocomplete_fields = ["person"]
    list_select_related = ["person", "event"]

    @admin.display(description="Employee ID")
    def employee_id(self, obj):
        return obj.person.employee_id

    @admin.display(description="Department")
    def department(self, obj):
        return obj.person.branch

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from .services import add_faculty

        add_faculty(obj.event, obj.person)  # gives the Faculty role


@admin.register(Supervisor)
class SupervisorAdmin(ScopedAdmin):
    """Only admins add supervisors or change what they may do (perm = None)."""

    list_display = ["person", "kind", "event", "is_active", "switched_on"]
    list_filter = ["event", "kind", "is_active"]
    search_fields = ["person__full_name", "person__email", "person__employee_id", "person__student_id"]
    autocomplete_fields = ["person"]
    fieldsets = [
        (None, {"fields": ["event", "person", "kind", "is_active"]}),
        (
            "What this supervisor can do at this hackathon",
            {
                "fields": list(PERMISSIONS),
                "description": "Supervisors always see this hackathon's page. Tick what else they may do. Only admins change these.",
            },
        ),
    ]

    @admin.display(description="Switched on")
    def switched_on(self, obj):
        return ", ".join(label for name, (label, _, _) in PERMISSIONS.items() if getattr(obj, name)) or "-"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from .services import add_supervisor

        add_supervisor(obj.event, obj.person, obj.kind)  # gives the Supervisor role and admin access


class ScoreInline(admin.TabularInline):
    """Read-only here: marks are changed on the hackathon's marks page, which records a reason."""

    model = Score
    extra = 0
    fields = ["criterion", "marks", "updated_at"]
    readonly_fields = ["criterion", "marks", "updated_at"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class MarkChangeInline(admin.TabularInline):
    model = MarkChange
    extra = 0
    fields = ["kind", "changed_at", "changed_by", "reason", "before", "after"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Assignment)
class AssignmentAdmin(ScopedAdmin):
    perm = "can_edit_allocations"
    event_path = "round__event"
    list_display = ["team", "room", "faculty", "round", "source", "total_marks", "submitted_at"]
    list_filter = ["round__event", "round", ("submitted_at", admin.EmptyFieldListFilter), "source"]
    search_fields = ["team__name", "faculty__person__full_name", "faculty__person__email"]
    list_select_related = ["team__room", "faculty__person", "round"]
    fields = ["round", "faculty", "team", "source", "remarks", "submitted_at", "change_marks"]
    readonly_fields = ["source", "remarks", "submitted_at", "change_marks"]
    inlines = [ScoreInline, MarkChangeInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("scores")

    @admin.display(description="Marks")
    def change_marks(self, obj):
        if not obj.pk:
            return "-"
        url = reverse("hackathon_change_marks", args=[obj.round.event.slug, obj.pk])
        return format_html('<a href="{}">Change marks or reopen (on the hackathon page, with a reason)</a>', url)

    @admin.display(description="Room")
    def room(self, obj):
        return obj.team.room

    @admin.display(description="Total")
    def total_marks(self, obj):
        scores = list(obj.scores.all())
        return sum(s.marks for s in scores).normalize() if scores else "-"

    def has_delete_permission(self, request, obj=None):
        # An allocation with marks can only go if the person may also change marks.
        if obj is not None and obj.scores.exists() and not allowed(request.user, obj.round.event, "can_change_marks"):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(FacultyRequest)
class FacultyRequestAdmin(admin.ModelAdmin):
    """Decisions are made on the Faculty requests page; this list is for looking things up."""

    list_display = ["full_name", "person", "employee_id", "department", "event", "status", "created_at", "decided_by"]
    list_filter = ["status", "event"]
    search_fields = ["full_name", "employee_id", "person__email"]
    readonly_fields = [f.name for f in FacultyRequest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
