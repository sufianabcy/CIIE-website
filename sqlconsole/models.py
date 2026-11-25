from django.conf import settings
from django.db import models


class SqlRun(models.Model):
    """One run of the SQL console: kept as a log of who ran what."""

    class Mode(models.TextChoices):
        READ = "read", "Read"
        DRY_RUN = "dry_run", "Write: dry run (rolled back)"
        WRITE = "write", "Write: committed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.READ)
    query = models.TextField()
    rows = models.IntegerField(null=True, blank=True, help_text="Rows returned (read) or affected (write).")
    committed = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "SQL console"
        verbose_name_plural = "SQL console"

    def __str__(self):
        return f"{self.get_mode_display()} by {self.user} at {self.created_at:%d %b %H:%M}"
