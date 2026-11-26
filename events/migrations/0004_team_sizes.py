import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0003_remove_event_insert_insert_and_more'),
    ]

    operations = [
        pgtrigger.migrations.RemoveTrigger(
            model_name='event',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='event',
            name='update_update',
        ),
        migrations.AddField(
            model_name='event',
            name='team_size_max',
            field=models.PositiveSmallIntegerField(default=6, help_text='Hackathons: most members in a team, including the leader.', verbose_name='largest team'),
        ),
        migrations.AddField(
            model_name='event',
            name='team_size_min',
            field=models.PositiveSmallIntegerField(default=1, help_text='Hackathons: fewest members a team may register with (1 allows solo entries).', verbose_name='smallest team'),
        ),
        migrations.AddField(
            model_name='eventevent',
            name='team_size_max',
            field=models.PositiveSmallIntegerField(default=6, help_text='Hackathons: most members in a team, including the leader.', verbose_name='largest team'),
        ),
        migrations.AddField(
            model_name='eventevent',
            name='team_size_min',
            field=models.PositiveSmallIntegerField(default=1, help_text='Hackathons: fewest members a team may register with (1 allows solo entries).', verbose_name='smallest team'),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='event',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "events_eventevent" ("academic_year", "created_at", "description", "door_token", "earns_points", "ends_at", "faculty_in_charge_id", "faculty_in_charge_name", "form_version", "id", "is_practice", "kind", "kl_only", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "points_note", "registration_closes_at", "registration_opens_at", "report_status", "seats", "show_jury_to_students", "slug", "starts_at", "status", "student_lead_id", "student_lead_name", "summary", "team_size_max", "team_size_min", "time_slot", "title", "unit", "venue") VALUES (NEW."academic_year", NEW."created_at", NEW."description", NEW."door_token", NEW."earns_points", NEW."ends_at", NEW."faculty_in_charge_id", NEW."faculty_in_charge_name", NEW."form_version", NEW."id", NEW."is_practice", NEW."kind", NEW."kl_only", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."points_note", NEW."registration_closes_at", NEW."registration_opens_at", NEW."report_status", NEW."seats", NEW."show_jury_to_students", NEW."slug", NEW."starts_at", NEW."status", NEW."student_lead_id", NEW."student_lead_name", NEW."summary", NEW."team_size_max", NEW."team_size_min", NEW."time_slot", NEW."title", NEW."unit", NEW."venue"); RETURN NULL;', hash='5f5e358b434c32ecaf53f7ab7a49bdd9494c954d', operation='INSERT', pgid='pgtrigger_insert_insert_82b94', table='events_event', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='event',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD."academic_year" IS DISTINCT FROM (NEW."academic_year") OR OLD."created_at" IS DISTINCT FROM (NEW."created_at") OR OLD."description" IS DISTINCT FROM (NEW."description") OR OLD."door_token" IS DISTINCT FROM (NEW."door_token") OR OLD."earns_points" IS DISTINCT FROM (NEW."earns_points") OR OLD."ends_at" IS DISTINCT FROM (NEW."ends_at") OR OLD."faculty_in_charge_id" IS DISTINCT FROM (NEW."faculty_in_charge_id") OR OLD."faculty_in_charge_name" IS DISTINCT FROM (NEW."faculty_in_charge_name") OR OLD."form_version" IS DISTINCT FROM (NEW."form_version") OR OLD."id" IS DISTINCT FROM (NEW."id") OR OLD."is_practice" IS DISTINCT FROM (NEW."is_practice") OR OLD."kind" IS DISTINCT FROM (NEW."kind") OR OLD."kl_only" IS DISTINCT FROM (NEW."kl_only") OR OLD."points_note" IS DISTINCT FROM (NEW."points_note") OR OLD."registration_closes_at" IS DISTINCT FROM (NEW."registration_closes_at") OR OLD."registration_opens_at" IS DISTINCT FROM (NEW."registration_opens_at") OR OLD."report_status" IS DISTINCT FROM (NEW."report_status") OR OLD."seats" IS DISTINCT FROM (NEW."seats") OR OLD."show_jury_to_students" IS DISTINCT FROM (NEW."show_jury_to_students") OR OLD."slug" IS DISTINCT FROM (NEW."slug") OR OLD."starts_at" IS DISTINCT FROM (NEW."starts_at") OR OLD."status" IS DISTINCT FROM (NEW."status") OR OLD."student_lead_id" IS DISTINCT FROM (NEW."student_lead_id") OR OLD."student_lead_name" IS DISTINCT FROM (NEW."student_lead_name") OR OLD."summary" IS DISTINCT FROM (NEW."summary") OR OLD."team_size_max" IS DISTINCT FROM (NEW."team_size_max") OR OLD."team_size_min" IS DISTINCT FROM (NEW."team_size_min") OR OLD."time_slot" IS DISTINCT FROM (NEW."time_slot") OR OLD."title" IS DISTINCT FROM (NEW."title") OR OLD."unit" IS DISTINCT FROM (NEW."unit") OR OLD."venue" IS DISTINCT FROM (NEW."venue"))', func='INSERT INTO "events_eventevent" ("academic_year", "created_at", "description", "door_token", "earns_points", "ends_at", "faculty_in_charge_id", "faculty_in_charge_name", "form_version", "id", "is_practice", "kind", "kl_only", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "points_note", "registration_closes_at", "registration_opens_at", "report_status", "seats", "show_jury_to_students", "slug", "starts_at", "status", "student_lead_id", "student_lead_name", "summary", "team_size_max", "team_size_min", "time_slot", "title", "unit", "venue") VALUES (NEW."academic_year", NEW."created_at", NEW."description", NEW."door_token", NEW."earns_points", NEW."ends_at", NEW."faculty_in_charge_id", NEW."faculty_in_charge_name", NEW."form_version", NEW."id", NEW."is_practice", NEW."kind", NEW."kl_only", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."points_note", NEW."registration_closes_at", NEW."registration_opens_at", NEW."report_status", NEW."seats", NEW."show_jury_to_students", NEW."slug", NEW."starts_at", NEW."status", NEW."student_lead_id", NEW."student_lead_name", NEW."summary", NEW."team_size_max", NEW."team_size_min", NEW."time_slot", NEW."title", NEW."unit", NEW."venue"); RETURN NULL;', hash='ab2d3bf15336d7135149dbe1772569cc175ffa9f', operation='UPDATE', pgid='pgtrigger_update_update_73f59', table='events_event', when='AFTER')),
        ),
    ]
