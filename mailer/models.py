from django.conf import settings
from django.db import models
from django.utils import timezone


class OutboxEmail(models.Model):
    """Every email the portal sends goes through this queue.

    Codes go first (priority 1), then confirmations (2), then everything
    else (3). A daily cap always keeps room for codes.
    """

    class Kind(models.TextChoices):
        CODE = "code", "Sign-in or verification code"
        LOGIN = "login", "Login details (temporary password)"
        CONFIRMATION = "confirmation", "Registration confirmation"
        DOOR_LIST = "door_list", "Door list"
        REMINDER = "reminder", "Reminder"
        RESULT = "result", "Results"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    to_email = models.EmailField()
    subject = models.CharField(max_length=255)
    text_body = models.TextField()
    html_body = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    priority = models.PositiveSmallIntegerField(default=3)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    dedupe_key = models.CharField(max_length=120, null=True, blank=True, unique=True)
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="emails",
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "email"
        verbose_name_plural = "email outbox"
        indexes = [
            models.Index(fields=["status", "priority", "next_attempt_at"], name="outbox_due_idx"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} to {self.to_email} ({self.status})"
