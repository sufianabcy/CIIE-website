from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import Event, Question


class QuestionFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if not self.instance.pk or not self.instance.has_answers():
            return
        for form in self.deleted_forms:
            if form.instance.pk:
                raise ValidationError(
                    f"People have already answered, so \"{form.instance.label}\" cannot be deleted. Tick \"hidden\" instead."
                )


class QuestionInline(admin.StackedInline):
    model = Question
    formset = QuestionFormSet
    extra = 0
    fields = [("key", "kind", "required", "is_hidden", "position"), "label", "help_text", "options"]
    prepopulated_fields = {}


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["title", "starts_at", "status", "tools"]
    list_filter = ["status"]
    search_fields = ["title", "slug", "venue"]
    prepopulated_fields = {"slug": ["title"]}
    autocomplete_fields = ["coordinators"]
    readonly_fields = ["seats_taken", "form_version", "door_team_list", "door_link", "tools"]
    inlines = [QuestionInline]

    def get_inlines(self, request, obj):
        from judging.admin import CriterionInline

        return [QuestionInline, CriterionInline]
    fieldsets = [
        (None, {"fields": ["title", "slug", "status", "summary", "description", "venue"]}),
        ("When", {"fields": [("starts_at", "ends_at"), ("registration_opens_at", "registration_closes_at")]}),
        ("Team", {"fields": ["coordinators", "door_link", "door_team_list"]}),
        (
            "Report fields (optional)",
            {
                "classes": ["collapse"],
                "description": "Fill these in only if the event needs a report.",
                "fields": [
                    "unit",
                    "academic_year",
                    ("faculty_in_charge_name", "faculty_in_charge_id"),
                    ("student_lead_name", "student_lead_id"),
                    "time_slot",
                    "report_status",
                ]
            },
        ),
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_portal_admin else qs.filter(coordinators=request.user)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.is_portal_admin:
            fields.append("coordinators")
        return fields

    def has_add_permission(self, request):
        return request.user.is_portal_admin

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and (obj is None or not obj.registrations.exists())

    @admin.display(description="Seats")
    def seats_display(self, obj):
        return f"{obj.seats_taken} / {obj.seats}" if obj.seats is not None else f"{obj.seats_taken} / no limit"

    @admin.display(description="Door-team link")
    def door_link(self, obj):
        if not obj.pk:
            return "Save the event first."
        url = settings.SITE_URL + reverse("door_join", args=[obj.slug, obj.door_token])
        return format_html('<code>{}</code><br><small>Share it the day before. Each volunteer signs in as themselves.</small>', url)

    @admin.display(description="Door team")
    def door_team_list(self, obj):
        if not obj.pk:
            return "-"
        names = obj.door_team.all()
        return format_html_join(", ", "{}", ((p.display_name,) for p in names)) or "Nobody yet"

    @admin.display(description="Open")
    def tools(self, obj):
        if not obj.pk:
            return "-"
        links = [
            ("Event page", reverse("event_detail", args=[obj.slug])),
            ("Door", reverse("door", args=[obj.slug])),
            ("Counts", reverse("door_counts", args=[obj.slug])),
            ("Report", reverse("event_report", args=[obj.slug])),
            ("Judging", reverse("judging_dashboard", args=[obj.slug])),
        ]
        return format_html_join(" · ", '<a href="{}">{}</a>', ((url, label) for label, url in links))
