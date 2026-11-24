import pghistory
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


@pghistory.track()
class Result(models.Model):
    """A position or title at an event. A second member checks it before it is published."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CHECKED = "checked", "Checked"
        PUBLISHED = "published", "Published"

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="results")
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="results"
    )
    position = models.CharField(max_length=100, help_text="For example Winner, Runner-up or Best pitch.")
    rank = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Sort order; 1 comes first.")
    note = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    drafted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event", "rank", "position"]
        constraints = [
            models.UniqueConstraint(fields=["event", "registration", "position"], name="result_unique_per_position"),
            models.CheckConstraint(
                condition=Q(checked_by__isnull=True) | ~Q(checked_by=F("drafted_by")),
                name="result_checker_differs_from_drafter",
            ),
            models.CheckConstraint(
                condition=Q(status="draft") | Q(checked_by__isnull=False),
                name="result_checked_before_publishing",
            ),
        ]

    def __str__(self):
        return f"{self.position}: {self.registration.person}"

    def clean(self):
        if self.registration_id and self.event_id and self.registration.event_id != self.event_id:
            raise ValidationError({"registration": "This registration belongs to another event."})
