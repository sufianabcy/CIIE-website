"""The two roles an admin can give: Coordinator and Admin (participant is everyone)."""

from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions

COORDINATOR_PERMS = [
    "events.view_event",
    "events.change_event",
    "events.view_question",
    "events.add_question",
    "events.change_question",
    "registrations.view_registration",
    "checkin.view_checkin",
    "checkin.view_scanlog",
    "results.view_result",
    "results.add_result",
    "results.change_result",
    "judging.view_judge",
    "judging.add_judge",
    "judging.change_judge",
    "judging.view_criterion",
    "judging.add_criterion",
    "judging.change_criterion",
    "judging.delete_criterion",
    "judging.view_assignment",
    "judging.add_assignment",
    "judging.change_assignment",
    "judging.delete_assignment",
    "judging.view_score",
    "judging.change_score",
]

ADMIN_APPS = ["people", "events", "registrations", "checkin", "results", "judging", "mailer", "ops", "pghistory", "account", "socialaccount", "auth"]
# Rows that are never deleted, even by admins: they deactivate, cancel or undo instead.
NEVER_DELETE = {"person", "registration", "checkin", "scanlog", "result", "event", "outboxemail", "backuprun", "sitesettings"}


def ensure_roles(apps=global_apps, **kwargs) -> None:
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for app_config in global_apps.get_app_configs():
        create_permissions(app_config, verbosity=0)

    def perms(codes):
        found = []
        for code in codes:
            app_label, codename = code.split(".")
            found += list(Permission.objects.filter(content_type__app_label=app_label, codename=codename))
        return found

    coordinator, _ = Group.objects.get_or_create(name="Coordinator")
    coordinator.permissions.set(perms(COORDINATOR_PERMS))

    admin, _ = Group.objects.get_or_create(name="Admin")
    admin_perms = Permission.objects.filter(content_type__app_label__in=ADMIN_APPS).exclude(
        codename__in=[f"delete_{m}" for m in NEVER_DELETE]
    )
    admin.permissions.set(admin_perms)


def on_post_migrate(sender, **kwargs):
    ensure_roles()
