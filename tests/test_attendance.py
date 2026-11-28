"""Attendance sessions: links per classroom that expire, ticking who is there, latecomers and corrections by admins,
requests from attendance-takers, the Excel report, and attendance-taker accounts made by the portal.
All names and IDs are made up."""

import re
from datetime import timedelta
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from events.models import Event
from hackathon import attendance, services
from hackathon.models import AttendanceRequest, AttendanceSession, Room, SessionMark, Supervisor, Team, TeamMember, Volunteer
from people.models import Person
from registrations.services import register

PASSWORD = "Quiet-Harbour-Lamp-71"


@pytest.fixture
def admin_user(make_person):
    person = make_person(full_name="Admin Example", is_staff=True)
    person.groups.add(Group.objects.get(name="Admin"))
    return person


@pytest.fixture
def hack(make_event):
    """L502 with team 1 (two students) and team 2 (one), L503 with team 3 (one); a volunteer for each room."""
    event = make_event(title="Attendance Hack", kind=Event.Kind.HACKATHON)
    l502, l503 = Room.objects.create(event=event, name="L502"), Room.objects.create(event=event, name="L503")
    regs = {}
    for number, room, names in (("1", l502, ["Asha Example", "Bala Example"]), ("2", l502, ["Chitra Example"]),
                                ("3", l503, ["Dev Example"])):
        team = Team.objects.create(event=event, name=f"Team {number}", number=number, room=room)
        for name in names:
            sid = f"25000900{len(regs) + 1:02d}"
            person = Person.objects.create_user(email=f"{sid}@kluniversity.in", full_name=name, student_id=sid)
            reg, _ = register(event=event, person=person)
            TeamMember.objects.create(team=team, registration=reg)
            regs[name.split()[0]] = reg
    vols = {}
    for name, room in (("Vol 502", l502), ("Vol 503", l503)):
        row = services.add_volunteer(event, Person.objects.create_user(email=f"{name.replace(' ', '').lower()}@example.com", full_name=name))[0]
        row.rooms.set([room])
        vols[room.name] = row
    return {"event": event, "rooms": (l502, l503), "regs": regs, "vols": vols}


def said(response):
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


def make(hack, admin, room, minutes=60, opens=None):
    return attendance.create_sessions(hack["event"], [room], "Morning check", opens or timezone.now(), minutes, admin)[0]


def test_admin_makes_a_link_per_classroom(client, hack, admin_user):
    event = hack["event"]
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_attendance", args=[event.slug])).content.decode()
    assert "Make attendance links" in page and "L502" in page
    client.post(reverse("hackathon_manage_attendance", args=[event.slug]),
                {"name": "Morning check", "rooms": [r.pk for r in hack["rooms"]], "minutes": "45", "opens_at": ""})
    sessions = list(AttendanceSession.objects.filter(event=event))
    assert len(sessions) == 2 and len({s.token for s in sessions}) == 2
    assert all(s.is_open() and (s.expires_at - s.opens_at) == timedelta(minutes=45) for s in sessions)
    page = client.get(reverse("hackathon_manage_attendance", args=[event.slug])).content.decode()
    assert all(reverse("attendance_take", args=[s.token]) in page for s in sessions)


def test_a_taker_sees_and_marks_only_their_classroom(client, hack, admin_user):
    l502, l503 = hack["rooms"]
    s502, s503 = make(hack, admin_user, l502), make(hack, admin_user, l503)
    url = reverse("attendance_take", args=[s502.token])
    assert client.get(url)["Location"].startswith(reverse("signin"))  # sign in first
    vol = hack["vols"]["L502"].person
    client.force_login(vol)
    page = client.get(url).content.decode()
    assert "Asha Example" in page and "Chitra Example" in page and "Dev Example" not in page
    assert client.get(reverse("attendance_take", args=[s503.token])).status_code == 403
    regs = hack["regs"]
    response = client.post(url, {"present": [regs["Asha"].pk, regs["Chitra"].pk]})
    assert "Saved: 2 of 3 present" in said(response)
    marks = {m.registration_id: m for m in SessionMark.objects.filter(session=s502)}
    assert marks[regs["Asha"].pk].present and not marks[regs["Bala"].pk].present and marks[regs["Chitra"].pk].present
    assert all(m.marked_by == vol and m.marked_at and m.source == "link" for m in marks.values())
    assert marks[regs["Asha"].pk].team.number == "1"
    # Saving again changes the marks; nobody is counted twice.
    client.post(url, {"present": [regs["Bala"].pk]})
    assert SessionMark.objects.filter(session=s502).count() == 3
    assert list(SessionMark.objects.filter(session=s502, present=True).values_list("registration", flat=True)) == [regs["Bala"].pk]
    with pytest.raises(IntegrityError), transaction.atomic():
        SessionMark.objects.create(session=s502, registration=regs["Bala"], present=True, marked_by=vol)
    # A student from another classroom can't be slipped in.
    response = client.post(url, {"present": [regs["Dev"].pk]})
    assert "aren't in this classroom" in said(response) and not SessionMark.objects.filter(registration=regs["Dev"]).exists()
    # The volunteer's own pages show the open link for their room only.
    assert reverse("attendance_take", args=[s502.token]) in client.get(reverse("staff_home")).content.decode()
    floors = client.get(reverse("hackathon_volunteer_today", args=[hack["event"].slug])).content.decode()
    assert url in floors and reverse("attendance_take", args=[s503.token]) not in floors


def test_the_link_expires_and_the_attendance_is_final(client, hack, admin_user):
    l502 = hack["rooms"][0]
    regs = hack["regs"]
    session = make(hack, admin_user, l502, minutes=60, opens=timezone.now() - timedelta(minutes=90))
    client.force_login(hack["vols"]["L502"].person)
    url = reverse("attendance_take", args=[session.token])
    page = client.get(url).content.decode()
    assert "Final" in page and "<fieldset disabled>" in page
    response = client.post(url, {"present": [regs["Asha"].pk]})
    assert "expired" in said(response) and not SessionMark.objects.exists()
    later = make(hack, admin_user, l502, opens=timezone.now() + timedelta(hours=1))
    response = client.post(reverse("attendance_take", args=[later.token]), {"present": [regs["Asha"].pk]})
    assert "isn't open yet" in said(response)
    # Only admins add latecomers once it has expired.
    detail = reverse("hackathon_attendance_session", args=[hack["event"].slug, session.pk])
    assert client.post(detail, {"action": "present", "registration": regs["Asha"].pk}).status_code == 403
    client.force_login(admin_user)
    client.post(detail, {"action": "present", "registration": regs["Asha"].pk})
    mark = SessionMark.objects.get(session=session, registration=regs["Asha"])
    assert mark.present and mark.source == "late" and mark.marked_by == admin_user
    response = client.post(detail, {"action": "present", "registration": regs["Dev"].pk})
    assert "isn't a student in L502" in said(response)


def test_an_admin_can_end_a_link_early(client, hack, admin_user):
    session = make(hack, admin_user, hack["rooms"][0])
    client.force_login(admin_user)
    client.post(reverse("hackathon_attendance_session", args=[hack["event"].slug, session.pk]), {"action": "end"})
    session.refresh_from_db()
    assert session.is_final()


def test_correction_requests(client, hack, admin_user):
    l502 = hack["rooms"][0]
    regs = hack["regs"]
    session = make(hack, admin_user, l502)
    vol = hack["vols"]["L502"].person
    client.force_login(vol)
    url = reverse("attendance_take", args=[session.token])
    client.post(url, {"present": [regs["Asha"].pk]})
    AttendanceSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(minutes=1),
                                                           opens_at=timezone.now() - timedelta(hours=1))
    client.post(url, {"action": "request", "registration": regs["Bala"].pk, "kind": "late", "want_present": "1", "note": "Came at 10:20"})
    response = client.post(url, {"action": "request", "registration": regs["Bala"].pk, "kind": "late", "want_present": "1"})
    assert "already a request" in said(response)
    response = client.post(url, {"action": "request", "registration": regs["Dev"].pk, "kind": "late", "want_present": "1"})
    assert "Choose a student from this classroom" in said(response)
    client.post(url, {"action": "request", "registration": regs["Chitra"].pk, "kind": "details", "want_present": "1", "note": "Name spelt wrong"})
    late, details = AttendanceRequest.objects.get(registration=regs["Bala"]), AttendanceRequest.objects.get(registration=regs["Chitra"])
    assert late.status == "pending" and late.requested_by == vol
    # Takers can't decide requests.
    assert client.post(reverse("hackathon_attendance_request", args=[hack["event"].slug, late.pk]), {"decision": "approve"}).status_code == 403
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_manage_attendance", args=[hack["event"].slug])).content.decode()
    assert 'data-testid="pending-requests"' in page and "Came at 10:20" in page
    client.post(reverse("hackathon_attendance_request", args=[hack["event"].slug, late.pk]), {"decision": "approve", "note": "OK"})
    client.post(reverse("hackathon_attendance_request", args=[hack["event"].slug, details.pk]), {"decision": "reject"})
    late.refresh_from_db()
    details.refresh_from_db()
    assert late.status == "approved" and late.decided_by == admin_user and details.status == "rejected"
    bala = SessionMark.objects.get(session=session, registration=regs["Bala"])
    assert bala.present and bala.source == "late"
    assert SessionMark.objects.get(session=session, registration=regs["Chitra"]).present is False  # details: no change
    client.force_login(vol)
    assert "Approved" in client.get(url).content.decode()


def test_excel_report_session_student_and_classroom_wise(client, hack, admin_user):
    l502, l503 = hack["rooms"]
    regs = hack["regs"]
    s1 = make(hack, admin_user, l502)
    s2 = make(hack, admin_user, l503)
    attendance.take(s1, hack["vols"]["L502"].person, [regs["Asha"].pk, regs["Bala"].pk])
    attendance.take(s2, hack["vols"]["L503"].person, [])
    client.force_login(admin_user)
    response = client.get(reverse("hackathon_attendance_report", args=[hack["event"].slug]))
    assert response.status_code == 200
    wb = load_workbook(BytesIO(response.content))
    assert wb.sheetnames[:5] == ["By session", "By student", "By classroom", "Every mark", "Requests"]
    by_session = list(wb["By session"].iter_rows(values_only=True))
    row = next(r for r in by_session[1:] if r[1] == "L502")
    assert row[5:9] == (3, 2, 1, 0)
    by_student = list(wb["By student"].iter_rows(values_only=True))
    header = by_student[0]
    asha = next(r for r in by_student[1:] if r[0] == "Asha Example")
    assert asha[header.index("KL ID")] == "2500090001" and asha[header.index("Team no")] == "1"
    assert asha[header.index("Morning check · L502")] == "Present" and asha[header.index("Morning check · L503")] in ("", None)
    assert asha[header.index("Present %")] == 100
    dev = next(r for r in by_student[1:] if r[0] == "Dev Example")
    assert dev[header.index("Morning check · L503")] == "Absent"
    by_room = list(wb["By classroom"].iter_rows(values_only=True))
    assert any(r[0] == "L502" and r[3:7] == (3, 2, 1, 0) for r in by_room)
    marks = list(wb["Every mark"].iter_rows(values_only=True))
    assert len(marks) == 1 + 4 and all(r[8] in ("Vol 502", "Vol 503") for r in marks[1:])
    # Volunteers can't download it.
    client.force_login(hack["vols"]["L502"].person)
    assert client.get(reverse("hackathon_attendance_report", args=[hack["event"].slug])).status_code == 403


def test_admin_makes_attendance_taker_accounts(client, hack, admin_user):
    event = hack["event"]
    l502 = hack["rooms"][0]
    client.force_login(admin_user)
    url = reverse("hackathon_attendance_takers", args=[event.slug])
    page = client.post(url, {"action": "make", "names": "Taker One\nTaker Two\n", "rooms": [l502.pk], "days": "7"}).content.decode()
    assert 'data-testid="new-logins"' in page
    one = Person.objects.get(full_name="Taker One")
    assert one.login_id and one.email is None and one.must_change_password
    row = Volunteer.objects.get(event=event, person=one)
    assert list(row.rooms.all()) == [l502] and row.can_team_attendance and not row.can_view_progress
    password = re.search(rf"{one.login_id}</code></td><td><code>([^<]+)</code>", page).group(1)
    session = make(hack, admin_user, l502)
    client.logout()
    response = client.post(reverse("signin"), {"login": one.login_id, "password": password})
    assert response["Location"].startswith(reverse("password_change"))
    response = client.post(reverse("password_change"), {"new_password1": PASSWORD, "new_password2": PASSWORD})
    assert response["Location"] == reverse("staff_home")
    assert reverse("attendance_take", args=[session.token]) in client.get(reverse("staff_home")).content.decode()
    assert client.get(reverse("attendance_take", args=[session.token])).status_code == 200
    # A new password for one taker, shown once.
    client.force_login(admin_user)
    page = client.post(url, {"action": "reset", "volunteer": row.pk}).content.decode()
    one.refresh_from_db()
    assert one.login_id in page and not one.check_password(PASSWORD)
    page = client.post(url, {"action": "make", "names": "   ", "days": "7"}).content.decode()
    assert "Type at least one name" in page


def test_who_can_reach_the_attendance_pages(client, hack, admin_user, make_person):
    event = hack["event"]
    session = make(hack, admin_user, hack["rooms"][0])
    sup_person = make_person(full_name="Sup Example")
    sup, _ = services.add_supervisor(event, sup_person)
    client.force_login(sup_person)
    for name, args in (("hackathon_manage_attendance", [event.slug]), ("hackathon_attendance_takers", [event.slug]),
                       ("hackathon_attendance_session", [event.slug, session.pk])):
        assert client.get(reverse(name, args=args)).status_code == 403
    assert client.get(reverse("attendance_take", args=[session.token])).status_code == 200  # "take attendance" switch
    Supervisor.objects.filter(pk=sup.pk).update(can_take_attendance=False)
    assert client.get(reverse("attendance_take", args=[session.token])).status_code == 403
    vol = hack["vols"]["L502"]
    Volunteer.objects.filter(pk=vol.pk).update(can_team_attendance=False)
    client.force_login(vol.person)
    assert client.get(reverse("attendance_take", args=[session.token])).status_code == 403
    student = hack["regs"]["Asha"].person
    client.force_login(student)
    assert client.get(reverse("attendance_take", args=[session.token])).status_code == 403
    assert client.get(reverse("attendance_take", args=["not-a-real-token"])).status_code == 404


def test_the_overview_shows_every_section_and_the_attendance_button(client, hack, admin_user, make_person):
    event = hack["event"]
    client.force_login(admin_user)
    page = client.get(reverse("hackathon_dashboard", args=[event.slug])).content.decode()
    url = reverse("hackathon_manage_attendance", args=[event.slug])
    assert 'data-testid="attendance-button"' in page and f'href="{url}">Attendance</a>' in page
    for name in ("hackathon_manage_students", "hackathon_manage_teams", "hackathon_manage_allocations"):
        assert reverse(name, args=[event.slug]) in page
    sup_person = make_person(full_name="Sup Example")
    services.add_supervisor(event, sup_person)
    client.force_login(sup_person)
    page = client.get(reverse("hackathon_dashboard", args=[event.slug])).content.decode()
    assert url not in page  # admins only
