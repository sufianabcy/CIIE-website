import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registrations', '0001_initial'),
    ]

    operations = [
        pgtrigger.migrations.RemoveTrigger(
            model_name='registration',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='registration',
            name='update_update',
        ),
        migrations.AddField(
            model_name='registration',
            name='added_email_at',
            field=models.DateTimeField(blank=True, help_text='When the "you\'ve been added" or confirmation email was queued. Sent once per person per event.', null=True),
        ),
        migrations.AddField(
            model_name='registrationevent',
            name='added_email_at',
            field=models.DateTimeField(blank=True, help_text='When the "you\'ve been added" or confirmation email was queued. Sent once per person per event.', null=True),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='registration',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "registrations_registrationevent" ("added_email_at", "answers", "cancelled_at", "cancelled_by_id", "created_at", "created_by_id", "event_id", "form_version", "id", "pass_code", "person_id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "source", "status") VALUES (NEW."added_email_at", NEW."answers", NEW."cancelled_at", NEW."cancelled_by_id", NEW."created_at", NEW."created_by_id", NEW."event_id", NEW."form_version", NEW."id", NEW."pass_code", NEW."person_id", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."source", NEW."status"); RETURN NULL;', hash='05a5a4a30da170f931896e230587eba8caefd759', operation='INSERT', pgid='pgtrigger_insert_insert_b1473', table='registrations_registration', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='registration',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "registrations_registrationevent" ("added_email_at", "answers", "cancelled_at", "cancelled_by_id", "created_at", "created_by_id", "event_id", "form_version", "id", "pass_code", "person_id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "source", "status") VALUES (NEW."added_email_at", NEW."answers", NEW."cancelled_at", NEW."cancelled_by_id", NEW."created_at", NEW."created_by_id", NEW."event_id", NEW."form_version", NEW."id", NEW."pass_code", NEW."person_id", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."source", NEW."status"); RETURN NULL;', hash='678d0ec98c78c12d0f9dd6844d0f90540068108b', operation='UPDATE', pgid='pgtrigger_update_update_12872', table='registrations_registration', when='AFTER')),
        ),
    ]
