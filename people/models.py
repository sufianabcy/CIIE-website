import re

import pghistory
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone


def normalise_email(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    return email or None


def is_kl_email(email: str | None) -> bool:
    email = normalise_email(email)
    if not email or "@" not in email:
        return False
    return email.rsplit("@", 1)[1] in settings.KL_EMAIL_DOMAINS


def student_id_from_kl_email(email: str | None) -> str:
    """The ID part of a KL address, if it looks like a student ID (to verify)."""
    if not is_kl_email(email):
        return ""
    local = normalise_email(email).split("@", 1)[0]
    return local if re.match(settings.KL_STUDENT_ID_PATTERN, local) else ""


class PersonManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username)

    def create_user(self, email=None, password=None, **extra):
        person = self.model(email=normalise_email(email), **extra)
        if password:
            person.set_password(password)
        else:
            person.set_unusable_password()
        person.save(using=self._db)
        return person

    def create_superuser(self, email, password=None, **extra):
        from allauth.account.models import EmailAddress

        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("source", Person.Source.IMPORT)
        person = self.create_user(email, password, **extra)
        # Sign-in is by emailed code; this row lets the first code verify the address.
        EmailAddress.objects.get_or_create(user=person, email=person.email, defaults={"primary": True})
        return person


@pghistory.track(exclude=["password", "last_login", "failed_password_signins"])
class Person(AbstractBaseUser, PermissionsMixin):
    """One row per human, however they signed in (brief B6).

    Walk-ins are people without a login. People who leave are deactivated,
    never deleted, so "added by" stamps keep pointing at someone.
    """

    class Kind(models.TextChoices):
        STUDENT = "student", "Student"
        FACULTY = "faculty", "Faculty"

    class Source(models.TextChoices):
        SIGNUP = "signup", "Signed up"
        WALK_IN = "walk_in", "Walk-in at the door"
        IMPORT = "import", "Imported"

    email = models.EmailField("email", null=True, blank=True)
    full_name = models.CharField("name for certificates", max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True, help_text="Stored as text.")
    institution = models.CharField(max_length=200, blank=True)
    branch = models.CharField(max_length=100, blank=True)
    year_of_study = models.CharField("year", max_length=20, blank=True)

    employee_id = models.CharField(
        "employee ID", max_length=50, blank=True, help_text="For faculty. Used to match faculty in Excel uploads."
    )

    # Staff (faculty and supervisors) can also sign in with a password. Students never need one.
    login_id = models.CharField(
        "login ID", max_length=20, null=True, blank=True, unique=True,
        help_text="Short ID for password sign-in, for people without an email. Made when a temporary password is issued.",
    )
    must_change_password = models.BooleanField(
        default=False, help_text="On next sign-in they must choose their own password before anything else."
    )
    temp_password_expires_at = models.DateTimeField(
        "temporary password expires", null=True, blank=True,
        help_text="A temporary password stops working after this, unless they have already changed it.",
    )
    failed_password_signins = models.PositiveSmallIntegerField(default=0, editable=False)

    kl_email = models.EmailField("verified KL email", null=True, blank=True)
    contact_email = models.EmailField(
        blank=True, help_text="Another address from an upload. Sign-in is always with the main (KL) email."
    )
    student_id = models.CharField("KL student ID", max_length=20, blank=True)
    kl_verified_at = models.DateTimeField("KL status verified", null=True, blank=True)

    source = models.CharField(max_length=20, choices=Source.choices, default=Source.SIGNUP)
    kind = models.CharField(
        "type", max_length=20, choices=Kind.choices, default=Kind.STUDENT,
        help_text="Students and faculty have different details. Coordinator and Admin are roles on top of this.",
    )
    profile_completed_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField("can open the admin", default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = PersonManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = "person"
        verbose_name_plural = "people"
        constraints = [
            models.UniqueConstraint(Lower("email"), name="person_email_unique_ci"),
            models.UniqueConstraint(
                Lower("kl_email"), condition=Q(kl_email__isnull=False), name="person_kl_email_unique_ci"
            ),
            models.UniqueConstraint(
                fields=["student_id"], condition=~Q(student_id=""), name="person_student_id_unique"
            ),
            models.UniqueConstraint(
                Lower("employee_id"), condition=~Q(employee_id=""), name="person_employee_id_unique_ci"
            ),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        self.email = normalise_email(self.email)
        self.kl_email = normalise_email(self.kl_email)
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        return self.full_name or self.email or self.student_id or f"Person {self.pk}"

    @property
    def is_kl_verified(self) -> bool:
        return self.kl_verified_at is not None

    @property
    def profile_complete(self) -> bool:
        return self.profile_completed_at is not None

    def mark_kl_verified(self, kl_email: str) -> None:
        """Record that this person proved they own a KL address."""
        kl_email = normalise_email(kl_email)
        if not is_kl_email(kl_email):
            return
        changed = False
        if self.kl_email != kl_email:
            self.kl_email = kl_email
            changed = True
        sid = student_id_from_kl_email(kl_email)
        if sid and not self.student_id:
            self.student_id = sid
            changed = True
        if not self.kl_verified_at:
            self.kl_verified_at = timezone.now()
            changed = True
        if changed:
            self.save(update_fields=["kl_email", "student_id", "kl_verified_at"])

    def in_group(self, name: str) -> bool:
        return self.is_superuser or self.groups.filter(name=name).exists()

    @property
    def is_coordinator(self) -> bool:
        return self.is_superuser or self.groups.filter(name__in=["Coordinator", "Admin"]).exists()

    @property
    def is_portal_admin(self) -> bool:
        return self.in_group("Admin")

    @property
    def is_faculty_type(self) -> bool:
        return self.kind == self.Kind.FACULTY

    @property
    def is_faculty(self) -> bool:
        return self.in_group("Faculty")

    @property
    def is_supervisor(self) -> bool:
        return self.in_group("Supervisor")
