import pghistory
from django.db import models
from django.db.models import Q


@pghistory.track()
class SiteSettings(models.Model):
    """Switches an admin can flip without touching code (one row only).

    Passwords and keys never live here; they stay in environment variables.
    """

    registration_open = models.BooleanField(
        "registration open",
        default=True,
        help_text="Turn off to stop new registrations on every event at once.",
    )
    email_paused = models.BooleanField(
        "pause non-urgent email",
        default=False,
        help_text="Holds confirmations and other mail in the queue. Sign-in codes still go out.",
    )
    signin_email_code_enabled = models.BooleanField(
        "sign-in by emailed code", default=True
    )
    signin_google_enabled = models.BooleanField(
        "sign-in with Google",
        default=True,
        help_text="Only shows when a Google client ID is configured.",
    )
    signin_microsoft_enabled = models.BooleanField(
        "sign-in with Microsoft (KL accounts)",
        default=False,
        help_text="Only shows when a Microsoft client ID and KL's tenant ID are configured.",
    )
    banner_text = models.CharField(
        max_length=300, blank=True, help_text="Shown at the top of every page when filled in."
    )
    sender_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="The name people see on emails. The address itself is a server setting.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "portal switches"
        verbose_name_plural = "portal switches"
        constraints = [models.CheckConstraint(condition=Q(pk=1), name="site_settings_single_row")]

    def __str__(self):
        return "Portal switches"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BackupRun(models.Model):
    """One row per backup attempt; feeds "last good backup" on the status page."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(null=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    destinations = models.TextField(blank=True)
    row_counts = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        state = {True: "ok", False: "failed", None: "running"}[self.ok]
        return f"Backup {self.started_at:%Y-%m-%d %H:%M} ({state})"
