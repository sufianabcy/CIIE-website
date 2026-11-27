import django.db.models.deletion
import django.utils.timezone
import pgtrigger.compiler
import pgtrigger.migrations
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0005_registration_switch'),
        ('hackathon', '0011_designation_and_constants'),
        ('pghistory', '0007_auto_20250421_0444'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RoundOutcome',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('advanced', models.BooleanField()),
                ('decided_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
        migrations.CreateModel(
            name='RoundOutcomeEvent',
            fields=[
                ('pgh_id', models.AutoField(primary_key=True, serialize=False)),
                ('pgh_created_at', models.DateTimeField(auto_now_add=True)),
                ('pgh_label', models.TextField(help_text='The event label.')),
                ('id', models.BigIntegerField()),
                ('advanced', models.BooleanField()),
                ('decided_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='RubricSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bands', models.JSONField(default=list, help_text='Highest first: name and the lowest percentage, e.g. Strong from 85%.')),
                ('red_flags', models.JSONField(blank=True, default=list)),
                ('self_score_max', models.PositiveSmallIntegerField(default=50, verbose_name='student self-score out of')),
                ('recommendations', models.JSONField(blank=True, default=list)),
            ],
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='assignment',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='assignment',
            name='update_update',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='criterion',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='criterion',
            name='update_update',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='round',
            name='insert_insert',
        ),
        pgtrigger.migrations.RemoveTrigger(
            model_name='round',
            name='update_update',
        ),
        migrations.AddField(
            model_name='assignment',
            name='advance_anyway',
            field=models.BooleanField(default=False, verbose_name='recommend advancing anyway'),
        ),
        migrations.AddField(
            model_name='assignment',
            name='comments',
            field=models.JSONField(blank=True, default=dict, help_text="The round's comment prompts and the answers."),
        ),
        migrations.AddField(
            model_name='assignment',
            name='recommendation',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='assignment',
            name='red_flags',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='assignment',
            name='self_score',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True, verbose_name='student self-score'),
        ),
        migrations.AddField(
            model_name='assignmentevent',
            name='advance_anyway',
            field=models.BooleanField(default=False, verbose_name='recommend advancing anyway'),
        ),
        migrations.AddField(
            model_name='assignmentevent',
            name='comments',
            field=models.JSONField(blank=True, default=dict, help_text="The round's comment prompts and the answers."),
        ),
        migrations.AddField(
            model_name='assignmentevent',
            name='recommendation',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='assignmentevent',
            name='red_flags',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='assignmentevent',
            name='self_score',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True, verbose_name='student self-score'),
        ),
        migrations.AddField(
            model_name='criterion',
            name='strong_text',
            field=models.TextField(blank=True, verbose_name='strong looks like'),
        ),
        migrations.AddField(
            model_name='criterion',
            name='tag',
            field=models.CharField(blank=True, help_text='A short tag, e.g. Q3.', max_length=20),
        ),
        migrations.AddField(
            model_name='criterion',
            name='weak_text',
            field=models.TextField(blank=True, verbose_name='weak looks like'),
        ),
        migrations.AddField(
            model_name='criterionevent',
            name='strong_text',
            field=models.TextField(blank=True, verbose_name='strong looks like'),
        ),
        migrations.AddField(
            model_name='criterionevent',
            name='tag',
            field=models.CharField(blank=True, help_text='A short tag, e.g. Q3.', max_length=20),
        ),
        migrations.AddField(
            model_name='criterionevent',
            name='weak_text',
            field=models.TextField(blank=True, verbose_name='weak looks like'),
        ),
        migrations.AddField(
            model_name='round',
            name='allow_override',
            field=models.BooleanField(default=False, verbose_name='evaluators may recommend advancing anyway'),
        ),
        migrations.AddField(
            model_name='round',
            name='comment_prompts',
            field=models.JSONField(blank=True, default=list, help_text='One prompt per line, filled in by faculty for each team.'),
        ),
        migrations.AddField(
            model_name='round',
            name='focus',
            field=models.TextField(blank=True, help_text='What this round is about, shown to faculty.', verbose_name='focus'),
        ),
        migrations.AddField(
            model_name='round',
            name='key_criterion',
            field=models.ForeignKey(blank=True, help_text='Optional: a criterion that must reach a band to advance.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='hackathon.criterion'),
        ),
        migrations.AddField(
            model_name='round',
            name='key_min_band',
            field=models.CharField(blank=True, max_length=40, verbose_name='key criterion at least'),
        ),
        migrations.AddField(
            model_name='round',
            name='min_total',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Optional: the average total a team needs to go to the next round.', max_digits=6, null=True, verbose_name='advance with at least'),
        ),
        migrations.AddField(
            model_name='round',
            name='rubric_unlocked',
            field=models.BooleanField(default=False, editable=False, help_text='An admin unlocked the rubric after marks exist.'),
        ),
        migrations.AddField(
            model_name='round',
            name='suggested_questions',
            field=models.TextField(blank=True, help_text='Questions faculty can ask, shown on the marksheet.'),
        ),
        migrations.AddField(
            model_name='round',
            name='total_marks',
            field=models.DecimalField(decimal_places=2, default=100, help_text="The criteria's max marks should add up to this.", max_digits=6, verbose_name='round total'),
        ),
        migrations.AddField(
            model_name='round',
            name='weight',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Share of the weighted total, e.g. 0.25. All rounds together make 1.', max_digits=4, null=True),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='allow_override',
            field=models.BooleanField(default=False, verbose_name='evaluators may recommend advancing anyway'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='comment_prompts',
            field=models.JSONField(blank=True, default=list, help_text='One prompt per line, filled in by faculty for each team.'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='focus',
            field=models.TextField(blank=True, help_text='What this round is about, shown to faculty.', verbose_name='focus'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='key_criterion',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='Optional: a criterion that must reach a band to advance.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', related_query_name='+', to='hackathon.criterion'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='key_min_band',
            field=models.CharField(blank=True, max_length=40, verbose_name='key criterion at least'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='min_total',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Optional: the average total a team needs to go to the next round.', max_digits=6, null=True, verbose_name='advance with at least'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='rubric_unlocked',
            field=models.BooleanField(default=False, editable=False, help_text='An admin unlocked the rubric after marks exist.'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='suggested_questions',
            field=models.TextField(blank=True, help_text='Questions faculty can ask, shown on the marksheet.'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='total_marks',
            field=models.DecimalField(decimal_places=2, default=100, help_text="The criteria's max marks should add up to this.", max_digits=6, verbose_name='round total'),
        ),
        migrations.AddField(
            model_name='roundevent',
            name='weight',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Share of the weighted total, e.g. 0.25. All rounds together make 1.', max_digits=4, null=True),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='assignment',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_assignmentevent" ("advance_anyway", "comments", "created_at", "faculty_id", "id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "recommendation", "red_flags", "remarks", "round_id", "self_score", "source", "submitted_at", "team_id", "updated_at") VALUES (NEW."advance_anyway", NEW."comments", NEW."created_at", NEW."faculty_id", NEW."id", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."recommendation", NEW."red_flags", NEW."remarks", NEW."round_id", NEW."self_score", NEW."source", NEW."submitted_at", NEW."team_id", NEW."updated_at"); RETURN NULL;', hash='af42751a572fe9d7be30875b9fbea6735a37d54d', operation='INSERT', pgid='pgtrigger_insert_insert_bd607', table='hackathon_assignment', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='assignment',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_assignmentevent" ("advance_anyway", "comments", "created_at", "faculty_id", "id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "recommendation", "red_flags", "remarks", "round_id", "self_score", "source", "submitted_at", "team_id", "updated_at") VALUES (NEW."advance_anyway", NEW."comments", NEW."created_at", NEW."faculty_id", NEW."id", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."recommendation", NEW."red_flags", NEW."remarks", NEW."round_id", NEW."self_score", NEW."source", NEW."submitted_at", NEW."team_id", NEW."updated_at"); RETURN NULL;', hash='93f632d08ff32f4388e83cb5eb129b904dd7f693', operation='UPDATE', pgid='pgtrigger_update_update_d24bb', table='hackathon_assignment', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='criterion',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_criterionevent" ("description", "id", "label", "max_marks", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "position", "round_id", "strong_text", "tag", "weak_text") VALUES (NEW."description", NEW."id", NEW."label", NEW."max_marks", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."position", NEW."round_id", NEW."strong_text", NEW."tag", NEW."weak_text"); RETURN NULL;', hash='b56d11208efde58617cae065d67b09eb9023ddff', operation='INSERT', pgid='pgtrigger_insert_insert_a49b1', table='hackathon_criterion', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='criterion',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_criterionevent" ("description", "id", "label", "max_marks", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "position", "round_id", "strong_text", "tag", "weak_text") VALUES (NEW."description", NEW."id", NEW."label", NEW."max_marks", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."position", NEW."round_id", NEW."strong_text", NEW."tag", NEW."weak_text"); RETURN NULL;', hash='82291eaf2eb49a9885763288ff96215ba65fce48', operation='UPDATE', pgid='pgtrigger_update_update_c6552', table='hackathon_criterion', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='round',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_roundevent" ("allow_override", "closes_at", "comment_prompts", "event_id", "focus", "id", "key_criterion_id", "key_min_band", "min_total", "name", "opens_at", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "position", "release_marks_at", "results_published_at", "rubric_unlocked", "suggested_questions", "total_marks", "weight") VALUES (NEW."allow_override", NEW."closes_at", NEW."comment_prompts", NEW."event_id", NEW."focus", NEW."id", NEW."key_criterion_id", NEW."key_min_band", NEW."min_total", NEW."name", NEW."opens_at", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."position", NEW."release_marks_at", NEW."results_published_at", NEW."rubric_unlocked", NEW."suggested_questions", NEW."total_marks", NEW."weight"); RETURN NULL;', hash='929838f687c15bde60c9b6e1b5bece53ef15ea1f', operation='INSERT', pgid='pgtrigger_insert_insert_2eb67', table='hackathon_round', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='round',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_roundevent" ("allow_override", "closes_at", "comment_prompts", "event_id", "focus", "id", "key_criterion_id", "key_min_band", "min_total", "name", "opens_at", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "position", "release_marks_at", "results_published_at", "rubric_unlocked", "suggested_questions", "total_marks", "weight") VALUES (NEW."allow_override", NEW."closes_at", NEW."comment_prompts", NEW."event_id", NEW."focus", NEW."id", NEW."key_criterion_id", NEW."key_min_band", NEW."min_total", NEW."name", NEW."opens_at", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."position", NEW."release_marks_at", NEW."results_published_at", NEW."rubric_unlocked", NEW."suggested_questions", NEW."total_marks", NEW."weight"); RETURN NULL;', hash='209da19396291f9e26fc5ce81bf18a9a2706aa40', operation='UPDATE', pgid='pgtrigger_update_update_254c6', table='hackathon_round', when='AFTER')),
        ),
        migrations.AddField(
            model_name='roundoutcome',
            name='decided_by',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='roundoutcome',
            name='round',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='outcomes', to='hackathon.round'),
        ),
        migrations.AddField(
            model_name='roundoutcome',
            name='team',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='outcomes', to='hackathon.team'),
        ),
        migrations.AddField(
            model_name='roundoutcomeevent',
            name='decided_by',
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', related_query_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='roundoutcomeevent',
            name='pgh_context',
            field=models.ForeignKey(db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='pghistory.context'),
        ),
        migrations.AddField(
            model_name='roundoutcomeevent',
            name='pgh_obj',
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='events', to='hackathon.roundoutcome'),
        ),
        migrations.AddField(
            model_name='roundoutcomeevent',
            name='round',
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', related_query_name='+', to='hackathon.round'),
        ),
        migrations.AddField(
            model_name='roundoutcomeevent',
            name='team',
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', related_query_name='+', to='hackathon.team'),
        ),
        migrations.AddField(
            model_name='rubricsettings',
            name='event',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='rubric_settings', to='events.event'),
        ),
        migrations.AddConstraint(
            model_name='roundoutcome',
            constraint=models.UniqueConstraint(fields=('round', 'team'), name='one_outcome_per_team_round'),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='roundoutcome',
            trigger=pgtrigger.compiler.Trigger(name='insert_insert', sql=pgtrigger.compiler.UpsertTriggerSql(func='INSERT INTO "hackathon_roundoutcomeevent" ("advanced", "decided_at", "decided_by_id", "id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "round_id", "team_id") VALUES (NEW."advanced", NEW."decided_at", NEW."decided_by_id", NEW."id", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."round_id", NEW."team_id"); RETURN NULL;', hash='7324b0cd6d4f2922395b111ec04477aa490b1cc6', operation='INSERT', pgid='pgtrigger_insert_insert_9012a', table='hackathon_roundoutcome', when='AFTER')),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name='roundoutcome',
            trigger=pgtrigger.compiler.Trigger(name='update_update', sql=pgtrigger.compiler.UpsertTriggerSql(condition='WHEN (OLD.* IS DISTINCT FROM NEW.*)', func='INSERT INTO "hackathon_roundoutcomeevent" ("advanced", "decided_at", "decided_by_id", "id", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "round_id", "team_id") VALUES (NEW."advanced", NEW."decided_at", NEW."decided_by_id", NEW."id", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."round_id", NEW."team_id"); RETURN NULL;', hash='d6b4f5ac8819f8070a88bcb7f59dd57052105f57', operation='UPDATE', pgid='pgtrigger_update_update_c3153', table='hackathon_roundoutcome', when='AFTER')),
        ),
    ]
