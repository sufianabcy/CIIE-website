"""The admin home page as a task dashboard: one Create event button, what needs attention, quick actions,
recent events, then every table in a few task groups (with a quick search). Technical tables are for
superusers only, under "Advanced".

The look comes from templates/admin/base_site.html and static/css/admin.css (the frontend's), plus
static/css/admin-layout.css for this page's layout.
"""

from datetime import timedelta

from django.contrib import admin
from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

# Task groups: title, one-line description, tables ("app_label.ModelName") in order.
GROUPS = [
    ("Events", "Events, their registrations and results.", ["events.Event", "registrations.Registration", "results.Result", "checkin.CheckIn", "checkin.ScanLog"]),
    ("People", "Accounts and roles: students, faculty, coordinators, admins.", ["people.Person"]),
    ("Email", "The email queue: sent, waiting and failed.", ["mailer.OutboxEmail"]),
    ("Settings", "Portal switches and backups.", ["ops.SiteSettings", "ops.BackupRun"]),
    ("Tools", "Run SQL, and see who changed what.", ["sqlconsole.SqlRun", "pghistory.Events"]),
]
# Shown to superusers only (and only under Advanced on the home page).
TECHNICAL = {"pghistory.Events", "account.EmailAddress", "socialaccount.SocialApp", "socialaccount.SocialAccount", "socialaccount.SocialToken"}
FRIENDLY = {
    "pghistory.Events": "Change history",
    "socialaccount.SocialApp": "Google / Microsoft sign-in keys",
    "socialaccount.SocialAccount": "Linked Google / Microsoft accounts",
    "socialaccount.SocialToken": "Sign-in tokens",
    "account.EmailAddress": "Verified email addresses",
    "sqlconsole.SqlRun": "SQL console",
}
STUCK_AFTER = timedelta(minutes=15)


def _key(app_label, model):
    return f"{app_label}.{model}"


def _grouped(app_list, superuser):
    models = {}
    for app in app_list:
        for m in app["models"]:
            key = _key(app["app_label"], m["object_name"])
            m["name"] = FRIENDLY.get(key, m["name"])
            models[key] = m
    groups = []
    for title, note, keys in GROUPS:
        items = [models.pop(k) for k in keys if k in models and (superuser or k not in TECHNICAL)]
        if items:
            groups.append({"title": title, "note": note, "links": [(m["name"], m.get("admin_url"), m.get("add_url")) for m in items]})
    advanced = sorted(((m["name"], m.get("admin_url"), m.get("add_url")) for m in models.values()), key=lambda x: x[0]) if superuser else []
    return groups, advanced


def _hackathon_group(user, events):
    links = [(f"{e.title}", reverse("hackathon_dashboard", args=[e.slug]), None) for e in events if e.is_hackathon][:8]
    if user.is_portal_admin:
        links.append(("Faculty requests", reverse("faculty_requests"), None))
    if not links:
        return None
    return {"title": "Hackathons", "note": "Students, teams, rooms, faculty, rounds and marks live on each hackathon's page.", "links": links}


def _age(dt, now):
    if not dt:
        return "never"
    days = (now - dt).days
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int((now - dt).total_seconds() // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''} ago" if hours else "within the hour"


def home_context(request, app_list):
    from events.models import Event
    from hackathon.models import FacultyRequest
    from mailer.models import OutboxEmail
    from registrations.models import Registration

    from .models import BackupRun

    user = request.user
    now = timezone.now()
    events = Event.objects.coordinated_by(user) if user.is_coordinator else Event.objects.none()
    recent = list(
        events.filter(ends_at__gte=now - timedelta(days=7)).order_by("starts_at")
        .annotate(registered=Count("registrations", filter=Q(registrations__status=Registration.Status.CONFIRMED)))[:8]
    )
    groups, advanced = _grouped(app_list, user.is_superuser)
    hack_group = _hackathon_group(user, events.filter(kind=Event.Kind.HACKATHON).order_by("-starts_at"))
    if hack_group:
        groups.insert(2, hack_group)

    can_add_event = user.has_perm("events.add_event") and user.is_portal_admin
    add_url = reverse("admin:events_event_add")
    attention = []
    if user.is_portal_admin:
        backup = BackupRun.objects.filter(ok=True).order_by("-started_at").first()
        failed = OutboxEmail.objects.filter(status=OutboxEmail.Status.FAILED).count()
        stuck = OutboxEmail.objects.filter(status=OutboxEmail.Status.QUEUED, created_at__lt=now - STUCK_AFTER).count()
        week_end = now + timedelta(days=7)
        attention = [
            {"label": "Faculty requests waiting", "value": FacultyRequest.objects.filter(status=FacultyRequest.Status.PENDING).count(),
             "url": reverse("faculty_requests"), "alert": True},
            {"label": "Emails failed or stuck", "value": failed + stuck,
             "url": reverse("admin:mailer_outboxemail_changelist") + ("?status__exact=failed" if failed else "?status__exact=queued"), "alert": True},
            {"label": "Last good backup", "value": _age(backup.started_at if backup else None, now),
             "url": reverse("admin:ops_backuprun_changelist"), "alert": not backup or now - backup.started_at > timedelta(days=2)},
            {"label": "Events this week", "value": Event.objects.filter(starts_at__lt=week_end, ends_at__gte=now).count(),
             "url": reverse("admin:events_event_changelist")},
        ]
    actions = []
    if can_add_event:
        actions.append({"label": "Create a hackathon", "note": "Teams, rooms, rounds and faculty marking.", "url": add_url + "?kind=hackathon"})
        hackathons = [e for e in recent if e.is_hackathon]
        if hackathons:
            actions.append({"label": f"Import students into {hackathons[0].title}", "note": "Upload the students Excel on its hackathon page.",
                            "url": reverse("hackathon_dashboard", args=[hackathons[0].slug])})
    if user.has_perm("people.change_person"):
        actions.append({"label": "Give someone a role", "note": "Make a person faculty, a coordinator or an admin.", "url": reverse("admin:people_person_changelist")})
    if user.has_perm("ops.change_sitesettings"):
        actions.append({"label": "Portal switches", "note": "Pause registration or emails, show a banner.", "url": reverse("admin:ops_sitesettings_changelist")})
    if user.is_portal_admin:
        actions.append({"label": "SQL console", "note": "Look things up, or fix data carefully.", "url": reverse("admin:sqlconsole_sqlrun_changelist")})
    try:
        status_url = reverse("ops_status") if user.is_portal_admin else None
    except NoReverseMatch:
        status_url = None
    return {
        "groups": groups, "advanced": advanced, "recent": recent, "actions": actions, "attention": attention,
        "can_add_event": can_add_event, "add_event_url": add_url + "?kind=normal", "status_url": status_url,
    }


def _superuser_only(model_admin):
    """Hide a technical table from everyone but superusers (the home page, the sidebar and its URLs)."""

    def has_module_permission(request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(request, obj=None):
        return request.user.is_active and request.user.is_superuser

    model_admin.has_module_permission = has_module_permission
    model_admin.has_view_permission = has_view_permission
    model_admin.has_change_permission = has_view_permission


def install():
    """Give the admin site this home page, and hide technical tables from non-superusers."""
    site = admin.site
    for model, model_admin in site._registry.items():
        if _key(model._meta.app_label, model.__name__) in TECHNICAL:
            _superuser_only(model_admin)
    original_index = site.index
    site.index_template = "admin/index.html"

    def index(request, extra_context=None):
        response = original_index(request, extra_context)
        if hasattr(response, "context_data"):
            response.context_data.update(home_context(request, response.context_data.get("app_list", [])))
        return response

    site.index = index
