from django.db import migrations, models


import re


def recompute(apps, schema_editor):
    """Block and floor from the room number, with the rule: letters, floor digits, last two digits = room."""
    Room = apps.get_model("hackathon", "Room")
    for room in Room.objects.all():
        code = re.sub(r"[\s-]+", "", room.name or "").upper()
        m = re.match(r"^([A-Z]+)(\d+)(\d\d)$", code)
        if m:
            room.block, room.floor, room.code_unread = m.group(1), str(int(m.group(2))), False
            if not Room.objects.filter(event_id=room.event_id, name__iexact=code).exclude(pk=room.pk).exists():
                room.name = code
            room.save(update_fields=["name", "block", "floor", "code_unread"])


class Migration(migrations.Migration):

    dependencies = [
        ('hackathon', '0008_team_leader_and_sheet_choice'),
    ]

    operations = [
        migrations.AlterField(
            model_name='room',
            name='capacity',
            field=models.PositiveIntegerField(blank=True, help_text='How many teams fit. Used by the automatic room assignment.', null=True, verbose_name='max capacity (teams)'),
        ),
        migrations.AlterField(
            model_name='room',
            name='name',
            field=models.CharField(help_text='For example L502: block L, floor 5, room 02.', max_length=100, verbose_name='room number'),
        ),
        migrations.AlterField(
            model_name='roomevent',
            name='capacity',
            field=models.PositiveIntegerField(blank=True, help_text='How many teams fit. Used by the automatic room assignment.', null=True, verbose_name='max capacity (teams)'),
        ),
        migrations.AlterField(
            model_name='roomevent',
            name='name',
            field=models.CharField(help_text='For example L502: block L, floor 5, room 02.', max_length=100, verbose_name='room number'),
        ),
        migrations.RunPython(recompute, migrations.RunPython.noop),
    ]
