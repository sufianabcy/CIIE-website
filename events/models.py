import secrets

import pghistory
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone


CLOSED_TO_REGISTRATION = "Participants are added by the organisers."


class EventQuerySet(models.QuerySet):
    def public(self):
        return self.filter(status=Event.Status.PUBLISHED, is_practice=False)

    def upcoming(self):
        return self.filter(ends_at__gte=timezone.now()).order_by("starts_at")

    def completed(self):
        return self.filter(ends_at__lt=timezone.now()).order_by("-starts_at")

    def coordinated_by(self, person):
        if person.is_portal_admin:
            return self
        return self.filter(coordinators=person)


def new_door_token() -> str:
    return secrets.token_urlsafe(16)


@pghistory.track(exclude=["seats_taken", "updated_at"])
class Event(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft (hidden)"
        PUBLISHED = "published", "Published"
        CANCELLED = "cancelled", "Cancelled"

    class Kind(models.TextChoices):
        NORMAL = "normal", "Normal event"
        HACKATHON = "hackathon", "Hackathon (teams, rooms, rounds, faculty marking)"

    class ReportStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        READY = "ready", "Ready for sign-off"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=200)
    slug = models.SlugField(
        "short link",
        max_length=80,
        unique=True,
        help_text="The event's own link: /e/<short link>/. Do not change it after posters go out.",
    )
    summary = models.CharField(max_length=300, blank=True, help_text="One line for the event card.")
    description = models.TextField(blank=True)
    venue = models.CharField(max_length=200, blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    registration_opens_at = models.DateTimeField(null=True, blank=True)
    registration_closes_at = models.DateTimeField(
        null=True, blank=True, help_text="Leave empty to keep registration open until the event ends."
    )
    seats = models.PositiveIntegerField(null=True, blank=True, help_text="Leave empty for no limit.")
    seats_taken = models.PositiveIntegerField(default=0, editable=False)
    kl_only = models.BooleanField("KL students only", default=False)
    earns_points = models.BooleanField("earns SIL points", default=False)
    points_note = models.CharField(max_length=200, blank=True)
    is_practice = models.BooleanField(
        "practice event",
        default=False,
        help_text="For rehearsals and training. Hidden from the public list and kept out of reports.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.NORMAL)
    registration_enabled = models.BooleanField(
        "open for registration", default=True,
        help_text="Off: nobody can register on the site; organisers add participants (Excel, Add person, the hackathon page).",
    )
    team_size_min = models.PositiveSmallIntegerField(
        "smallest team", default=1, help_text="Hackathons: fewest members a team may register with (1 allows solo entries)."
    )
    team_size_max = models.PositiveSmallIntegerField(
        "largest team", default=6, help_text="Hackathons: most members in a team, including the leader."
    )
    show_jury_to_students = models.BooleanField(
        default=False, help_text="Hackathons: let each team see which faculty are judging them."
    )
    coordinators = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="coordinated_events"
    )
    door_team = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="door_events",
        help_text="Volunteers who joined through the door-team link. Their scan rights end with the event.",
    )
    door_token = models.CharField(max_length=40, default=new_door_token, editable=False)
    form_version = models.PositiveIntegerField(default=1, editable=False)

    # Report fields, held as data so the report can only use what was collected.
    unit = models.CharField("organising unit", max_length=120, blank=True)
    academic_year = models.CharField(max_length=20, blank=True, help_text="For example 2026-27.")
    faculty_in_charge_name = models.CharField(max_length=150, blank=True)
    faculty_in_charge_id = models.CharField("faculty in charge ID", max_length=50, blank=True)
    student_lead_name = models.CharField(max_length=150, blank=True)
    student_lead_id = models.CharField("student lead ID", max_length=50, blank=True)
    time_slot = models.CharField(max_length=100, blank=True, help_text="As the report should show it.")
    report_status = models.CharField(
        max_length=20, choices=ReportStatus.choices, default=ReportStatus.DRAFT
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(seats__isnull=True) | Q(seats_taken__lte=F("seats")),
                name="event_seats_never_overbooked",
            ),
            models.CheckConstraint(condition=Q(ends_at__gte=F("starts_at")), name="event_ends_after_start"),
        ]

    def __str__(self):
        return f"{self.title} (practice)" if self.is_practice else self.title

    def get_absolute_url(self):
        return reverse("event_detail", args=[self.slug])

    def clean(self):
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValidationError({"ends_at": "The event must end after it starts."})
        if self.seats is not None and self.pk:
            taken = Event.objects.filter(pk=self.pk).values_list("seats_taken", flat=True).first() or 0
            if self.seats < taken:
                raise ValidationError({"seats": f"{taken} seats are already taken. Seats cannot go below that."})

    @property
    def seats_left(self) -> int | None:
        if self.seats is None:
            return None
        return max(self.seats - self.seats_taken, 0)

    @property
    def is_full(self) -> bool:
        return self.seats is not None and self.seats_taken >= self.seats

    @property
    def is_over(self) -> bool:
        return self.ends_at < timezone.now()

    @property
    def registration_closes(self):
        return self.registration_closes_at or self.ends_at

    def registration_state(self, site=None) -> tuple[bool, str]:
        """Whether registration is open now, and if not, why (for the page)."""
        from ops.models import SiteSettings

        site = site or SiteSettings.load()
        now = timezone.now()
        if self.status == self.Status.CANCELLED:
            return False, "This event was cancelled."
        if not self.registration_enabled:
            return False, CLOSED_TO_REGISTRATION
        if self.status != self.Status.PUBLISHED:
            return False, "Registration has not opened yet."
        if not site.registration_open:
            return False, "Registration is paused for the moment. Please try again later."
        if self.registration_opens_at and now < self.registration_opens_at:
            local = timezone.localtime(self.registration_opens_at)
            return False, f"Registration opens {local:%d %b, %I:%M %p}."
        if now > self.registration_closes:
            return False, "Registration has closed."
        if self.is_full:
            return False, "All seats are taken."
        return True, ""

    def has_answers(self) -> bool:
        return self.registrations.exists()

    def door_open_for(self, person) -> bool:
        """Coordinators always; door-team volunteers until six hours after the end."""
        if not person.is_authenticated:
            return False
        if person.is_portal_admin or self.coordinators.filter(pk=person.pk).exists():
            return True
        grace = self.ends_at + timezone.timedelta(hours=6)
        return timezone.now() <= grace and self.door_team.filter(pk=person.pk).exists()

    @property
    def is_hackathon(self) -> bool:
        return self.kind == self.Kind.HACKATHON

    def coordinated_by(self, person) -> bool:
        if not person.is_authenticated:
            return False
        return person.is_portal_admin or self.coordinators.filter(pk=person.pk).exists()


@pghistory.track()
class Question(models.Model):
    """A registration question. It locks once anyone has registered.

    After that, only typo fixes to labels, new options and new optional
    questions are allowed; anything else would break past answers.
    """

    class Kind(models.TextChoices):
        SHORT_TEXT = "short_text", "Short answer"
        LONG_TEXT = "long_text", "Paragraph"
        CHOICE = "choice", "One choice"
        MULTI_CHOICE = "multi_choice", "Several choices"
        YES_NO = "yes_no", "Yes or no"
        NUMBER = "number", "Number"
        LINK = "link", "Link"

    CHOICE_KINDS = {Kind.CHOICE, Kind.MULTI_CHOICE}

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    key = models.SlugField(
        "permanent ID",
        max_length=50,
        help_text="Used as the column name in exports. Never reused or changed.",
    )
    label = models.CharField(max_length=300)
    help_text = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SHORT_TEXT)
    options = models.JSONField(
        default=list, blank=True, help_text='For choice questions: a list such as ["CSE", "ECE"].'
    )
    required = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)
    is_hidden = models.BooleanField(
        "hidden", default=False, help_text="Hide from the form. Past answers stay in exports."
    )
    added_in_version = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="question_key_unique_per_event"),
        ]

    def __str__(self):
        return self.label

    def clean(self):
        errors = {}
        if self.kind in self.CHOICE_KINDS:
            if not isinstance(self.options, list) or not self.options:
                errors["options"] = "List the choices, for example [\"Yes\", \"No\"]."
            elif len({str(o).strip() for o in self.options}) != len(self.options):
                errors["options"] = "Each choice must be different."
        elif self.options:
            errors["options"] = "Only choice questions have options."
        errors.update(self.lock_errors())
        if errors:
            raise ValidationError(errors)

    def lock_errors(self) -> dict:
        """What this change would break, if the form is already locked."""
        if not self.event_id or not self.event.has_answers():
            return {}
        locked = "People have already registered, so"
        if not self.pk:
            if self.required:
                return {"required": f"{locked} a new question must be optional."}
            return {}
        before = Question.objects.get(pk=self.pk)
        errors = {}
        if self.key != before.key:
            errors["key"] = f"{locked} the permanent ID cannot change."
        if self.kind != before.kind:
            errors["kind"] = f"{locked} the type cannot change. Hide this question and add a new one."
        missing = [o for o in (before.options or []) if o not in (self.options or [])]
        if missing:
            errors["options"] = f"{locked} existing choices cannot be removed or renamed: {', '.join(map(str, missing))}."
        if self.required and not before.required:
            errors["required"] = f"{locked} an optional question cannot become required."
        return errors

    def save(self, *args, **kwargs):
        locked = self.event.has_answers()
        if locked and not self.pk:
            Event.objects.filter(pk=self.event_id).update(form_version=F("form_version") + 1)
            self.event.refresh_from_db(fields=["form_version"])
            self.added_in_version = self.event.form_version
        elif not self.pk:
            self.added_in_version = self.event.form_version
        elif locked:
            Event.objects.filter(pk=self.event_id).update(form_version=F("form_version") + 1)
        super().save(*args, **kwargs)
