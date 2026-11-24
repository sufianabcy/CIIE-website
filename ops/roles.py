"""Roles: Admin, Coordinator, Supervisor and Faculty (student is everyone).

Supervisor and Faculty are scoped to events by hackathon.Supervisor and
hackathon.EventFaculty rows; the groups give the model permissions.
"""

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
    # Coordinators run hackathons they are added to like a supervisor: details yes, marks no.
    "hackathon.view_eventfaculty",
    "hackathon.add_eventfaculty",
    "hackathon.change_eventfaculty",
    "hackathon.view_supervisor",
    "hackathon.view_room",
    "hackathon.add_room",
    "hackathon.change_room",
    "hackathon.delete_room",
    "hackathon.view_team",
    "hackathon.add_team",
    "hackathon.change_team",
    "hackathon.view_teammember",
    "hackathon.add_teammember",
    "hackathon.change_teammember",
    "hackathon.delete_teammember",
    "hackathon.view_round",
    "hackathon.add_round",
    "hackathon.change_round",
    "hackathon.view_criterion",
    "hackathon.add_criterion",
    "hackathon.change_criterion",
    "hackathon.delete_criterion",
    "hackathon.view_assignment",
    "hackathon.add_assignment",
    "hackathon.change_assignment",
    "hackathon.delete_assignment",
    "hackathon.view_score",
]

# Supervisors: everything about their hackathon except marks and the rubric; they never add supervisors.
SUPERVISOR_PERMS = [
    "events.view_event",
    "events.view_question",
    "registrations.view_registration",
    "hackathon.view_eventfaculty",
    "hackathon.change_eventfaculty",
    "hackathon.view_supervisor",
    "hackathon.view_room",
    "hackathon.add_room",
    "hackathon.change_room",
    "hackathon.view_team",
    "hackathon.add_team",
    "hackathon.change_team",
    "hackathon.view_teammember",
    "hackathon.add_teammember",
    "hackathon.change_teammember",
    "hackathon.delete_teammember",
    "hackathon.view_round",
    "hackathon.change_round",
    "hackathon.view_criterion",
    "hackathon.view_assignment",
    "hackathon.add_assignment",
    "hackathon.change_assignment",
    "hackathon.delete_assignment",
    "hackathon.view_score",
]

ADMIN_APPS = ["people", "events", "registrations", "checkin", "results", "judging", "hackathon", "mailer", "ops", "pghistory", "account", "socialaccount", "auth"]
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

    supervisor, _ = Group.objects.get_or_create(name="Supervisor")
    supervisor.permissions.set(perms(SUPERVISOR_PERMS))

    Group.objects.get_or_create(name="Faculty")  # no admin permissions: faculty use the judging pages

    admin, _ = Group.objects.get_or_create(name="Admin")
    admin_perms = Permission.objects.filter(content_type__app_label__in=ADMIN_APPS).exclude(
        codename__in=[f"delete_{m}" for m in NEVER_DELETE]
    )
    admin.permissions.set(admin_perms)


def on_post_migrate(sender, **kwargs):
    ensure_roles()
