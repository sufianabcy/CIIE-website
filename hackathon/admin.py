from django.contrib import admin
from django.db.models import Q

from events.models import Event

from .access import can_change_marks
from .models import Assignment, Criterion, EventFaculty, Room, Round, Score, Supervisor, Team, TeamMember
from .services import room_change_warnings


def overseen_q(request, path="event"):
    """Rows of hackathons this person runs: all for admins; their events for coordinators and supervisors."""
    user = request.user
    return Q(**{f"{path}__coordinators": user}) | Q(
        **{f"{path}__supervisors__person": user, f"{path}__supervisors__is_active": True}
    )


class ScopedAdmin(admin.ModelAdmin):
    event_path = "event"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_portal_admin:
            return qs
        return qs.filter(overseen_q(request, self.event_path)).distinct()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_portal_admin:
            model = db_field.related_model
            if model is Event:
                kwargs["queryset"] = Event.objects.filter(
                    Q(coordinators=request.user) | Q(supervisors__person=request.user, supervisors__is_active=True),
                    kind=Event.Kind.HACKATHON,
                ).distinct()
            elif model in (Round, Room, Team, EventFaculty):
                kwargs["queryset"] = model.objects.filter(overseen_q(request)).distinct()
        elif db_field.related_model is Event:
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
        # Publishing results to students is the admin's call.
        return [] if request.user.is_portal_admin else ["results_published_at"]


@admin.register(Room)
class RoomAdmin(ScopedAdmin):
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


@admin.register(Team)
class TeamAdmin(ScopedAdmin):
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
    list_display = ["person", "kind", "event", "is_active"]
    list_filter = ["event", "kind", "is_active"]
    search_fields = ["person__full_name", "person__email", "person__employee_id", "person__student_id"]
    autocomplete_fields = ["person"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from .services import add_supervisor

        add_supervisor(obj.event, obj.person, obj.kind)  # gives the Supervisor role and admin access


class ScoreInline(admin.TabularInline):
    model = Score
    extra = 0
    fields = ["criterion", "marks", "updated_at"]
    readonly_fields = ["updated_at"]

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj and "criterion" in formset.form.base_fields:
            formset.form.base_fields["criterion"].queryset = Criterion.objects.filter(round=obj.round)
        return formset

    # Only admins change marks (see access.can_change_marks).
    def has_add_permission(self, request, obj=None):
        return can_change_marks(request.user)

    def has_change_permission(self, request, obj=None):
        return can_change_marks(request.user)

    def has_delete_permission(self, request, obj=None):
        return can_change_marks(request.user)


@admin.register(Assignment)
class AssignmentAdmin(ScopedAdmin):
    event_path = "round__event"
    list_display = ["team", "room", "faculty", "round", "source", "total_marks", "submitted_at"]
    list_filter = ["round__event", "round", ("submitted_at", admin.EmptyFieldListFilter), "source"]
    search_fields = ["team__name", "faculty__person__full_name", "faculty__person__email"]
    list_select_related = ["team__room", "faculty__person", "round"]
    fields = ["round", "faculty", "team", "source", "remarks", "submitted_at"]
    readonly_fields = ["source"]
    inlines = [ScoreInline]
    actions = ["reopen"]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("scores")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not can_change_marks(request.user):
            fields += ["remarks", "submitted_at"]
        return fields

    @admin.display(description="Room")
    def room(self, obj):
        return obj.team.room

    @admin.display(description="Total")
    def total_marks(self, obj):
        scores = list(obj.scores.all())
        return sum(s.marks for s in scores).normalize() if scores else "-"

    @admin.action(description="Let faculty change these marks again (admins only)")
    def reopen(self, request, queryset):
        if not can_change_marks(request.user):
            self.message_user(request, "Only admins can reopen submitted marks.", level="error")
            return
        n = queryset.exclude(submitted_at__isnull=True).update(submitted_at=None)
        self.message_user(request, f"{n} allocation(s) reopened. The faculty member must submit again.")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.scores.exists() and not can_change_marks(request.user):
            return False
        return super().has_delete_permission(request, obj)

