import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hackathon', '0007_rooms_floors_volunteers_attendance'),
        ('registrations', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='teammember',
            options={'ordering': ['team', '-is_leader', 'registration__person__full_name'], 'verbose_name': 'team member'},
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='teammember',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='teammember',
            name='update_update',
        ),
        migrations.AddField(
            model_name='importupload',
            name='header_row',
            field=models.PositiveIntegerField(default=0, help_text='1-based row holding the column names. 0: the first non-empty row.'),
        ),
        migrations.AddField(
            model_name='importupload',
            name='sheet_name',
            field=models.CharField(blank=True, help_text='Empty: the first sheet.', max_length=100),
        ),
        migrations.AddField(
            model_name='teammember',
            name='is_leader',
            field=models.BooleanField(default=False, verbose_name='team leader'),
        ),
        migrations.AddField(
            model_name='teammemberevent',
            name='is_leader',
            field=models.BooleanField(default=False, verbose_name='team leader'),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='teammember',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_teammemberevent" ("extra", "id", "is_leader", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "registration_id", "team_id") VALUES (NEW."extra", NEW."id", NEW."is_leader", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."registration_id", NEW."team_id"); RETURN NULL;', hash='ef765749c133e1e95c0f13a5dcd45849299561c6', operation='INSERT', pgid='pgtrigger_insert_insert_41bd5', table='hackathon_teammember', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='teammember',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_teammemberevent" ("extra", "id", "is_leader", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "registration_id", "team_id") VALUES (NEW."extra", NEW."id", NEW."is_leader", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."registration_id", NEW."team_id"); RETURN NULL;', hash='02f5208241bf6e9eb1f0d07afe57b7f93e0ae881', operation='UPDATE', pgid='pgtrigger_update_update_f7dab', table='hackathon_teammember', when='AFTER')),
        ),
        migrations.AddConstraint(
            model_name='teammember',
            constraint=models.UniqueConstraint(condition=models.Q(('is_leader', True)), fields=('team',), name='one_leader_per_team'),
        ),
    ]
