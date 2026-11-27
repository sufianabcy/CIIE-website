from urllib.parse import urlencode

from allauth.account.models import EmailAddress
from allauth.account.views import PasswordResetView, SignupView
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from ops.models import SiteSettings

from .forms import ProfileForm, RegisterForm, SignInForm
from .models import Person, is_kl_email, normalise_email
from .staff import SignInRefused, check_password_signin, find_by_login

IN_APP_BROWSERS = ("Instagram", "FBAN", "FBAV", "WhatsApp", "Snapchat", "LinkedInApp", "Line/")


def _safe_next(request, url: str | None) -> str:
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return ""


def _with_next(name: str, next_url: str) -> str:
    url = reverse(name)
    return f"{url}?{urlencode({'next': next_url})}" if next_url and next_url != url else url


def signin_routes(request) -> dict:
    site = SiteSettings.load()
    agent = request.META.get("HTTP_USER_AGENT", "")
    in_app = any(marker in agent for marker in IN_APP_BROWSERS)
    return {
        "email_code": site.signin_email_code_enabled,
        "google": bool(site.signin_google_enabled and settings.GOOGLE_CLIENT_ID and not in_app),
        "microsoft": bool(
            site.signin_microsoft_enabled and settings.MICROSOFT_CLIENT_ID and settings.KL_MICROSOFT_TENANT_ID
        ),
        "in_app_browser": in_app,
    }


def find_account(email: str) -> Person | None:
    """The person who owns this email (a verified or unverified address of theirs, or their main email)."""
    email = normalise_email(email)
    address = EmailAddress.objects.filter(email__iexact=email).select_related("user").first()
    if address:
        return address.user
    return Person.objects.filter(email__iexact=email).first()


def account_exists(email: str) -> bool:
    return find_account(email) is not None


def has_own_password(person: Person) -> bool:
    """They chose a password themselves (not a temporary one from the organisers)."""
    return person.has_usable_password() and not person.must_change_password


def _staff_landing(person) -> str:
    if person.must_change_password:
        return "password_change"
    if person.faculty_roles.exists() or person.supervisor_roles.exists() or person.volunteer_roles.exists():
        return "staff_home"
    return "my_registrations"


def _onward(person, next_url: str):
    """Where to go once signed in with a password of their own. Faculty and other staff go straight to their pages
    (their details come from the organisers); others fill in their profile first, once."""
    landing = _staff_landing(person)
    if not person.profile_complete and landing == "my_registrations":
        return redirect(_with_next("profile", next_url))
    return redirect(next_url or landing)


def _after_signin(person, next_url: str):
    if person.must_change_password:
        return redirect(_with_next("password_change", next_url))
    return _onward(person, next_url)


def _password_signin(request, form):
    """Sign in with email or login ID and password. A response on success, else None (with a message)."""
    throttle = f"signin-fail:{request.META.get('REMOTE_ADDR', '')}"
    if cache.get(throttle, 0) >= 30:
        messages.error(request, "Too many wrong tries from this network. Wait an hour, or use \"Forgot password\".")
        return None
    try:
        person = check_password_signin(find_by_login(form.cleaned_data["login"]), form.cleaned_data["password"])
    except SignInRefused as refused:
        cache.set(throttle, cache.get(throttle, 0) + 1, 3600)
        messages.error(request, str(refused))
        return None
    login(request, person, backend="django.contrib.auth.backends.ModelBackend")
    return _after_signin(person, _safe_next(request, form.cleaned_data.get("next")))


def _register(request, form, routes):
    """New registration: a code to the email, then a password, then their details.

    A brand-new email gets an account and a verification code (allauth's sign-up). An account the organisers
    already made, with no password of its own yet, gets a code to set one (allauth's reset by code), so it
    is the same three steps for them. Someone who already has a password is told to sign in instead.
    """
    if not routes["email_code"]:
        messages.error(request, "New registrations are paused for a few minutes. Please try again shortly.")
        return None
    email = normalise_email(form.cleaned_data["email"])
    person = find_account(email)
    if person is not None and has_own_password(person):
        messages.error(request, "You already have an account with this email. Sign in with your password, "
                                "or use \"Forgot password\" if you don't remember it.")
        return redirect(_with_next("signin", _safe_next(request, form.cleaned_data.get("next"))))
    post = request.POST.copy()
    post["email"] = email  # the code goes to the address they typed, which is theirs
    post["next"] = _safe_next(request, form.cleaned_data.get("next"))
    request.POST = post
    if person is None:
        return SignupView.as_view()(request)
    return PasswordResetView.as_view()(request)


@never_cache
def signin(request):
    """Two choices: sign in (email and password, no code) or a new registration (email, code, password, details)."""
    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next"))
    if request.user.is_authenticated:
        return redirect(next_url or _staff_landing(request.user))
    routes = signin_routes(request)
    mode = "register" if (request.POST.get("mode") or request.GET.get("mode")) == "register" else "signin"
    posted = request.method == "POST"
    signin_form = SignInForm(request.POST if posted and mode == "signin" else None, initial={"next": next_url})
    register_form = RegisterForm(request.POST if posted and mode == "register" else None, initial={"next": next_url})
    if posted and mode == "signin" and signin_form.is_valid():
        response = _password_signin(request, signin_form)
        if response:
            return response
    elif posted and mode == "register" and register_form.is_valid():
        response = _register(request, register_form, routes)
        if response:
            return response
    return render(request, "people/signin.html", {
        "mode": mode, "signin_form": signin_form, "register_form": register_form, "routes": routes, "next": next_url,
    })


@never_cache
def staff_signin(request):
    """The old faculty sign-in address: the one sign-in page now takes passwords from everyone."""
    if request.method == "POST":
        return signin(request)
    query = request.GET.urlencode()
    return redirect(reverse("signin") + (f"?{query}" if query else ""))


@login_required
def profile(request):
    """Your details. The first time after a new registration: "Are you a student or faculty?", then only those details."""
    person = request.user
    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next"))
    first_time = not person.profile_complete
    choosing = (
        first_time and person.source == Person.Source.SIGNUP and person.kind == Person.Kind.STUDENT
        and not person.is_faculty and not person.faculty_requests.exists()
    )
    chosen = request.POST.get("as") or request.GET.get("as")
    if choosing and chosen not in ("student", "faculty"):
        return render(request, "people/choose_type.html", {"next": next_url})
    if choosing and chosen == "faculty":
        return _faculty_registration(request, person, next_url)
    form = ProfileForm(request.POST or None, instance=person)
    if request.method == "POST" and form.is_valid():
        person = form.save(commit=False)
        if not person.profile_completed_at:
            person.profile_completed_at = timezone.now()
        person.save()
        messages.success(request, "Profile saved.")
        return redirect(next_url or _staff_landing(person))
    verified_emails = EmailAddress.objects.filter(user=person, verified=True)
    return render(
        request,
        "people/profile.html",
        {
            "form": form,
            "next": next_url,
            "first_time": first_time,
            "choosing": choosing,
            "kl_domains": settings.KL_EMAIL_DOMAINS,
            "has_kl_email": any(is_kl_email(a.email) for a in verified_emails),
        },
    )


def _faculty_registration(request, person, next_url):
    """Faculty details at registration. The account stays a student account until an admin approves the request."""
    from hackathon import services as hackathon_services
    from hackathon.forms import FacultyRequestForm

    form = FacultyRequestForm(request.POST or None, initial={"full_name": person.full_name, "phone": person.phone,
                                                             "department": person.branch})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                person.full_name = " ".join(data["full_name"].split())
                person.phone = data["phone"]
                person.branch = data["department"]
                person.designation = data["designation"]
                person.profile_completed_at = timezone.now()
                person.save()
                hackathon_services.request_faculty(person, data)
        except (hackathon_services.RequestError, IntegrityError):
            messages.info(request, "Your faculty request is already waiting for an admin.")
        else:
            messages.success(request, "Registered. An admin checks every faculty request; you'll get an email when they decide.")
        return redirect(next_url or "faculty_register")
    return render(request, "people/profile.html", {
        "form": form, "next": next_url, "first_time": True, "choosing": True, "as_faculty": True,
        "kl_domains": settings.KL_EMAIL_DOMAINS,
    })


@never_cache
@login_required
def password_change(request):
    """Choose your own password: after a new registration's code, after signing in with a temporary one, or any time."""
    person = request.user
    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next"))
    changing = has_own_password(person)  # a normal change asks for the current password first
    form = (PasswordChangeForm if changing else SetPasswordForm)(person, request.POST or None)
    for field in form.fields.values():
        field.widget.attrs["class"] = "form-control form-control-lg"
    if request.method == "POST" and form.is_valid():
        person = form.save(commit=False)
        person.must_change_password = False
        person.temp_password_expires_at = None
        person.failed_password_signins = 0
        person.save()
        update_session_auth_hash(request, person)
        messages.success(request, "Password saved. Use it with your email next time you sign in.")
        return _onward(person, next_url)
    return render(request, "people/password_change.html", {
        "form": form, "forced": person.must_change_password, "first": not person.has_usable_password(),
        "changing": changing, "next": next_url,
    })
