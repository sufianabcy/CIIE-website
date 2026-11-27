import django.db.models.deletion
import django.utils.timezone
import hackathon.models
import pgtrigger.compiler
import pgtrigger.migrations
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0006_students_form_teams"),
        ("hackathon", "0018_elimination_switch"),
        ("pghistory", "0007_auto_20250421_0444"),
        ("registrations", "0002_email_and_release"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AttendanceRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("late", "Missed: the student arrived late"),
                            ("wrong", "Marked wrongly"),
                            ("details", "Wrong details submitted"),
                        ],
                        max_length=20,
                        verbose_name="what happened",
                    ),
                ),
                (
                    "want_present",
                    models.BooleanField(
                        default=True, verbose_name="should be marked present"
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=500)),
                (
                    "requested_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Waiting for an admin"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_note", models.CharField(blank=True, max_length=500)),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attendance_requests",
                        to="registrations.registration",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["status", "-requested_at"],
            },
        ),
        migrations.CreateModel(
            name="AttendanceSession",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="For example Morning check.", max_length=100
                    ),
                ),
                (
                    "token",
                    models.CharField(
                        default=hackathon.models._session_token,
                        editable=False,
                        max_length=40,
                        unique=True,
                    ),
                ),
                ("opens_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attendance_sessions",
                        to="events.event",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attendance_sessions",
                        to="hackathon.room",
                    ),
                ),
            ],
            options={
                "ordering": ["event", "-opens_at", "room__name"],
            },
        ),
        migrations.CreateModel(
            name="AttendanceRequestEvent",
            fields=[
                ("pgh_id", models.AutoField(primary_key=True, serialize=False)),
                ("pgh_created_at", models.DateTimeField(auto_now_add=True)),
                ("pgh_label", models.TextField(help_text="The event label.")),
                ("id", models.BigIntegerField()),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("late", "Missed: the student arrived late"),
                            ("wrong", "Marked wrongly"),
                            ("details", "Wrong details submitted"),
                        ],
                        max_length=20,
                        verbose_name="what happened",
                    ),
                ),
                (
                    "want_present",
                    models.BooleanField(
                        default=True, verbose_name="should be marked present"
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=500)),
                (
                    "requested_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Waiting for an admin"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_note", models.CharField(blank=True, max_length=500)),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pgh_context",
                    models.ForeignKey(
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="pghistory.context",
                    ),
                ),
                (
                    "pgh_obj",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="events",
                        to="hackathon.attendancerequest",
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to="registrations.registration",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to="hackathon.attendancesession",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="attendancerequest",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="requests",
                to="hackathon.attendancesession",
            ),
        ),
        migrations.CreateModel(
            name="AttendanceSessionEvent",
            fields=[
                ("pgh_id", models.AutoField(primary_key=True, serialize=False)),
                ("pgh_created_at", models.DateTimeField(auto_now_add=True)),
                ("pgh_label", models.TextField(help_text="The event label.")),
                ("id", models.BigIntegerField()),
                (
                    "name",
                    models.CharField(
                        help_text="For example Morning check.", max_length=100
                    ),
                ),
                (
                    "token",
                    models.CharField(
                        default=hackathon.models._session_token,
                        editable=False,
                        max_length=40,
                    ),
                ),
                ("opens_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to="events.event",
                    ),
                ),
                (
                    "pgh_context",
                    models.ForeignKey(
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="pghistory.context",
                    ),
                ),
                (
                    "pgh_obj",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="events",
                        to="hackathon.attendancesession",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        related_query_name="+",
                        to="hackathon.room",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="SessionMark",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("present", models.BooleanField()),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("link", "Marked with the link"),
                            ("late", "Added by an admin (late)"),
                            ("correction", "Corrected by an admin"),
                        ],
                        default="link",
                        max_length=20,
                    ),
                ),
                ("marked_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "marked_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="session_marks",
                        to="registrations.registration",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="marks",
                        to="hackathon.attendancesession",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        blank=True,
                        help_text="The team they were in when marked.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="session_marks",
                        to="hackathon.team",
                    ),
                ),
            ],
            options={
                "ordering": ["session", "registration__person__full_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="attendancesession",
            constraint=models.CheckConstraint(
                condition=models.Q(("expires_at__gt", models.F("opens_at"))),
                name="attendance_session_expires_after_opens",
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name="attendancesession",
            trigger=pgtrigger.compiler.Trigger(
                name="insert_insert",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='INSERT INTO "hackathon_attendancesessionevent" ("created_at", "created_by_id", "event_id", "expires_at", "id", "name", "opens_at", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "room_id", "token") VALUES (NEW."created_at", NEW."created_by_id", NEW."event_id", NEW."expires_at", NEW."id", NEW."name", NEW."opens_at", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."room_id", NEW."token"); RETURN NULL;',
                    hash="bde427a03d7f0fd55cba05bf5706409fea4bc803",
                    operation="INSERT",
                    pgid="pgtrigger_insert_insert_b4940",
                    table="hackathon_attendancesession",
                    when="AFTER",
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name="attendancesession",
            trigger=pgtrigger.compiler.Trigger(
                name="update_update",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition="WHEN (OLD.* IS DISTINCT FROM NEW.*)",
                    func='INSERT INTO "hackathon_attendancesessionevent" ("created_at", "created_by_id", "event_id", "expires_at", "id", "name", "opens_at", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "room_id", "token") VALUES (NEW."created_at", NEW."created_by_id", NEW."event_id", NEW."expires_at", NEW."id", NEW."name", NEW."opens_at", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."room_id", NEW."token"); RETURN NULL;',
                    hash="60bd360390277e8afb327dffed3ab403f7718a63",
                    operation="UPDATE",
                    pgid="pgtrigger_update_update_91f30",
                    table="hackathon_attendancesession",
                    when="AFTER",
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name="attendancerequest",
            trigger=pgtrigger.compiler.Trigger(
                name="insert_insert",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func='INSERT INTO "hackathon_attendancerequestevent" ("decided_at", "decided_by_id", "decision_note", "id", "kind", "note", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "registration_id", "requested_at", "requested_by_id", "session_id", "status", "want_present") VALUES (NEW."decided_at", NEW."decided_by_id", NEW."decision_note", NEW."id", NEW."kind", NEW."note", _pgh_attach_context(), NOW(), \'insert\', NEW."id", NEW."registration_id", NEW."requested_at", NEW."requested_by_id", NEW."session_id", NEW."status", NEW."want_present"); RETURN NULL;',
                    hash="800a637afe2d2c1e2a9ef0fafa9d95d849bdfb3a",
                    operation="INSERT",
                    pgid="pgtrigger_insert_insert_1f2a9",
                    table="hackathon_attendancerequest",
                    when="AFTER",
                ),
            ),
        ),
        pgtrigger.migrations.AddTrigger(
            model_name="attendancerequest",
            trigger=pgtrigger.compiler.Trigger(
                name="update_update",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    condition="WHEN (OLD.* IS DISTINCT FROM NEW.*)",
                    func='INSERT INTO "hackathon_attendancerequestevent" ("decided_at", "decided_by_id", "decision_note", "id", "kind", "note", "pgh_context_id", "pgh_created_at", "pgh_label", "pgh_obj_id", "registration_id", "requested_at", "requested_by_id", "session_id", "status", "want_present") VALUES (NEW."decided_at", NEW."decided_by_id", NEW."decision_note", NEW."id", NEW."kind", NEW."note", _pgh_attach_context(), NOW(), \'update\', NEW."id", NEW."registration_id", NEW."requested_at", NEW."requested_by_id", NEW."session_id", NEW."status", NEW."want_present"); RETURN NULL;',
                    hash="ad51b6d9a72c03b765b03a7672176241cd9dfa11",
                    operation="UPDATE",
                    pgid="pgtrigger_update_update_8ff30",
                    table="hackathon_attendancerequest",
                    when="AFTER",
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionmark",
            constraint=models.UniqueConstraint(
                fields=("session", "registration"),
                name="one_mark_per_student_per_session",
            ),
        ),
    ]
