import pgtrigger.compiler
import pgtrigger.migrations
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0006_students_form_teams"),
        ("hackathon", "0016_volunteer_rooms"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        pgtrigger.migrations.RemoveTrigger(
            model_name="team",
            name="insert_insert",
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name="team",
            name="update_update",
        ),
        migrations.AddField(
            model_name="importupload",
            name="options",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Choices on the column step, e.g. how teams are formed.",
            ),
        ),
        migrations.AddField(
            model_name="rubricsettings",
            name="guidance",
            field=models.TextField(
                blank=True, help_text="Shown at the top of every faculty marksheet."
            ),
        ),
        migrations.AddField(
            model_name="team",
            name="category",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="team",
            name="number",
            field=models.CharField(
                blank=True,
                help_text="From the Team No column of the students Excel. Unique in this hackathon.",
                max_length=30,
                verbose_name="team number",
            ),
        ),
        migrations.AddField(
            model_name="teamevent",
            name="category",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="teamevent",
            name="number",
            field=models.CharField(
                blank=True,
                help_text="From the Team No column of the students Excel. Unique in this hackathon.",
                max_length=30,
                verbose_name="team number",
            ),
        ),
        migrations.AlterField(
            model_name="criterion",
            name="tag",
            field=models.CharField(
                blank=True, help_text="A short tag, e.g. Q3.", max_length=60
            ),
        ),
        migrations.AlterField(
            model_name="criterionevent",
            name="tag",
            field=models.CharField(
                blank=True, help_text="A short tag, e.g. Q3.", max_length=60
            ),
        ),
        migrations.AlterField(
            model_name="rubricsettings",
            name="self_score_max",
            field=models.PositiveSmallIntegerField(
                blank=True,
                default=50,
                help_text="Empty: faculty don't record a self-score.",
                null=True,
                verbose_name="student self-score out of",
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name="team",
            trigger=pgtrigger.compiler.Trigger(
                name="insert_insert",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='INSERT INTO "hackathon_teamevent" ("category", "created_at", "event_id", "extra", "id", "mentor_id", "mentor_name", "name", "number", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "project_description", "project_title", "room_id") VALUES (NEW."category", NEW."created_at", NEW."event_id", NEW."extra", NEW."id", NEW."mentor_id", NEW."mentor_name", NEW."name", NEW."number", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."project_description", NEW."project_title", NEW."room_id"); RETURN NULL;',
                    hash="e43fb8c02d3544ac1228420accd4d5ea2f3a2229",
                    operation="INSERT",
                    pgid="pgtrigger_insert_insert_8ed40",
                    table="hackathon_team",
                    when="AFTER",
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name="team",
            trigger=pgtrigger.compiler.Trigger(
                name="update_update",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition="WHEN (OLD.* IS DISTINCT FROM NEW.*)",
                    func='INSERT INTO "hackathon_teamevent" ("category", "created_at", "event_id", "extra", "id", "mentor_id", "mentor_name", "name", "number", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "project_description", "project_title", "room_id") VALUES (NEW."category", NEW."created_at", NEW."event_id", NEW."extra", NEW."id", NEW."mentor_id", NEW."mentor_name", NEW."name", NEW."number", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."project_description", NEW."project_title", NEW."room_id"); RETURN NULL;',
                    hash="d73d2d54fa7614333969da5703f4411a18516ac1",
                    operation="UPDATE",
                    pgid="pgtrigger_update_update_86b0e",
                    table="hackathon_team",
                    when="AFTER",
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="team",
            constraint=models.UniqueConstraint(
                condition=models.Q(("number", ""), _negated=True),
                fields=("event", "number"),
                name="team_number_unique_per_event",
            ),
        ),
    ]
