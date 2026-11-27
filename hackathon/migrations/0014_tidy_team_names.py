# Tidy team names before they become unique ignoring case and extra spaces (0015 adds the rule).

from django.conf import settings
from django.db import migrations, models


def _clean(name):
    return " ".join(str(name or "").split())


def merge_duplicate_team_names(apps, schema_editor):
    """Before team names become unique ignoring case and extra spaces: tidy every name, and for teams whose
    names now match, merge a team that has no allocations, marks, attendance or round decisions into the other.
    A team that does have any of those is never merged: it is renamed "<name> (2)" and reported here."""
    Team = apps.get_model("hackathon", "Team")
    TeamMember = apps.get_model("hackathon", "TeamMember")
    Assignment = apps.get_model("hackathon", "Assignment")
    Attendance = apps.get_model("hackathon", "Attendance")
    RoundOutcome = apps.get_model("hackathon", "RoundOutcome")

    def used(team):
        return (Assignment.objects.filter(team=team).exists() or Attendance.objects.filter(team=team).exists()
                or RoundOutcome.objects.filter(team=team).exists())

    groups = {}
    for team in Team.objects.order_by("event_id", "pk"):
        groups.setdefault((team.event_id, _clean(team.name).lower()), []).append(team)
    for (event_id, _), teams in groups.items():
        clean = _clean(teams[0].name)
        if len(teams) > 1:
            keep = next((t for t in teams if used(t)), teams[0])
            taken = {key for (ev, key) in groups if ev == event_id}
            for other in teams:
                if other.pk == keep.pk:
                    continue
                if not used(other):
                    has_leader = TeamMember.objects.filter(team=keep, is_leader=True).exists()
                    moved = TeamMember.objects.filter(team=other)
                    if has_leader:
                        moved.update(is_leader=False)
                    moved.update(team=keep)
                    for field in ("project_title", "project_description", "mentor_name"):
                        if not getattr(keep, field) and getattr(other, field):
                            setattr(keep, field, getattr(other, field))
                    for field in ("room_id", "mentor_id"):
                        if getattr(keep, field) is None and getattr(other, field) is not None:
                            setattr(keep, field, getattr(other, field))
                    keep.save()
                    other_pk = other.pk
                    other.delete()
                    print(f"\n  Team names: merged team #{other_pk} \"{other.name}\" into #{keep.pk} \"{clean}\" (event {event_id}).")
                else:
                    n = 2
                    while f"{clean} ({n})".lower() in taken:
                        n += 1
                    new_name = f"{clean} ({n})"
                    taken.add(new_name.lower())
                    print(f"\n  Team names: team #{other.pk} \"{other.name}\" (event {event_id}) has allocations, marks or "
                          f"attendance, so it was not merged; renamed to \"{new_name}\". Check it on the Teams page.")
                    other.name = new_name
                    other.save(update_fields=["name"])
            teams = [keep]
        team = teams[0]
        if team.name != clean:
            team.name = clean
            team.save(update_fields=["name"])


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0006_students_form_teams"),
        ("hackathon", "0013_import_blank_teams"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="team",
            name="team_name_unique_per_event",
        ),
        migrations.RemoveField(
            model_name="importupload",
            name="blank_teams",
        ),
        migrations.RunPython(merge_duplicate_team_names, migrations.RunPython.noop),
    ]
