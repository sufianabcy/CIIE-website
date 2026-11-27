from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hackathon', '0010_email_and_release'),
    ]

    operations = [
        migrations.AddField(
            model_name='importupload',
            name='constants',
            field=models.JSONField(blank=True, default=dict, help_text='Fields not in the sheet, with one value for every row.'),
        ),
    ]
