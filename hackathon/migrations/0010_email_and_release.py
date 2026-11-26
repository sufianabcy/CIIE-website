import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hackathon', '0009_room_numbers'),
    ]

    operations = [
        pgtrigger.migrations.RemoveTrigger(
            model_name='round',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='round',
            name='update_update',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='supervisor',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='supervisor',
            name='update_update',
        ),
        migrations.AddField(
            model_name='round',
            name='release_marks_at',
            field=models.DateTimeField(blank=True, help_text='Teams see their own marks for this round automatically from this time.', null=True, verbose_name='show marks to students from'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='release_marks_at',
            field=models.DateTimeField(blank=True, help_text='Teams see their own marks for this round automatically from this time.', null=True, verbose_name='show marks to students from'),
        ),
        migrations.AddField(
            model_name='supervisor',
            name='can_email_participants',
            field=models.BooleanField(default=False, help_text="Send sign-in invites to this hackathon's students.", verbose_name='email participants'),
        ),
        migrations.AddField(
            model_name='supervisorevent',
            name='can_email_participants',
            field=models.BooleanField(default=False, help_text="Send sign-in invites to this hackathon's students.", verbose_name='email participants'),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='round',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_roundevent" ("closes_at", "event_id", "id", "name", "opens_at", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "position", "release_marks_at", "results_published_at") VALUES (NEW."closes_at", NEW."event_id", NEW."id", NEW."name", NEW."opens_at", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."position", NEW."release_marks_at", NEW."results_published_at"); RETURN NULL;', hash='d4e74ffe0cab8231fe50dfa98a8e8f4d83b8c57f', operation='INSERT', pgid='pgtrigger_insert_insert_2eb67', table='hackathon_round', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='round',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_roundevent" ("closes_at", "event_id", "id", "name", "opens_at", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "position", "release_marks_at", "results_published_at") VALUES (NEW."closes_at", NEW."event_id", NEW."id", NEW."name", NEW."opens_at", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."position", NEW."release_marks_at", NEW."results_published_at"); RETURN NULL;', hash='c02badb7cb75763483fd69dce7df8419415936df', operation='UPDATE', pgid='pgtrigger_update_update_254c6', table='hackathon_round', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='supervisor',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_supervisorevent" ("can_add_faculty", "can_approve_requests", "can_change_marks", "can_download", "can_edit_allocations", "can_edit_rounds", "can_edit_teams", "can_email_participants", "can_import", "can_make_logins", "can_manage_fields", "can_publish", "can_reopen_marks", "can_take_attendance", "can_view_floors", "can_view_marks", "created_at", "event_id", "extra", "id", "is_active", "kind", "person_id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id") VALUES (NEW."can_add_faculty", NEW."can_approve_requests", NEW."can_change_marks", NEW."can_download", NEW."can_edit_allocations", NEW."can_edit_rounds", NEW."can_edit_teams", NEW."can_email_participants", NEW."can_import", NEW."can_make_logins", NEW."can_manage_fields", NEW."can_publish", NEW."can_reopen_marks", NEW."can_take_attendance", NEW."can_view_floors", NEW."can_view_marks", NEW."created_at", NEW."event_id", NEW."extra", NEW."id", NEW."is_active", NEW."kind", NEW."person_id", _pgh_attach_context(), NOW(), \'insert\', NEW."id"); RETURN NULL;', hash='d4bb819da107461c30e856c0055e5540a93c6527', operation='INSERT', pgid='pgtrigger_insert_insert_2e839', table='hackathon_supervisor', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='supervisor',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_supervisorevent" ("can_add_faculty", "can_approve_requests", "can_change_marks", "can_download", "can_edit_allocations", "can_edit_rounds", "can_edit_teams", "can_email_participants", "can_import", "can_make_logins", "can_manage_fields", "can_publish", "can_reopen_marks", "can_take_attendance", "can_view_floors", "can_view_marks", "created_at", "event_id", "extra", "id", "is_active", "kind", "person_id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id") VALUES (NEW."can_add_faculty", NEW."can_approve_requests", NEW."can_change_marks", NEW."can_download", NEW."can_edit_allocations", NEW."can_edit_rounds", NEW."can_edit_teams", NEW."can_email_participants", NEW."can_import", NEW."can_make_logins", NEW."can_manage_fields", NEW."can_publish", NEW."can_reopen_marks", NEW."can_take_attendance", NEW."can_view_floors", NEW."can_view_marks", NEW."created_at", NEW."event_id", NEW."extra", NEW."id", NEW."is_active", NEW."kind", NEW."person_id", _pgh_attach_context(), NOW(), \'update\', NEW."id"); RETURN NULL;', hash='32cbbde793cb5c8e39dd004970119f60b091d70b', operation='UPDATE', pgid='pgtrigger_update_update_81ce7', table='hackathon_supervisor', when='AFTER')),
        ),
    ]
