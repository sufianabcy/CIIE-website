import secrets

import pghistory
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

# No 0/O, 1/I/L or U, so a code read aloud or typed from a screenshot is unambiguous.
PASS_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
PASS_LENGTH = 8


def new_pass_code() -> str:
    return "".join(secrets.choice(PASS_ALPHABET) for _ in range(PASS_LENGTH))


def normalise_pass_code(text: str) -> str | None:
    """Turn whatever was scanned or typed into a pass code, if it looks like one."""
    cleaned = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if len(cleaned) == PASS_LENGTH and all(ch in PASS_ALPHABET for ch in cleaned):
        return cleaned
    return None


def format_pass_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


@pghistory.track()
class Registration(models.Model):
    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        ONLINE = "online", "Registered online"
        WALK_IN = "walk_in", "Walk-in at the door"
        MANUAL = "manual", "Added at the door (said they registered)"
        IMPORT = "import", "Imported"

    # Walk-ins and door additions never take an online seat.
    SEAT_SOURCES = {Source.ONLINE, Source.IMPORT}

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="registrations")
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="registrations"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONFIRMED)
    pass_code = models.CharField(max_length=PASS_LENGTH, unique=True, default=new_pass_code, editable=False)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.ONLINE)
    answers = models.JSONField(default=dict, blank=True)
    form_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["event", "person"], name="one_registration_per_person_per_event"),
        ]
        indexes = [models.Index(fields=["event", "status"], name="registration_event_status_idx")]

    def __str__(self):
        return f"{self.person} at {self.event}"

    @property
    def pass_code_display(self) -> str:
        return format_pass_code(self.pass_code)

    @property
    def takes_seat(self) -> bool:
        return self.source in self.SEAT_SOURCES

    @property
    def is_confirmed(self) -> bool:
        return self.status == self.Status.CONFIRMED

    def get_pass_url(self):
        return reverse("pass", args=[self.pass_code])

    def get_home_url(self):
        """Where this registration lives for the student: the pass, or for hackathons (no passes) My hackathon."""
        if self.event.kind == "hackathon":
            return reverse("my_hackathon", args=[self.event.slug])
        return self.get_pass_url()

    def active_checkin(self):
        return self.checkins.filter(undone_at__isnull=True).first()
