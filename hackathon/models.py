"""Hackathons: who judges and supervises, rooms, teams, rounds, rubrics, allocations and marks.

Faculty and supervisors are ordinary portal accounts (people.Person) linked to
one event here. Students are registrations, grouped into teams; a solo
participant is a team of one.
"""

from decimal import Decimal

import pghistory
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone


@pghistory.track()
class EventFaculty(models.Model):
    """A faculty member who judges (and may mentor) at one hackathon."""

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="faculty")
    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="faculty_roles")
    extra = models.JSONField(
        "other columns from the upload", default=dict, blank=True,
        help_text="Columns in the faculty Excel that the portal does not use yet.",
    )
    is_active = models.BooleanField("can judge", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event", "person__full_name"]
        verbose_name = "event faculty"
        verbose_name_plural = "event faculty"
        constraints = [models.UniqueConstraint(fields=["event", "person"], name="event_faculty_unique")]

    def __str__(self):
        return self.person.display_name


@pghistory.track()
class Supervisor(models.Model):
    """Oversees one hackathon: sees everything for it and edits details, but not marks."""

    class Kind(models.TextChoices):
        FACULTY = "faculty", "Faculty"
        STUDENT = "student", "Student"

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="supervisors")
    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="supervisor_roles")
    kind = models.CharField("type", max_length=20, choices=Kind.choices, default=Kind.FACULTY)
    extra = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event", "person__full_name"]
        verbose_name = "hackathon supervisor"
        constraints = [models.UniqueConstraint(fields=["event", "person"], name="supervisor_unique")]

    def __str__(self):
        return f"{self.person.display_name} ({self.get_kind_display()})"


@pghistory.track()
class Room(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="rooms")
    name = models.CharField(max_length=100)
    building = models.CharField("building / floor", max_length=150, blank=True)
    capacity = models.PositiveIntegerField("capacity (teams)", null=True, blank=True)

    class Meta:
        ordering = ["event", "name"]
        constraints = [models.UniqueConstraint("event", Lower("name"), name="room_name_unique_per_event")]

    def __str__(self):
        return f"{self.name} ({self.building})" if self.building else self.name


@pghistory.track()
class Team(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="teams")
    name = models.CharField(max_length=150)
    project_title = models.CharField(max_length=200, blank=True)
    project_description = models.TextField("project description / problem statement", blank=True)
    room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams")
    mentor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="mentored_teams",
        help_text="A faculty member of this event. They can never be allocated to judge this team.",
    )
    mentor_name = models.CharField(
        max_length=150, blank=True, help_text="For a mentor who has no account, as written in the upload."
    )
    extra = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event", "name"]
        constraints = [models.UniqueConstraint("event", Lower("name"), name="team_name_unique_per_event")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.room_id and self.event_id and self.room.event_id != self.event_id:
            raise ValidationError({"room": "That room belongs to another event."})

    @property
    def mentor_display(self) -> str:
        return self.mentor.display_name if self.mentor else self.mentor_name


@pghistory.track()
class TeamMember(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
    # One registration is one person at one event, so this keeps a person in at most one team per event.
    registration = models.OneToOneField(
        "registrations.Registration", on_delete=models.PROTECT, related_name="team_membership"
    )
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["team", "registration__person__full_name"]
        verbose_name = "team member"

    def __str__(self):
        return f"{self.registration.person} in {self.team}"

    def clean(self):
        if self.team_id and self.registration_id and self.team.event_id != self.registration.event_id:
            raise ValidationError("The team and the registration belong to different events.")


@pghistory.track()
class Round(models.Model):
    """A judging round. Its rubric can be marked only while it is open."""

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="rounds")
    name = models.CharField(max_length=100)
    position = models.PositiveSmallIntegerField("order", default=1)
    opens_at = models.DateTimeField(null=True, blank=True, help_text="Marking opens. Empty: closed.")
    closes_at = models.DateTimeField(null=True, blank=True, help_text="Marking closes. Empty: stays open once opened.")
    results_published_at = models.DateTimeField(
        null=True, blank=True, help_text="When set, each team can see its own marks for this round."
    )

    class Meta:
        ordering = ["event", "position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "position"], name="round_position_unique_per_event"),
            models.CheckConstraint(
                condition=Q(opens_at__isnull=True) | Q(closes_at__isnull=True) | Q(closes_at__gt=models.F("opens_at")),
                name="round_closes_after_opens",
            ),
        ]

    def __str__(self):
        return self.name

    def is_open(self, now=None) -> bool:
        now = now or timezone.now()
        if not self.opens_at or now < self.opens_at:
            return False
        return not self.closes_at or now < self.closes_at

    @property
    def state(self) -> str:
        now = timezone.now()
        if self.is_open(now):
            return "open"
        if self.opens_at and now < self.opens_at:
            return "upcoming"
        return "closed" if self.opens_at else "not scheduled"

    @property
    def out_of(self) -> Decimal:
        return sum((c.max_marks for c in self.criteria.all()), Decimal("0"))


@pghistory.track()
class Criterion(models.Model):
    """One rubric row of a round, for example "Innovation, out of 10"."""

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="criteria")
    label = models.CharField(max_length=200)
    description = models.CharField(
        "guidance", max_length=500, blank=True, help_text="What faculty should look for. Shown next to the marks box."
    )
    max_marks = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal("0.5"))])
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["round", "position", "id"]
        verbose_name = "rubric criterion"
        verbose_name_plural = "rubric criteria"
        constraints = [models.CheckConstraint(condition=Q(max_marks__gt=0), name="hackathon_criterion_max_positive")]

    def __str__(self):
        return f"{self.label} (out of {self.max_marks.normalize():f})"

    def clean(self):
        if self.pk and self.max_marks is not None:
            highest = self.scores.aggregate(m=models.Max("marks"))["m"]
            if highest is not None and highest > self.max_marks:
                raise ValidationError({"max_marks": f"A faculty member already gave {highest.normalize():f}. The maximum cannot go below that."})


def room_conflict(round_id, faculty_id, team, exclude_pk=None) -> str:
    """Why this faculty member can't take this team in this round under the one-room rule, or ""."""
    others = (
        Assignment.objects.filter(round_id=round_id, faculty_id=faculty_id)
        .exclude(pk=exclude_pk)
        .exclude(team__room_id=team.room_id)
        .select_related("team__room")
        .first()
    )
    if not others:
        return ""
    where = others.team.room.name if others.team.room else "no room"
    here = team.room.name if team.room else "no room"
    return f"In this round they already judge teams in {where}; one faculty member judges one room per round, and {team.name} is in {here}."


@pghistory.track()
class Assignment(models.Model):
    """This faculty member judges this team in this round."""

    class Source(models.TextChoices):
        WEBSITE = "website", "Allocated on the website"
        IMPORT = "import", "Imported from Excel"

    round = models.ForeignKey(Round, on_delete=models.PROTECT, related_name="assignments")
    faculty = models.ForeignKey(EventFaculty, on_delete=models.PROTECT, related_name="assignments")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="assignments")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.WEBSITE)
    remarks = models.TextField(blank=True)
    submitted_at = models.DateTimeField(
        null=True, blank=True, help_text="Once submitted, the faculty member can no longer change the marks."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["round", "faculty", "team__name"]
        verbose_name = "allocation"
        constraints = [
            models.UniqueConstraint(fields=["round", "faculty", "team"], name="allocation_unique"),
        ]

    def __str__(self):
        return f"{self.round}: {self.faculty} judges {self.team}"

    def clean(self):
        if not (self.round_id and self.faculty_id and self.team_id):
            return
        event_ids = {self.round.event_id, self.faculty.event_id, self.team.event_id}
        if len(event_ids) > 1:
            raise ValidationError("The round, faculty member and team must all belong to the same event.")
        if self.team.mentor_id and self.team.mentor_id == self.faculty.person_id:
            raise ValidationError(f"{self.faculty} mentors {self.team.name}, so they can't judge it.")
        if self.source == self.Source.WEBSITE:
            problem = room_conflict(self.round_id, self.faculty_id, self.team, exclude_pk=self.pk)
            if problem:
                raise ValidationError(problem)

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None

    def total(self):
        return sum((s.marks for s in self.scores.all()), Decimal("0"))


@pghistory.track()
class Score(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="scores")
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="scores")
    marks = models.DecimalField(max_digits=6, decimal_places=2)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assignment", "criterion"], name="hackathon_score_unique"),
            models.CheckConstraint(condition=Q(marks__gte=0), name="hackathon_score_not_negative"),
        ]

    def __str__(self):
        return f"{self.criterion.label}: {self.marks}"

    def clean(self):
        if self.criterion_id and self.assignment_id and self.criterion.round_id != self.assignment.round_id:
            raise ValidationError("That criterion belongs to another round.")
        if self.criterion_id and self.marks is not None and self.marks > self.criterion.max_marks:
            raise ValidationError({"marks": f"At most {self.criterion.max_marks.normalize():f}."})


@pghistory.track()
class FacultyRequest(models.Model):
    """Someone who signed up asks to be faculty. They get nothing until an admin approves."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for an admin"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="faculty_requests")
    full_name = models.CharField("name", max_length=150)
    employee_id = models.CharField("employee ID", max_length=50)
    department = models.CharField(max_length=150)
    designation = models.CharField(max_length=100, help_text="For example Assistant Professor.")
    phone = models.CharField(max_length=20)
    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL, related_name="faculty_requests",
        verbose_name="hackathon to judge", help_text="Optional. Approving adds them as faculty there.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    note = models.CharField("note to them", max_length=500, blank=True, help_text="Sent in the email when you decide.")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["status", "-created_at"]
        verbose_name = "faculty request"
        constraints = [
            models.UniqueConstraint(
                fields=["person"], condition=Q(status="pending"), name="one_pending_faculty_request_per_person"
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.get_status_display()})"

    def duplicates(self):
        """Other accounts that already look like this faculty member (same email or employee ID)."""
        from people.models import Person

        q = Q(employee_id__iexact=self.employee_id) if self.employee_id else Q(pk__in=[])
        for email in {e for e in (self.person.email, self.person.kl_email) if e}:
            q |= Q(email__iexact=email) | Q(kl_email__iexact=email)
        return Person.objects.filter(q).exclude(pk=self.person_id).distinct()
