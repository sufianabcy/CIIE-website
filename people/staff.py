"""Password sign-in for staff (faculty and supervisors): temporary passwords, lockout.

Students keep signing in with an emailed code. Staff who were imported may have
no usable email, so they get a short login ID as well.
"""

import secrets

from django.db.models import Q
from django.utils import timezone

from .models import Person, normalise_email

# No 0/O, 1/I/L or U, so a password copied from a printout or a phone works first time.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_FAILED_SIGNINS = 8


def _random(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def issue_temp_password(person: Person, expires_at) -> str:
    """Give a new temporary password (and a login ID if they have none). Returns the password, never stored."""
    if not person.login_id:
        while True:
            candidate = "S" + _random(6)
            if not Person.objects.filter(login_id=candidate).exists():
                break
        person.login_id = candidate
    raw = f"{_random(4)}-{_random(4)}-{_random(4)}"
    person.set_password(raw)
    person.must_change_password = True
    person.temp_password_expires_at = expires_at
    person.failed_password_signins = 0
    person.save()
    return raw


def find_by_login(login: str) -> Person | None:
    login = (login or "").strip()
    if not login:
        return None
    if "@" in login:
        email = normalise_email(login)
        return Person.objects.filter(Q(email__iexact=email) | Q(kl_email__iexact=email)).first()
    return Person.objects.filter(login_id__iexact=login).first()


class SignInRefused(Exception):
    pass


def check_password_signin(person: Person | None, password: str) -> Person:
    """The person, if this password signs them in now; otherwise SignInRefused with a reason to show."""
    wrong = "That login and password don't match."
    if person is None or not person.is_active or not person.has_usable_password():
        raise SignInRefused(wrong)
    if person.failed_password_signins >= MAX_FAILED_SIGNINS:
        raise SignInRefused("Too many wrong passwords. Ask the organisers to unlock your account or send a new temporary password.")
    if not person.check_password(password or ""):
        Person.objects.filter(pk=person.pk).update(failed_password_signins=person.failed_password_signins + 1)
        raise SignInRefused(wrong)
    if person.must_change_password and person.temp_password_expires_at and timezone.now() > person.temp_password_expires_at:
        raise SignInRefused("This temporary password has expired. Ask the organisers for a new one.")
    Person.objects.filter(pk=person.pk).update(failed_password_signins=0)
    return person
