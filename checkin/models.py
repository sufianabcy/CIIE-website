import pghistory
import pgtrigger
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


@pghistory.track()
class CheckIn(models.Model):
    """One person let in at one event. Rows are never edited; an undo is its own stamp."""

    class Method(models.TextChoices):
        SCAN = "scan", "Scanned"
        SEARCH = "search", "Found by search"
        WALK_IN = "walk_in", "Walk-in"
        MANUAL_ADD = "manual_add", "Manual add"
        BACKFILL = "backfill", "Outage backfill"

    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="checkins"
    )
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="checkins")
    method = models.CharField(max_length=20, choices=Method.choices)
    gate = models.CharField(max_length=30, blank=True)
    checked_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    checked_in_at = models.DateTimeField(default=timezone.now, help_text="When the person came in.")
    recorded_at = models.DateTimeField(default=timezone.now, help_text="When the row was written.")
    note = models.CharField(max_length=200, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    undo_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-checked_in_at"]
        verbose_name = "check-in"
        constraints = [
            models.UniqueConstraint(
                fields=["registration"],
                condition=Q(undone_at__isnull=True),
                name="one_active_checkin_per_registration",
            ),
            models.CheckConstraint(
                condition=Q(undone_at__isnull=True) | Q(undone_by__isnull=False),
                name="checkin_undo_says_who",
            ),
        ]
        indexes = [models.Index(fields=["event", "undone_at"], name="checkin_event_active_idx")]
        triggers = [
            pgtrigger.ReadOnly(
                name="checkin_rows_never_edited",
                exclude=["undone_at", "undone_by", "undo_reason"],
            ),
            pgtrigger.Protect(
                name="checkin_undo_is_final",
                operation=pgtrigger.Update,
                condition=pgtrigger.Q(old__undone_at__isnull=False),
            ),
            pgtrigger.Protect(name="checkin_never_deleted", operation=pgtrigger.Delete),
        ]

    def __str__(self):
        return f"{self.registration.person} at {self.event} ({self.get_method_display()})"

    @property
    def is_active(self) -> bool:
        return self.undone_at is None


class ScanLog(models.Model):
    """Every attempt at the door, including duplicates and "not found". Append-only."""

    class Outcome(models.TextChoices):
        CHECKED_IN = "checked_in", "Checked in"
        ALREADY = "already", "Already checked in"
        NOT_FOUND = "not_found", "Not found"
        WRONG_EVENT = "wrong_event", "Pass for another event"
        CANCELLED = "cancelled", "Registration cancelled"
        SEARCH = "search", "Search"

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="scan_logs")
    raw_input = models.CharField(max_length=200)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    registration = models.ForeignKey(
        "registrations.Registration", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    gate = models.CharField(max_length=30, blank=True)
    device = models.CharField(max_length=200, blank=True)
    scanned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        triggers = [
            pgtrigger.Protect(
                name="scan_log_append_only", operation=pgtrigger.Update | pgtrigger.Delete
            ),
        ]

    def __str__(self):
        return f"{self.get_outcome_display()}: {self.raw_input}"
