"""Drop the first judging version's tables (replaced by this app; approved for deletion 24 Nov 2026).

Written as plain SQL so it also runs where the judging app no longer exists:
on a fresh database it does nothing.
"""

from django.db import migrations

TABLES = [
    "judging_scoreevent", "judging_assignmentevent", "judging_criterionevent", "judging_judgeevent",
    "judging_score", "judging_assignment", "judging_criterion", "judging_judge",
]


def forget_judging(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="judging").delete()  # takes its permissions with it
    schema_editor.execute("DELETE FROM django_migrations WHERE app = 'judging'")


class Migration(migrations.Migration):
    dependencies = [
        ("hackathon", "0002_facultyrequest_facultyrequestevent_and_more"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunSQL(
            ";".join(f"DROP TABLE IF EXISTS {t} CASCADE" for t in TABLES),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunPython(forget_judging, migrations.RunPython.noop),
    ]
