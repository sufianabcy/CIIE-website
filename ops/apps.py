from django.apps import AppConfig
from django.db.models.signals import post_migrate


class OpsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ops"
    verbose_name = "Portal operations"

    def ready(self):
        from .roles import on_post_migrate

        post_migrate.connect(on_post_migrate, sender=self, dispatch_uid="ops_ensure_roles")
