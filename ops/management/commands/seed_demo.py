"""Fill an empty database with a demo event, so you can click through locally.

    python manage.py seed_demo --admin you@example.com
"""

from datetime import timedelta

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.utils import timezone

from events.models import Event, Question
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
        """A hackathon with 2 rooms, 4 teams, 2 rounds with rubrics, 3 faculty, a supervisor and allocations."""
        from hackathon import services
        from hackathon.models import Criterion, Round, Team, TeamMember
        from people.staff import issue_temp_password

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
        if event.kind != Event.Kind.HACKATHON:
            event.kind = Event.Kind.HACKATHON
            event.save(update_fields=["kind"])
        event.coordinators.add(*coordinators)
        if created:
            Question.objects.create(event=event, key="team_name", label="Team name", required=True, position=1)
            Question.objects.create(
                event=event, key="track", label="Track", kind=Question.Kind.CHOICE,
                options=["HealthTech", "EdTech", "FinTech", "Open"], required=True, position=2,
            )
        students = []
        for i in range(1, 13):
            person = Person.objects.filter(email=f"student{i}@example.com").first() or Person.objects.create_user(
                email=f"student{i}@example.com", full_name=f"Demo Student {i}", phone=f"90000001{i:02d}",
                institution="KL University", branch="CSE", year_of_study="3", profile_completed_at=now,
            )
            registration = Registration.objects.filter(event=event, person=person).first() or register(
                event=event, person=person, answers={"team_name": f"Team {i}", "track": "Open"},
                source=Registration.Source.IMPORT,
            )[0]
            students.append(registration)

        faculty = {}
        for name, emp, dept in [("Dr. Kavya Reddy", "KLF101", "CSE"), ("Dr. Arjun Rao", "KLF102", "ECE"), ("Dr. Meera Nair", "KLF103", "AI&DS"),
                                ("Dr. Ravi Kumar", "KLF104", "CSE")]:
            email = name.split()[1].lower() + "@example.com"
            person = (
                Person.objects.filter(employee_id=emp).first()
                or Person.objects.filter(email=email).first()
                or Person.objects.create_user(email=email, full_name=name, source=Person.Source.IMPORT, profile_completed_at=now)
            )
            person.employee_id, person.branch, person.full_name = emp, dept, name
            person.save()
            faculty[emp] = services.add_faculty(event, person)[0]
        sup_person = Person.objects.filter(email="supervisor@example.com").first() or Person.objects.create_user(
            email="supervisor@example.com", full_name="Prof. Sita Verma", employee_id="KLF100", branch="CSE",
            source=Person.Source.IMPORT, profile_completed_at=now,
        )
        services.add_supervisor(event, sup_person, "faculty")

        lab1, lab2 = services.get_room(event, "R206"), services.get_room(event, "R207")
        s101 = services.get_room(event, "S101")
        for room, capacity in ((lab1, 4), (lab2, 4), (s101, 3)):
            room.capacity = room.capacity or capacity
            room.save()
        plan = [
            ("Team Nova", "Smart attendance", "Face-based attendance for labs, with an offline mode.", lab1, None, students[0:2]),
            ("Team Byte", "Canteen queue", "Pre-order and pick-up slots to cut the lunch queue.", lab1, None, students[2:4]),
            ("Team Pulse", "Clinic finder", "Find the nearest open campus clinic and its wait time.", lab2, faculty["KLF102"], students[4:5]),
            ("Team Solo", "Lab booking", "Book lab benches by the hour.", lab2, None, students[5:6]),
            ("Team Orbit", "Bus tracker", "Live campus bus positions.", None, None, students[6:7]),
            ("Team Quill", "Notes swap", "Share class notes by course.", None, None, students[7:8]),
            ("Team Volt", "Energy meter", "Show each block's power use.", None, None, students[8:9]),
            ("Team Kite", "Event buddy", "Find people to go to events with.", None, None, students[9:10]),
            ("Team Lens", "Lost and found", "Photos of found items by place.", None, None, students[10:12]),
        ]
        for name, title, description, room, mentor, members in plan:
            team = Team.objects.filter(event=event, name=name).first() or Team.objects.create(event=event, name=name)
            team.project_title, team.project_description = title, description  # rooms come from the room assignment below
            team.mentor = mentor.person if mentor else None
            team.save()
            for registration in members:
                if not TeamMember.objects.filter(registration=registration).exists():
                    TeamMember.objects.create(team=team, registration=registration)

        round1, _ = Round.objects.get_or_create(
            event=event, position=1, defaults=dict(name="Round 1: Idea and demo", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(days=3))
        )
        round2, _ = Round.objects.get_or_create(
            event=event, position=2, defaults=dict(name="Round 2: Final pitch", opens_at=ten_am + timedelta(days=11), closes_at=ten_am + timedelta(days=11, hours=4))
        )
        if not round1.criteria.exists():
            for i, (label, top) in enumerate([("Problem and idea", 10), ("Working demo", 15), ("Technical depth", 10), ("Presentation", 5)], start=1):
                Criterion.objects.create(round=round1, label=label, max_marks=top, position=i)
        if not round2.criteria.exists():
            for i, (label, top) in enumerate([("Final pitch", 20), ("Impact", 10)], start=1):
                Criterion.objects.create(round=round2, label=label, max_marks=top, position=i)

        # Faculty to teams first (at random, never a mentor), then rooms by capacity, one room per faculty.
        from hackathon import rooms as room_tools

        if not round1.assignments.exists():
            room_tools.random_allocate(round1, per_team=1, seed=7)
        try:
            room_tools.apply_plan(round1)
        except Exception as exc:  # noqa: BLE001 - show it, the demo still works
            self.stdout.write(f"  rooms not assigned: {exc}")
        volunteer_person = Person.objects.filter(email="volunteer@example.com").first() or Person.objects.create_user(
            email="volunteer@example.com", full_name="Demo Volunteer", phone="9000000300", source=Person.Source.IMPORT, profile_completed_at=now,
        )
        services.add_volunteer(event, volunteer_person)

        expires = now + timedelta(days=7)
        self.stdout.write("Demo hackathon staff (local demo data only). Sign in at /signin/password/:")
        for person in [f.person for f in faculty.values()] + [sup_person, volunteer_person]:
            if person.has_usable_password():
                self.stdout.write(f"  {person.full_name}: {person.email} / login ID {person.login_id} (already has a password)")
                continue
            password = issue_temp_password(person, expires)
            self.stdout.write(f"  {person.full_name}: {person.email} / login ID {person.login_id}, temporary password {password}")
