"""Hackathons: who judges and supervises, rooms, teams, rounds, rubrics, allocations and marks.

Faculty and supervisors are ordinary portal accounts (people.Person) linked to
one event here. Students are registrations, grouped into teams; a student who
isn't in a team yet is listed under "Not in a team" (never put in a made-up team).
"""

from decimal import Decimal

import re

import pghistory
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Func, Q, Value
from django.db.models.functions import Lower, Trim
from django.utils import timezone


def clean_team_name(name) -> str:
    """A team name as stored: no spaces at the ends and single spaces inside. Compared ignoring case."""
    return " ".join(str(name or "").split())


def clean_team_number(value) -> str:
    """A team number as stored: trimmed, and "7.0" (an Excel number cell read as text) becomes "7"."""
    text = " ".join(str(value if value is not None else "").split())
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text


def team_number_order(team) -> tuple:
    """Sort key: numbered teams first, by number (2 before 10), then the rest by name."""
    number = team.number or ""
    return (not number, int(number) if number.isdigit() else float("inf"), number.lower(), team.name.lower())


# The same normalisation in SQL, so the database itself refuses "Team  A" next to "team a".
TEAM_NAME_KEY = Lower(Trim(Func(F("name"), Value(r"\s+"), Value(" "), Value("g"), function="REGEXP_REPLACE")))


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

    # What this supervisor may do at this hackathon. Labels and defaults: hackathon.access.PERMISSIONS
    # (a test keeps the two in step). Only admins change these.
    can_view_marks = models.BooleanField("view all marks", default=True, help_text="See every faculty member's marks on the marks page.")
    can_edit_teams = models.BooleanField("edit rooms, teams and students", default=True, help_text="Add and edit rooms, teams and students' details, and move teams between rooms.")
    can_edit_allocations = models.BooleanField("edit allocations", default=True, help_text="Allocate faculty to rooms for each round, and remove allocations.")
    can_edit_rounds = models.BooleanField("edit round times", default=True, help_text="Change when rounds open and close, including Open now and Close now.")
    can_download = models.BooleanField("download Excel files", default=True, help_text="Download this hackathon's Excel files.")
    can_change_marks = models.BooleanField("change marks", default=False, help_text="Change any faculty member's marks, even after they submit. A reason is recorded.")
    can_reopen_marks = models.BooleanField("reopen marksheets", default=False, help_text="Let a faculty member edit marks they already submitted.")
    can_publish = models.BooleanField("publish results", default=False, help_text="Publish or hide round results, and switch \"show jury to students\".")
    can_make_logins = models.BooleanField("make faculty passwords", default=False, help_text="Make or reset temporary passwords for this hackathon's faculty.")
    can_import = models.BooleanField("import Excel", default=False, help_text="Upload the students, faculty and allocations Excel files.")
    can_approve_requests = models.BooleanField("approve faculty requests", default=False, help_text="Approve or reject faculty requests that chose this hackathon.")
    can_add_faculty = models.BooleanField("add and edit faculty", default=False, help_text="Add people to this hackathon's faculty list and edit their details.")
    can_take_attendance = models.BooleanField("take attendance", default=True, help_text="Mark teams and faculty present or absent in each round.")
    can_view_floors = models.BooleanField("see floor lists", default=True, help_text="Open the By floor page, and print or export it.")
    can_email_participants = models.BooleanField("email participants", default=False, help_text="Send sign-in invites to this hackathon's students.")
    can_manage_fields = models.BooleanField("manage fields and imports", default=False, help_text="Define the student and faculty detail fields, and match Excel columns on import.")

    class Meta:
        ordering = ["event", "person__full_name"]
        verbose_name = "hackathon supervisor"
        constraints = [models.UniqueConstraint(fields=["event", "person"], name="supervisor_unique")]

    def __str__(self):
        return f"{self.person.display_name} ({self.get_kind_display()})"

    @property
    def switch_labels(self) -> list[str]:
        from .access import PERMISSIONS

        return [label for name, (label, _, _) in PERMISSIONS.items() if getattr(self, name)]


ROOM_CODE = re.compile(r"^([A-Z]+)(\d+)(\d\d)$")


def clean_room_code(code: str) -> str:
    """Upper-case, no spaces or dashes: " l-502 " -> "L502"."""
    return re.sub(r"[\s-]+", "", code or "").upper()


def parse_room_code(code: str):
    """Letters = block, last two digits = room, digits between = floor: L502 -> ("L", "5", "02"), L1002 -> ("L", "10", "02").

    None if it isn't letters followed by at least three digits.
    """
    match = ROOM_CODE.match(clean_room_code(code))
    return (match.group(1), str(int(match.group(2))), match.group(3)) if match else None


@pghistory.track()
class Room(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="rooms")
    name = models.CharField("room number", max_length=100, help_text="For example L502: block L, floor 5, room 02.")
    building = models.CharField("building / notes", max_length=150, blank=True)
    capacity = models.PositiveIntegerField("max capacity (teams)", null=True, blank=True, help_text="How many teams fit. Used by the automatic room assignment.")
    block = models.CharField(max_length=20, blank=True, help_text="Read from the code; change it if the code doesn't follow the pattern.")
    floor = models.CharField(max_length=10, blank=True, help_text="Read from the code; change it if the code doesn't follow the pattern.")
    code_unread = models.BooleanField("code could not be read", default=False, editable=False)

    class Meta:
        ordering = ["event", "block", "floor", "name"]
        constraints = [models.UniqueConstraint("event", Lower("name"), name="room_name_unique_per_event")]

    def __str__(self):
        return self.name  # people see just the room number, like L502

    def save(self, *args, **kwargs):
        # Block and floor always come from the room number, so they can't drift from it.
        parsed = parse_room_code(self.name)
        if parsed:
            self.name = clean_room_code(self.name)
            self.block, self.floor = parsed[0], parsed[1]
        self.code_unread = parsed is None and not (self.block and self.floor)
        super().save(*args, **kwargs)


@pghistory.track()
class Team(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="teams")
    name = models.CharField(max_length=150)
    project_title = models.CharField(max_length=200, blank=True)
    project_description = models.TextField("project description / problem statement", blank=True)
    number = models.CharField("team number", max_length=30, blank=True,
                              help_text="From the Team No column of the students Excel. Unique in this hackathon.")
    category = models.CharField(max_length=150, blank=True)
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
        constraints = [
            models.UniqueConstraint("event", TEAM_NAME_KEY, name="team_name_unique_per_event_normalised"),
            models.UniqueConstraint(fields=["event", "number"], condition=~Q(number=""), name="team_number_unique_per_event"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = clean_team_name(self.name)
        self.number = clean_team_number(self.number)
        super().save(*args, **kwargs)

    @property
    def number_name(self) -> str:
        """The name an unnamed numbered team gets: "Team 7"."""
        return f"Team {self.number}" if self.number else ""

    @property
    def has_number_name(self) -> bool:
        return bool(self.number) and self.name.lower() == self.number_name.lower()

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
    is_leader = models.BooleanField("team leader", default=False)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["team", "-is_leader", "registration__person__full_name"]
        verbose_name = "team member"
        constraints = [
            models.UniqueConstraint(fields=["team"], condition=Q(is_leader=True), name="one_leader_per_team"),
        ]

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
    focus = models.TextField("focus", blank=True, help_text="What this round is about, shown to faculty.")
    suggested_questions = models.TextField(blank=True, help_text="Questions faculty can ask, shown on the marksheet.")
    total_marks = models.DecimalField("round total", max_digits=6, decimal_places=2, default=100,
                                      help_text="The criteria's max marks should add up to this.")
    weight = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True,
                                 help_text="Share of the weighted total, e.g. 0.25. All rounds together make 1.")
    min_total = models.DecimalField("advance with at least", max_digits=6, decimal_places=2, null=True, blank=True,
                                    help_text="Optional: the average total a team needs to go to the next round.")
    key_criterion = models.ForeignKey("Criterion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                      help_text="Optional: a criterion that must reach a band to advance.")
    key_min_band = models.CharField("key criterion at least", max_length=40, blank=True)
    allow_override = models.BooleanField("evaluators may recommend advancing anyway", default=False)
    comment_prompts = models.JSONField(default=list, blank=True, help_text="One prompt per line, filled in by faculty for each team.")
    rubric_unlocked = models.BooleanField(default=False, editable=False, help_text="An admin unlocked the rubric after marks exist.")
    release_marks_at = models.DateTimeField(
        "show marks to students from", null=True, blank=True,
        help_text="Teams see their own marks for this round automatically from this time.",
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

    def marks_released(self, now=None) -> bool:
        now = now or timezone.now()
        return bool(self.results_published_at or (self.release_marks_at and now >= self.release_marks_at))

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
    tag = models.CharField(max_length=60, blank=True, help_text="A short tag, e.g. Q3.")
    strong_text = models.TextField("strong looks like", blank=True)
    weak_text = models.TextField("weak looks like", blank=True)
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


def member_conflict(person_id, team) -> str:
    """Why this person can't judge this team: they are one of its members (a faculty member who is also a student). Or ""."""
    if team.pk and TeamMember.objects.filter(team=team, registration__person_id=person_id).exists():
        return f"They are a member of {team.name}, so they can't judge it."
    return ""


def volunteer_conflict(person_id, team) -> str:
    """Why this person can't judge this team: they volunteer (take attendance) in its room. Or ""."""
    if not team.room_id:
        return ""
    if Volunteer.objects.filter(event_id=team.event_id, person_id=person_id, is_active=True, rooms=team.room_id).exists():
        return (f"They volunteer in {team.room.name}, where they take attendance for {team.name}, "
                "so they can't judge it. Change their rooms on the Supervisors & volunteers tab first.")
    return ""


@pghistory.track()
class Assignment(models.Model):
    """This faculty member judges this team in this round."""

    class Source(models.TextChoices):
        WEBSITE = "website", "Allocated on the website"
        IMPORT = "import", "Imported from Excel"
        RANDOM = "random", "Allocated randomly"

    round = models.ForeignKey(Round, on_delete=models.PROTECT, related_name="assignments")
    faculty = models.ForeignKey(EventFaculty, on_delete=models.PROTECT, related_name="assignments")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="assignments")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.WEBSITE)
    remarks = models.TextField(blank=True)
    comments = models.JSONField(default=dict, blank=True, help_text="The round's comment prompts and the answers.")
    red_flags = models.JSONField(default=list, blank=True)
    self_score = models.DecimalField("student self-score", max_digits=6, decimal_places=2, null=True, blank=True)
    recommendation = models.CharField(max_length=100, blank=True)
    advance_anyway = models.BooleanField("recommend advancing anyway", default=False)
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
        problem = volunteer_conflict(self.faculty.person_id, self.team) or member_conflict(self.faculty.person_id, self.team)
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


class MarkChange(models.Model):
    """A change to marks by someone other than the faculty member who gave them, or a reopened marksheet."""

    class Kind(models.TextChoices):
        CHANGED = "changed", "Marks changed"
        REOPENED = "reopened", "Marksheet reopened"

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="changes")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CHANGED)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=500)
    before = models.JSONField(default=dict, blank=True, help_text="Criterion label to marks, before.")
    after = models.JSONField(default=dict, blank=True, help_text="Criterion label to marks, after.")

    class Meta:
        ordering = ["-changed_at"]
        verbose_name = "mark change"

    def __str__(self):
        return f"{self.get_kind_display()} by {self.changed_by} on {self.changed_at:%d %b %Y}"


class FieldDefinition(models.Model):
    """One student or faculty detail field for one hackathon: built-in (stored on the person) or custom."""

    class AppliesTo(models.TextChoices):
        STUDENT = "student", "Students"
        FACULTY = "faculty", "Faculty"

    class Kind(models.TextChoices):
        TEXT = "text", "Short text"
        LONG_TEXT = "long_text", "Long text"
        NUMBER = "number", "Number"
        EMAIL = "email", "Email"
        PHONE = "phone", "Phone"
        DATE = "date", "Date"
        YES_NO = "yes_no", "Yes or no"
        CHOICE = "choice", "One choice"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="detail_fields")
    applies_to = models.CharField(max_length=20, choices=AppliesTo.choices)
    key = models.SlugField(max_length=50, help_text="Permanent ID, used in exports.")
    label = models.CharField(max_length=150)
    kind = models.CharField("type", max_length=20, choices=Kind.choices, default=Kind.TEXT)
    required = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField("order", default=0)
    choices = models.JSONField(default=list, blank=True, help_text="For one-choice fields.")
    builtin = models.CharField(
        max_length=30, blank=True, help_text="Set for the built-in fields stored on the person (name, email, ...)."
    )

    class Meta:
        ordering = ["applies_to", "position", "id"]
        constraints = [models.UniqueConstraint(fields=["event", "applies_to", "key"], name="detail_field_key_unique")]

    def __str__(self):
        return self.label

    @property
    def always_required(self) -> bool:
        return self.builtin in ("name", "email")


class PersonDetails(models.Model):
    """Custom field values for one person at one hackathon."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="person_details")
    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hackathon_details")
    values = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "person"], name="person_details_unique")]


class ImportUpload(models.Model):
    """An uploaded Excel file going through the import steps (match columns, check rows, import)."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=20)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    filename = models.CharField(max_length=255)
    content = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    mapping = models.JSONField(default=dict, blank=True, help_text="Target field to column index, as last chosen.")
    sheet_name = models.CharField(max_length=100, blank=True, help_text="Empty: the first sheet.")
    constants = models.JSONField(default=dict, blank=True, help_text="Fields not in the sheet, with one value for every row.")
    header_row = models.PositiveIntegerField(default=0, help_text="1-based row holding the column names. 0: the first non-empty row.")
    result = models.JSONField(default=dict, blank=True, help_text="Summary and skipped rows, once imported.")
    imported_at = models.DateTimeField(null=True, blank=True)
    options = models.JSONField(default=dict, blank=True, help_text="Choices on the column step, e.g. how teams are formed.")


class ImportMapping(models.Model):
    """The last column matching used for one kind of upload at one hackathon, to pre-fill the next one."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=20)
    mapping = models.JSONField(default=dict, help_text="Target field to Excel header text.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "kind"], name="import_mapping_unique")]


@pghistory.track()
class Volunteer(models.Model):
    """Helps on the day at one hackathon: attendance and marking progress. Never sees marks."""

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="volunteers")
    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="volunteer_roles")
    is_active = models.BooleanField(default=True)
    extra = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    rooms = models.ManyToManyField(
        "Room", blank=True, related_name="volunteers",
        help_text="The rooms they look after. They see only these rooms, and take attendance only there.",
    )
    # Labels and defaults: hackathon.access.VOLUNTEER_PERMISSIONS. Only admins change these. There is no switch for marks.
    can_team_attendance = models.BooleanField("team attendance", default=True, help_text="Mark teams (and their members) present or absent in each round.")
    can_faculty_attendance = models.BooleanField("faculty attendance", default=True, help_text="Mark faculty present or absent in each round.")
    can_view_progress = models.BooleanField("see marking progress", default=True, help_text="See which teams each faculty member has submitted marks for (never the marks).")
    can_view_floors = models.BooleanField("see floor lists", default=True, help_text="Open the By floor page, and print or export it.")

    class Meta:
        ordering = ["event", "person__full_name"]
        constraints = [models.UniqueConstraint(fields=["event", "person"], name="volunteer_unique")]

    def __str__(self):
        return self.person.display_name

    @property
    def switch_labels(self) -> list[str]:
        from .access import VOLUNTEER_PERMISSIONS

        return [label for name, (label, _, _) in VOLUNTEER_PERMISSIONS.items() if getattr(self, name)]


@pghistory.track()
class Attendance(models.Model):
    """One attendance mark for a team or a faculty member in a round. Undone marks stay, with who and why."""

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="attendance")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.CASCADE, related_name="attendance")
    faculty = models.ForeignKey(EventFaculty, null=True, blank=True, on_delete=models.CASCADE, related_name="attendance")
    present = models.BooleanField()
    members_present = models.JSONField(default=list, blank=True, help_text="Registration IDs of members ticked present.")
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    marked_at = models.DateTimeField(default=timezone.now)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    undo_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-marked_at"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(team__isnull=False) & Q(faculty__isnull=True)) | (Q(team__isnull=True) & Q(faculty__isnull=False)),
                name="attendance_team_or_faculty",
            ),
            models.UniqueConstraint(fields=["round", "team"], condition=Q(undone_at__isnull=True, team__isnull=False), name="one_live_team_attendance"),
            models.UniqueConstraint(fields=["round", "faculty"], condition=Q(undone_at__isnull=True, faculty__isnull=False), name="one_live_faculty_attendance"),
        ]


DEFAULT_BANDS = [{"name": "Strong", "min": 85}, {"name": "Adequate", "min": 60}, {"name": "Weak", "min": 30}, {"name": "Missing", "min": 0}]
DEFAULT_PROMPTS = ["One strength to tell the team", "One thing the team must test next"]
DEFAULT_RECOMMENDATIONS = ["Pursue as a startup", "Refine and re-test", "Pivot to a new problem"]


class RubricSettings(models.Model):
    """Per hackathon: scoring bands, red flags, the self-score scale and recommendation categories."""

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="rubric_settings")
    bands = models.JSONField(default=list, help_text="Highest first: name and the lowest percentage, e.g. Strong from 85%.")
    red_flags = models.JSONField(default=list, blank=True)
    self_score_max = models.PositiveSmallIntegerField("student self-score out of", default=50, null=True, blank=True,
                                                      help_text="Empty: faculty don't record a self-score.")
    guidance = models.TextField(blank=True, help_text="Shown at the top of every faculty marksheet.")
    eliminate_teams = models.BooleanField(
        "teams are eliminated between rounds", default=False,
        help_text="Off: every team goes through every round. On: after each round an admin confirms which teams go on, "
                  "using the round's advance rule; the others stop.",
    )
    recommendations = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"Rubric settings for {self.event}"


@pghistory.track()
class RoundOutcome(models.Model):
    """Whether a team goes on from a round, as confirmed by an admin."""

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="outcomes")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="outcomes")
    advanced = models.BooleanField()
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["round", "team"], name="one_outcome_per_team_round")]


def _session_token() -> str:
    import secrets

    return secrets.token_urlsafe(16)


@pghistory.track()
class AttendanceSession(models.Model):
    """One attendance check in one classroom: a link that works from opens_at until expires_at, then is final."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="attendance_sessions")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="attendance_sessions")
    name = models.CharField(max_length=100, help_text="For example Morning check.")
    token = models.CharField(max_length=40, unique=True, default=_session_token, editable=False)
    opens_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event", "-opens_at", "room__name"]
        constraints = [models.CheckConstraint(condition=Q(expires_at__gt=F("opens_at")), name="attendance_session_expires_after_opens")]

    def __str__(self):
        return f"{self.name} · {self.room.name}"

    def is_open(self, now=None) -> bool:
        now = now or timezone.now()
        return self.opens_at <= now < self.expires_at

    def is_final(self, now=None) -> bool:
        return (now or timezone.now()) >= self.expires_at

    @property
    def state(self) -> str:
        now = timezone.now()
        return "open" if self.is_open(now) else ("ended" if self.is_final(now) else "upcoming")


class SessionMark(models.Model):
    """One student's attendance in one session. One per student per session, so nobody is counted twice."""

    class Source(models.TextChoices):
        LINK = "link", "Marked with the link"
        LATE = "late", "Added by an admin (late)"
        CORRECTION = "correction", "Corrected by an admin"

    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="marks")
    registration = models.ForeignKey("registrations.Registration", on_delete=models.PROTECT, related_name="session_marks")
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="session_marks",
                             help_text="The team they were in when marked.")
    present = models.BooleanField()
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.LINK)
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    marked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["session", "registration__person__full_name"]
        constraints = [models.UniqueConstraint(fields=["session", "registration"], name="one_mark_per_student_per_session")]


@pghistory.track()
class AttendanceRequest(models.Model):
    """An attendance-taker asks an admin to fix a session's attendance."""

    class Kind(models.TextChoices):
        LATE = "late", "Missed: the student arrived late"
        WRONG = "wrong", "Marked wrongly"
        DETAILS = "details", "Wrong details submitted"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for an admin"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="requests")
    registration = models.ForeignKey("registrations.Registration", on_delete=models.PROTECT, related_name="attendance_requests")
    kind = models.CharField("what happened", max_length=20, choices=Kind.choices)
    want_present = models.BooleanField("should be marked present", default=True)
    note = models.CharField(max_length=500, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["status", "-requested_at"]
