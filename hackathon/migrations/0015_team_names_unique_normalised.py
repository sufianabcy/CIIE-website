# Team names unique per hackathon ignoring case and extra spaces. In its own migration (its own transaction)
# so the index is built after 0014's merges are committed; Postgres refuses it while their checks are pending.

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hackathon", "0014_tidy_team_names"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="team",
            constraint=models.UniqueConstraint(
                models.F("event"),
                django.db.models.functions.text.Lower(
                    django.db.models.functions.text.Trim(
                        models.Func(
                            models.F("name"),
                            models.Value("\\s+"),
                            models.Value(" "),
                            models.Value("g"),
                            function="REGEXP_REPLACE",
                        )
                    )
                ),
                name="team_name_unique_per_event_normalised",
            ),
        ),
    ]
