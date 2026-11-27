"""Sign in (email and password, no code) and New registration (email, code, password, then student or faculty)."""

from datetime import timedelta

from django.core import mail
from django.urls import reverse
from django.utils import timezone

from mailer.models import OutboxEmail
from people.models import Person
from people.staff import MAX_FAILED_SIGNINS, issue_temp_password

PASSWORD = "Correct-Horse-Battery-9"


def _code(body):
    return next(line.strip() for line in body.splitlines() if line.strip() and len(line.strip()) <= 9
                and line.strip().replace("-", "").isalnum() and line.strip().upper() == line.strip())


def _register(client, capture, email):
    with capture(execute=True):
        response = client.post(reverse("signin"), {"mode": "register", "email": email})
    assert response.status_code == 302, response.content[-800:]
    return response


def test_signin_page_offers_sign_in_and_new_registration(client, db):
    page = client.get(reverse("signin")).content.decode()
    assert "New registration" in page and 'name="password"' in page and "Forgot password" in page
    register = client.get(reverse("signin") + "?mode=register").content.decode()
    assert 'name="email"' in register and 'name="password"' not in register and "Email me a code" in register
    # The old faculty sign-in address now opens the one sign-in page.
    assert client.get(reverse("staff_signin") + "?next=/faculty/")["Location"] == reverse("signin") + "?next=%2Ffaculty%2F"


def test_new_student_registration_code_then_password_then_student_details(client, db, django_capture_on_commit_callbacks):
    _register(client, django_capture_on_commit_callbacks, "New.Student@Example.com")
    person = Person.objects.get(email="new.student@example.com")
    assert person.must_change_password and not person.has_usable_password()
    verify = client.post(reverse("account_email_verification_sent"), {"code": _code(mail.outbox[-1].body)})
    assert verify.status_code == 302
    # Signed in now, but every page asks for a password first.
    profile = client.get(reverse("profile"))
    assert profile["Location"].startswith(reverse("password_change"))
    page = client.get(profile["Location"]).content.decode()
    assert "Choose your password" in page and "Student or faculty" in page and "old_password" not in page
    saved = client.post(reverse("password_change") + "?next=/me/profile/", {"new_password1": PASSWORD, "new_password2": PASSWORD})
    assert saved["Location"] == reverse("profile")
    # Then: student or faculty?
    choose = client.get(reverse("profile")).content.decode()
    assert "Are you a student or faculty?" in choose and "?as=student" in choose and "?as=faculty" in choose
    student = client.get(reverse("profile") + "?as=student").content.decode()
    assert 'name="institution"' in student and 'name="year_of_study"' in student and 'name="employee_id"' not in student
    done = client.post(reverse("profile"), {"as": "student", "full_name": "New Student", "phone": "9876543210",
                                            "institution": "KL University", "branch": "CSE", "year_of_study": "2"})
    assert done.status_code == 302
    person.refresh_from_db()
    assert person.profile_complete and person.kind == Person.Kind.STUDENT and not person.must_change_password

    # Next time: email and password, and no code is emailed.
    client.logout()
    codes = OutboxEmail.objects.filter(kind="code").count()
    response = client.post(reverse("signin"), {"login": "new.student@example.com", "password": PASSWORD})
    assert response["Location"] == reverse("my_registrations")
    assert OutboxEmail.objects.filter(kind="code").count() == codes


def test_new_faculty_registration_asks_faculty_details_and_waits_for_an_admin(client, db, django_capture_on_commit_callbacks):
    from hackathon.models import FacultyRequest

    _register(client, django_capture_on_commit_callbacks, "dr.new@kluniversity.in")
    client.post(reverse("account_email_verification_sent"), {"code": _code(mail.outbox[-1].body)})
    client.post(reverse("password_change"), {"new_password1": PASSWORD, "new_password2": PASSWORD})
    page = client.get(reverse("profile") + "?as=faculty").content.decode()
    assert 'name="employee_id"' in page and 'name="designation"' in page and 'name="institution"' not in page
    response = client.post(reverse("profile"), {"as": "faculty", "full_name": "Dr. New", "employee_id": "KLF900", "department": "CSE",
                                                "designation": "Assistant Professor", "phone": "9876543210"})
    assert response["Location"] == reverse("faculty_register")
    person = Person.objects.get(email="dr.new@kluniversity.in")
    req = FacultyRequest.objects.get(person=person)
    assert req.status == FacultyRequest.Status.PENDING and req.employee_id == "KLF900"
    # Nothing is granted until an admin approves.
    assert not person.is_faculty and person.kind == Person.Kind.STUDENT and person.profile_complete
    assert person.faculty_pending and person.designation == "Assistant Professor"
    profile = client.get(reverse("profile")).content.decode()
    assert 'name="institution"' not in profile and "Department" in profile


def test_registering_an_email_that_already_has_a_password_says_sign_in(client, make_person, django_capture_on_commit_callbacks):
    person = make_person(email="has@example.com")
    person.set_password(PASSWORD)
    person.save()
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("signin"), {"mode": "register", "email": "HAS@example.com"}, follow=True)
    assert b"You already have an account" in response.content
    assert not OutboxEmail.objects.filter(kind="code").exists()  # no code for an existing account


def test_someone_added_by_the_organisers_registers_by_setting_a_password(client, db, django_capture_on_commit_callbacks):
    person = Person.objects.create_user(email="added@kluniversity.in", full_name="Added", source=Person.Source.IMPORT)
    _register(client, django_capture_on_commit_callbacks, "added@kluniversity.in")
    assert Person.objects.filter(email="added@kluniversity.in").count() == 1
    assert "set a new password" in mail.outbox[-1].body
    client.post(reverse("account_confirm_password_reset_code"), {"code": _code(mail.outbox[-1].body)})
    done = client.post(reverse("account_complete_password_reset"), {"password1": PASSWORD, "password2": PASSWORD})
    assert done.status_code == 302
    person.refresh_from_db()
    assert person.check_password(PASSWORD) and not person.must_change_password
    client.logout()
    assert client.post(reverse("signin"), {"login": "added@kluniversity.in", "password": PASSWORD}).status_code == 302
    assert "_auth_user_id" in client.session


def test_forgot_password_sets_a_new_one_and_lifts_a_lockout(client, make_person, django_capture_on_commit_callbacks):
    person = make_person(email="locked@example.com")
    temp = issue_temp_password(person, timezone.now() + timedelta(days=1))
    for _ in range(MAX_FAILED_SIGNINS):
        client.post(reverse("signin"), {"login": person.email, "password": "wrong"})
    locked = client.post(reverse("signin"), {"login": person.email, "password": temp}).content.decode()
    assert "Too many wrong passwords" in locked and "Forgot password" in locked
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("account_reset_password"), {"email": person.email})
    client.post(reverse("account_confirm_password_reset_code"), {"code": _code(mail.outbox[-1].body)})
    client.post(reverse("account_complete_password_reset"), {"password1": PASSWORD, "password2": PASSWORD})
    person.refresh_from_db()
    assert person.failed_password_signins == 0 and not person.must_change_password and person.temp_password_expires_at is None
    client.logout()
    assert client.post(reverse("signin"), {"login": person.email, "password": PASSWORD})["Location"] == reverse("my_registrations")


def test_changing_a_chosen_password_needs_the_current_one(client, make_person):
    person = make_person(email="change@example.com")
    person.set_password(PASSWORD)
    person.save()
    client.force_login(person)
    page = client.get(reverse("password_change")).content.decode()
    assert "Current password" in page
    refused = client.post(reverse("password_change"), {"old_password": "nope", "new_password1": "Another-Good-Pass-7", "new_password2": "Another-Good-Pass-7"})
    assert refused.status_code == 200
    person.refresh_from_db()
    assert person.check_password(PASSWORD)


def test_sign_in_keeps_where_you_were_going(client, make_person):
    person = make_person(email="next@example.com")
    person.set_password(PASSWORD)
    person.save()
    response = client.post(reverse("signin"), {"login": person.email, "password": PASSWORD, "next": "/me/events/"})
    assert response["Location"] == "/me/events/"
    client.logout()
    outside = client.post(reverse("signin"), {"login": person.email, "password": PASSWORD, "next": "https://evil.example/"})
    assert outside["Location"] == reverse("my_registrations")
