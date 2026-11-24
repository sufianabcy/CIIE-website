"""Fill an empty database with a demo event, so you can click through locally.

    python manage.py seed_demo --admin you@example.com
"""

from datetime import timedelta

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.utils import timezone

from events.models import Event, Question
from judging.models import Criterion, Judge
from people.models import Person
from registrations.models import Registration
from registrations.services import register


class Command(BaseCommand):
    help = "Create a demo admin, coordinator and events for trying the portal locally."

    def add_arguments(self, parser):
        parser.add_argument("--admin", default="admin@example.com", help="Email of the admin to create")

    def handle(self, *args, admin, **options):
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        # A week from now at 10 AM local time, so the demo looks like a real event.
        ten_am = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
        boss = Person.objects.filter(email__iexact=admin).first() or Person.objects.create_superuser(admin)
        boss.full_name = boss.full_name or "Demo Admin"
        boss.phone = boss.phone or "9000000001"
        boss.institution = boss.institution or "KL University"
        boss.profile_completed_at = boss.profile_completed_at or now
        boss.is_staff = True
        boss.save()
        boss.groups.add(Group.objects.get(name="Admin"))

        coordinator = Person.objects.filter(email="coordinator@example.com").first() or Person.objects.create_user(
            email="coordinator@example.com", full_name="Demo Coordinator", phone="9000000002",
            institution="KL University", profile_completed_at=now, is_staff=True,
        )
        coordinator.groups.add(Group.objects.get(name="Coordinator"))

        event, created = Event.objects.get_or_create(
            slug="demo-pitch-day",
            defaults=dict(
                title="Demo Pitch Day",
                summary="Pitch your startup idea in three minutes.",
                description="A practice run of the portal: register, show your pass, get checked in.",
                venue="C Block Seminar Hall",
                starts_at=ten_am + timedelta(days=7),
                ends_at=ten_am + timedelta(days=7, hours=3),
                seats=50,
                status=Event.Status.PUBLISHED,
                earns_points=True,
                points_note="2 SIL points for attending",
                unit="KL-CIIE",
                academic_year="2026-27",
            ),
        )
        event.coordinators.add(boss, coordinator)
        if created:
            Question.objects.create(event=event, key="team_name", label="Team name", position=1)
            Question.objects.create(
                event=event, key="stage", label="Stage of your idea", kind=Question.Kind.CHOICE,
                options=["Just an idea", "Prototype", "Has users"], required=True, position=2,
            )

        practice, _ = Event.objects.get_or_create(
            slug="door-rehearsal",
            defaults=dict(
                title="Door rehearsal",
                summary="Practice check-in for volunteers.",
                starts_at=now - timedelta(hours=1),
                ends_at=now + timedelta(hours=4),
                status=Event.Status.PUBLISHED,
                is_practice=True,
            ),
        )
        practice.coordinators.add(boss, coordinator)
        self.seed_hackathon(ten_am, now, [boss, coordinator])
        self.stdout.write(self.style.SUCCESS(f"Demo ready. Sign in as {boss.email} at /signin/ (the code appears in this terminal)."))

    def seed_hackathon(self, ten_am, now, coordinators):
        """A hackathon with questions, a rubric, two faculty and a few demo participants."""
        event, created = Event.objects.get_or_create(
            slug="demo-hackathon",
            defaults=dict(
                title="Demo Hackathon",
                summary="Build something in 24 hours. Faculty mark every team on the rubric.",
                venue="R&D Block",
                starts_at=ten_am + timedelta(days=10),
                ends_at=ten_am + timedelta(days=11),
                status=Event.Status.PUBLISHED,
            ),
        )
        event.coordinators.add(*coordinators)
        if not created:
            return
        Question.objects.create(event=event, key="team_name", label="Team name", required=True, position=1)
        Question.objects.create(event=event, key="members", label="Team members (names and KL IDs)", kind=Question.Kind.LONG_TEXT, position=2)
        Question.objects.create(
            event=event, key="track", label="Track", kind=Question.Kind.CHOICE,
            options=["HealthTech", "FinTech", "Open"], required=True, position=3,
        )
        Question.objects.create(event=event, key="repo", label="GitHub link", kind=Question.Kind.LINK, position=4)
        for i, (label, top) in enumerate([("Innovation", 10), ("Technical execution", 20), ("Presentation", 10)], start=1):
            Criterion.objects.create(event=event, label=label, max_marks=top, position=i)
        Judge.objects.create(event=event, name="Dr. Demo Faculty One", email="faculty1@example.com", department="CSE")
        Judge.objects.create(event=event, name="Dr. Demo Faculty Two", email="faculty2@example.com", department="ECE")
        for i in range(1, 7):
            person = Person.objects.filter(email=f"student{i}@example.com").first() or Person.objects.create_user(
                email=f"student{i}@example.com", full_name=f"Demo Student {i}", phone=f"90000001{i:02d}",
                institution="KL University", branch="CSE", year_of_study="3", profile_completed_at=now,
            )
            if not Registration.objects.filter(event=event, person=person).exists():
                register(event=event, person=person, answers={
                    "team_name": f"Team {i}", "members": f"Demo Student {i}", "track": "Open",
                    "repo": f"https://github.com/example/team-{i}",
                })
