from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hackathon", "0017_team_number_category_rubric_file"),
    ]

    operations = [
        migrations.AddField(
            model_name="rubricsettings",
            name="eliminate_teams",
            field=models.BooleanField(
                default=False,
                help_text="Off: every team goes through every round. On: after each round an admin confirms which teams go on, using the round's advance rule; the others stop.",
                verbose_name="teams are eliminated between rounds",
            ),
        ),
    ]
