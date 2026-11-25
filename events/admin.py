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
    list_display = ["title", "kind", "starts_at", "status", "registered", "tools"]
    list_filter = ["kind", "status", "is_practice"]
    search_fields = ["title", "slug", "venue"]
    prepopulated_fields = {"slug": ["title"]}
    date_hierarchy = "starts_at"
    change_form_template = "admin/events/event/change_form.html"
    change_list_template = "admin/events/event/change_list.html"
    # The first screen when creating an event: just enough to save it. Everything else comes after.
    add_fieldsets = [
        (None, {"fields": ["title", "kind", "status"]}),
        ("When and where", {"fields": [("starts_at", "ends_at"), "venue"]}),
    ]
    HELP = {
        "kind": "A hackathon has teams, rooms, rounds and faculty marking, and no passes or door check-in.",
        "status": "Draft events are hidden from students. Publish when registration details are ready.",
        "slug": "The short link: /e/<this>/. Made from the title; change it only before anyone shares it.",
        "summary": "One line for the event card.",
        "registration_closes_at": "Leave empty to keep registration open until the event ends.",
        "coordinators": "People who run this event. They also need the Coordinator role (People page).",
    }
    autocomplete_fields = ["coordinators"]
    readonly_fields = ["seats_taken", "form_version", "door_team_list", "door_link", "tools"]
    inlines = [QuestionInline]

    fieldsets = [
        ("Basics", {"fields": ["title", "slug", "kind", "status", "summary", "description", "venue"]}),
        ("Dates", {"fields": [("starts_at", "ends_at"), ("registration_opens_at", "registration_closes_at")]}),
        (
            "Hackathon",
            {
                "classes": ["only-hackathon"],
                "fields": ["show_jury_to_students"],
                "description": "Only for hackathons. Students, teams, rooms, faculty, rounds and marks are on the event's Hackathon page.",
            },
        ),
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

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        fieldsets = super().get_fieldsets(request, obj)
        if obj.is_hackathon:  # no door at hackathons
            fieldsets = [
                (name, {**opts, "fields": [f for f in opts["fields"] if f not in ("door_link", "door_team_list")]})
                for name, opts in fieldsets
            ]
        else:
            fieldsets = [(name, opts) for name, opts in fieldsets if name != "Hackathon"]
        return fieldsets

    def get_inlines(self, request, obj):
        return [QuestionInline] if obj else []  # questions come after the first save

    def get_prepopulated_fields(self, request, obj=None):
        return self.prepopulated_fields if obj else {}

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change=change, **kwargs)
        for name, text in self.HELP.items():
            if name in form.base_fields:
                form.base_fields[name].help_text = text
        return form

    def save_model(self, request, obj, form, change):
        if not obj.slug:
            from django.utils.text import slugify

            base = slugify(obj.title)[:40] or "event"
            slug, n = base, 2
            while Event.objects.filter(slug=slug).exclude(pk=obj.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            obj.slug = slug
        if not obj.is_hackathon:
            obj.show_jury_to_students = False
        super().save_model(request, obj, form, change)

    def response_add(self, request, obj, post_url_continue=None):
        from django.contrib import messages
        from django.shortcuts import redirect

        messages.success(request, f"{obj.title} is created. Now add the details below: description, registration questions and the team.")
        return redirect("admin:events_event_change", obj.pk)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context.update(show_save_and_continue=False, show_save_and_add_another=False)
        return super().render_change_form(request, context, add, change, form_url, obj)

    @admin.display(description="Registered")
    def registered(self, obj):
        return obj.registrations.filter(status="confirmed").count()

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
            *([] if obj.is_hackathon else [("Door", reverse("door", args=[obj.slug])), ("Counts", reverse("door_counts", args=[obj.slug]))]),
            ("Report", reverse("event_report", args=[obj.slug])),
        ]
        if obj.is_hackathon:
            links.append(("Hackathon", reverse("hackathon_dashboard", args=[obj.slug])))
        return format_html_join(" · ", '<a href="{}">{}</a>', ((url, label) for label, url in links))
