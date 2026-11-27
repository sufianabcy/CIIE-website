import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('people', '0004_email_and_release'),
    ]

    operations = [
        pgtrigger.migrations.RemoveTrigger(
            model_name='person',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='person',
            name='update_update',
        ),
        migrations.AddField(
            model_name='person',
            name='designation',
            field=models.CharField(blank=True, help_text='For faculty, e.g. Professor & HoD.', max_length=150),
        ),
        migrations.AddField(
            model_name='personevent',
            name='designation',
            field=models.CharField(blank=True, help_text='For faculty, e.g. Professor & HoD.', max_length=150),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='person',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "people_personevent" ("branch", "contact_email", "date_joined", "designation", "email", "employee_id", "full_name", "id", "institution", "is_active", "is_staff", "is_superuser", "kind", "kl_email", "kl_verified_at", "login_id", "must_change_password", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "phone", "profile_completed_at", "source", "student_id", "temp_password_expires_at", "year_of_study") VALUES (NEW."branch", NEW."contact_email", NEW."date_joined", NEW."designation", NEW."email", NEW."employee_id", NEW."full_name", NEW."id", NEW."institution", NEW."is_active", NEW."is_staff", NEW."is_superuser", NEW."kind", NEW."kl_email", NEW."kl_verified_at", NEW."login_id", NEW."must_change_password", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."phone", NEW."profile_completed_at", NEW."source", NEW."student_id", NEW."temp_password_expires_at", NEW."year_of_study"); RETURN NULL;', hash='4bb9f7c4ddaf2183b7267ee6fd0b2c8eb82e85f8', operation='INSERT', pgid='pgtrigger_insert_insert_65688', table='people_person', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='person',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD."branch" IS DISTINCT FROM (NEW."branch") OR OLD."contact_email" IS DISTINCT FROM (NEW."contact_email") OR OLD."date_joined" IS DISTINCT FROM (NEW."date_joined") OR OLD."designation" IS DISTINCT FROM (NEW."designation") OR OLD."email" IS DISTINCT FROM (NEW."email") OR OLD."employee_id" IS DISTINCT FROM (NEW."employee_id") OR OLD."full_name" IS DISTINCT FROM (NEW."full_name") OR OLD."id" IS DISTINCT FROM (NEW."id") OR OLD."institution" IS DISTINCT FROM (NEW."institution") OR OLD."is_active" IS DISTINCT FROM (NEW."is_active") OR OLD."is_staff" IS DISTINCT FROM (NEW."is_staff") OR OLD."is_superuser" IS DISTINCT FROM (NEW."is_superuser") OR OLD."kind" IS DISTINCT FROM (NEW."kind") OR OLD."kl_email" IS DISTINCT FROM (NEW."kl_email") OR OLD."kl_verified_at" IS DISTINCT FROM (NEW."kl_verified_at") OR OLD."login_id" IS DISTINCT FROM (NEW."login_id") OR OLD."must_change_password" IS DISTINCT FROM (NEW."must_change_password") OR OLD."phone" IS DISTINCT FROM (NEW."phone") OR OLD."profile_completed_at" IS DISTINCT FROM (NEW."profile_completed_at") OR OLD."source" IS DISTINCT FROM (NEW."source") OR OLD."student_id" IS DISTINCT FROM (NEW."student_id") OR OLD."temp_password_expires_at" IS DISTINCT FROM (NEW."temp_password_expires_at") OR OLD."year_of_study" IS DISTINCT FROM (NEW."year_of_study"))', func='INSERT INTO "people_personevent" ("branch", "contact_email", "date_joined", "designation", "email", "employee_id", "full_name", "id", "institution", "is_active", "is_staff", "is_superuser", "kind", "kl_email", "kl_verified_at", "login_id", "must_change_password", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "phone", "profile_completed_at", "source", "student_id", "temp_password_expires_at", "year_of_study") VALUES (NEW."branch", NEW."contact_email", NEW."date_joined", NEW."designation", NEW."email", NEW."employee_id", NEW."full_name", NEW."id", NEW."institution", NEW."is_active", NEW."is_staff", NEW."is_superuser", NEW."kind", NEW."kl_email", NEW."kl_verified_at", NEW."login_id", NEW."must_change_password", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."phone", NEW."profile_completed_at", NEW."source", NEW."student_id", NEW."temp_password_expires_at", NEW."year_of_study"); RETURN NULL;', hash='20cf072c79def2669b3d270410b53b426d7e12e1', operation='UPDATE', pgid='pgtrigger_update_update_de829', table='people_person', when='AFTER')),
        ),
    ]
