from django.apps import AppConfig


class JudgingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "judging"
    verbose_name = "Old judging (replaced by Hackathons, kept for its data)"
