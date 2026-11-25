"""The admin's side menu and the icons on the admin home.

The menu (templates/admin/nav_sidebar.html) uses the same task groups as the admin home (GROUPS in
ops/admin_home.py), gives each page an icon, marks the page you are on, and puts a red count on
Faculty requests and on the email queue when something there needs a look. Technical tables stay
under Advanced, for superusers only. If building the menu fails, the template falls back to
Django's own list, so a menu bug never breaks an admin page.
"""

import logging

from django import template
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from ops import admin_home

logger = logging.getLogger(__name__)
register = template.Library()

# Icons come from the portal's sprite (static/img/icons.svg, used through templates/ui/icon.html).
ICONS = {
    "events.Event": "calendar",
    "registrations.Registration": "ticket",
    "results.Result": "trophy",
    "checkin.CheckIn": "door",
    "checkin.ScanLog": "scan",
    "people.Person": "users",
    "mailer.OutboxEmail": "mail",
    "ops.SiteSettings": "gear",
    "ops.BackupRun": "shield",
    "sqlconsole.SqlRun": "clipboard",
    "pghistory.Events": "clock",
    "account.EmailAddress": "seal-check",
    "socialaccount.SocialApp": "sign-in",
    "socialaccount.SocialAccount": "user",
    "socialaccount.SocialToken": "qr",
}
GROUP_ICONS = {"Events": "calendar", "People": "users", "Hackathons": "rocket", "Email": "mail", "Settings": "gear", "Tools": "clipboard", "Status": "chart"}
TILE_ICONS = {"Faculty requests waiting": "student", "Emails failed or stuck": "mail", "Last good backup": "shield", "Events this week": "calendar"}
ACTION_ICONS = [("Create a hackathon", "rocket"), ("Import students", "download"), ("Give someone a role", "users"),
                ("Portal switches", "gear"), ("SQL console", "clipboard")]
DEFAULT_ICON = "list"


def _is_current(url, path, home):
    if not url:
        return False
    return path == url if url == home else path.startswith(url)


def _item(name, url, icon, path, home, add_url=None):
    return {"name": name, "url": url, "add_url": add_url, "icon": icon, "current": _is_current(url, path, home), "badge": 0}


def _counts(user):
    """Red counts for the menu: faculty requests waiting, and emails that failed or are stuck."""
    if not user.is_portal_admin:
        return 0, 0
    from hackathon.models import FacultyRequest
    from mailer.models import OutboxEmail

    stuck_before = timezone.now() - admin_home.STUCK_AFTER
    waiting = FacultyRequest.objects.filter(status=FacultyRequest.Status.PENDING).count()
    failed = OutboxEmail.objects.filter(status=OutboxEmail.Status.FAILED).count()
    stuck = OutboxEmail.objects.filter(status=OutboxEmail.Status.QUEUED, created_at__lt=stuck_before).count()
    return waiting, failed + stuck


def _hackathon_items(user, path, home, waiting):
    from events.models import Event

    events = Event.objects.coordinated_by(user) if user.is_coordinator else Event.objects.none()
    group = admin_home._hackathon_group(user, events.filter(kind=Event.Kind.HACKATHON).order_by("-starts_at"))
    if not group:
        return None
    try:
        requests_url = reverse("faculty_requests")
    except NoReverseMatch:
        requests_url = None
    items = []
    for name, url, add in group["links"]:
        item = _item(name, url, "student" if url == requests_url else "rocket", path, home, add)
        if url == requests_url:
            item["badge"], item["badge_title"] = waiting, "waiting for a decision"
        items.append(item)
    return {"title": group["title"], "icon": GROUP_ICONS["Hackathons"], "entries": items}


def build_nav(request, available_apps):
    user = request.user
    path = request.path
    home = reverse("admin:index")
    waiting, mail_problems = _counts(user)
    models = {}
    for app in available_apps:
        for m in app["models"]:
            key = f'{app["app_label"]}.{m["object_name"]}'
            item = _item(admin_home.FRIENDLY.get(key, m["name"]), m.get("admin_url"), ICONS.get(key, DEFAULT_ICON), path, home, m.get("add_url"))
            if key == "mailer.OutboxEmail":
                item["badge"], item["badge_title"] = mail_problems, "failed or stuck"
            models[key] = item
    groups = []
    for title, _note, keys in admin_home.GROUPS:
        items = [models.pop(k) for k in keys if k in models and (user.is_superuser or k not in admin_home.TECHNICAL)]
        if items:
            groups.append({"title": title, "icon": GROUP_ICONS.get(title, DEFAULT_ICON), "entries": items})
    hackathons = _hackathon_items(user, path, home, waiting)
    if hackathons:
        groups.insert(min(2, len(groups)), hackathons)  # same place as on the admin home
    advanced = sorted(models.values(), key=lambda m: m["name"]) if user.is_superuser else []
    try:
        status_url = reverse("ops_status") if user.is_portal_admin else None
    except NoReverseMatch:
        status_url = None
    return {
        "home": _item("Home", home, "home", path, home),
        "groups": groups,
        "advanced": advanced,
        "advanced_open": any(m["current"] for m in advanced),
        "status_url": status_url,
    }


@register.simple_tag(takes_context=True)
def admin_nav(context):
    """{% admin_nav as nav %}: the grouped menu for the signed-in staff member, or {} if it can't be built."""
    request = context.get("request")
    if request is None or not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    try:
        return build_nav(request, context.get("available_apps") or [])
    except Exception:  # the menu must never take an admin page down
        logger.exception("Could not build the admin menu; showing Django's list instead")
        return {}


@register.filter
def tile_icon(label):
    return TILE_ICONS.get(str(label), "info")


@register.filter
def action_icon(label):
    for prefix, icon in ACTION_ICONS:
        if str(label).startswith(prefix):
            return icon
    return "arrow-right"


@register.filter
def group_icon(title):
    return GROUP_ICONS.get(str(title), DEFAULT_ICON)


@register.filter
def is_count(value):
    """True for a number (the tile shows it big), False for words like "3 hours ago"."""
    return isinstance(value, int) and not isinstance(value, bool)
