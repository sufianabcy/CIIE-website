"""Faculty judges, which registrations each one marks, the rubric, and the marks.

Faculty do not get portal accounts. An admin generates a temporary login ID and
access code for each one; the code is stored hashed and stops working when the
event's marking window closes or the admin regenerates it.
"""

import secrets
from decimal import Decimal

import pghistory
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

# Same unambiguous alphabet as pass codes: no 0/O, 1/I/L or U.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_FAILED_SIGNINS = 10


def new_code(length: int) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


@pghistory.track(exclude=["access_code_hash", "failed_signins", "last_signin_at"])
class Judge(models.Model):
    """A faculty member who marks registrations for one event."""

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="judges")
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    employee_id = models.CharField("employee ID", max_length=50, blank=True)
    department = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    extra = models.JSONField(
        "other columns from the upload", default=dict, blank=True,
        help_text="Any columns in the faculty Excel that the portal does not use yet.",
    )
    is_active = models.BooleanField("can sign in", default=True)

    login_id = models.CharField(max_length=20, unique=True, null=True, blank=True, editable=False)
    access_code_hash = models.CharField(max_length=200, blank=True, editable=False)
    credentials_issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    credentials_expire_at = models.DateTimeField(
        null=True, blank=True,
        help_text="After this the access code stops working. Set when credentials are generated.",
    )
    failed_signins = models.PositiveSmallIntegerField(default=0, editable=False)
    last_signin_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event", "name"]
        verbose_name = "faculty judge"
        constraints = [
            models.UniqueConstraint(
                "event", Lower("email"), condition=~Q(email=""), name="judge_email_unique_per_event"
            ),
            models.UniqueConstraint(
                fields=["event", "employee_id"], condition=~Q(employee_id=""), name="judge_employee_id_unique_per_event"
            ),
        ]

    def __str__(self):
        return self.name

    def issue_credentials(self, expires_at) -> str:
        """Give this judge a new login ID (kept if they have one) and a new access code. Returns the code."""
        if not self.login_id:
            while True:
                candidate = "F" + new_code(6)
                if not Judge.objects.filter(login_id=candidate).exists():
                    break
            self.login_id = candidate
        code = new_code(8)
        self.access_code_hash = make_password(code)
        self.credentials_issued_at = timezone.now()
        self.credentials_expire_at = expires_at
        self.failed_signins = 0
        self.save()
        return code

    def revoke_credentials(self):
        self.access_code_hash = ""
        self.save(update_fields=["access_code_hash"])

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_code_hash)

    def can_sign_in(self) -> bool:
        if not (self.is_active and self.access_code_hash):
            return False
        if self.failed_signins >= MAX_FAILED_SIGNINS:
            return False
        return not (self.credentials_expire_at and timezone.now() > self.credentials_expire_at)

    def check_code(self, code: str) -> bool:
        code = "".join(ch for ch in (code or "").upper() if ch.isalnum())
        return bool(self.access_code_hash) and check_password(code, self.access_code_hash)


@pghistory.track()
class Criterion(models.Model):
    """One rubric row, for example "Innovation, out of 10"."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="criteria")
    label = models.CharField(max_length=200)
    description = models.CharField(
        max_length=500, blank=True, help_text="What the faculty should look for. Shown next to the marks box."
    )
    max_marks = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal("0.5"))])
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "rubric criterion"
        verbose_name_plural = "rubric criteria"
        constraints = [
            models.CheckConstraint(condition=Q(max_marks__gt=0), name="criterion_max_marks_positive"),
        ]

    def __str__(self):
        return f"{self.label} (out of {self.max_marks.normalize():f})"

    def clean(self):
        if self.pk and self.max_marks is not None:
            highest = self.scores.aggregate(m=models.Max("marks"))["m"]
            if highest is not None and highest > self.max_marks:
                raise ValidationError({"max_marks": f"A faculty member already gave {highest.normalize():f}. The maximum cannot go below that."})


@pghistory.track()
class Assignment(models.Model):
    """This faculty member marks this registration."""

    judge = models.ForeignKey(Judge, on_delete=models.PROTECT, related_name="assignments")
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="judge_assignments"
    )
    remarks = models.TextField(blank=True)
    submitted_at = models.DateTimeField(
        null=True, blank=True, help_text="Once submitted, the faculty member can no longer change the marks."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["judge", "registration__created_at"]
        verbose_name = "student-faculty mapping"
        constraints = [
            models.UniqueConstraint(fields=["judge", "registration"], name="assignment_unique_pair"),
        ]

    def __str__(self):
        return f"{self.registration.person} → {self.judge}"

    def clean(self):
        if self.judge_id and self.registration_id and self.judge.event_id != self.registration.event_id:
            raise ValidationError("The faculty member and the registration belong to different events.")

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
            models.UniqueConstraint(fields=["assignment", "criterion"], name="score_unique_per_criterion"),
            models.CheckConstraint(condition=Q(marks__gte=0), name="score_not_negative"),
        ]

    def __str__(self):
        return f"{self.criterion.label}: {self.marks}"

    def clean(self):
        if self.criterion_id and self.marks is not None and self.marks > self.criterion.max_marks:
            raise ValidationError({"marks": f"At most {self.criterion.max_marks.normalize():f}."})
