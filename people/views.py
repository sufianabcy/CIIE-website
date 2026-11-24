from allauth.account.models import EmailAddress
from allauth.account.views import RequestLoginCodeView, SignupView
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from ops.models import SiteSettings

from .forms import ProfileForm, SignInForm
from .models import Person, is_kl_email, normalise_email

IN_APP_BROWSERS = ("Instagram", "FBAN", "FBAV", "WhatsApp", "Snapchat", "LinkedInApp", "Line/")


def _safe_next(request, url: str | None) -> str:
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return ""


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


def account_exists(email: str) -> bool:
    email = normalise_email(email)
    return (
        EmailAddress.objects.filter(email__iexact=email).exists()
        or Person.objects.filter(email__iexact=email).exists()
    )


@never_cache
def signin(request):
    """One email box; the portal picks the route (code to sign in, or a new account)."""
    if request.user.is_authenticated:
        return redirect(_safe_next(request, request.GET.get("next")) or "my_registrations")
    routes = signin_routes(request)
    form = SignInForm(request.POST or None, initial={"next": request.GET.get("next", "")})
    if request.method == "POST" and form.is_valid():
        if not routes["email_code"]:
            messages.error(request, "Sign-in by email code is switched off right now.")
        else:
            email = normalise_email(form.cleaned_data["email"])
            # Hand the same POST to allauth: an existing person gets a sign-in
            # code, a new email gets an account and a verification code.
            post = request.POST.copy()
            post["email"] = email
            post["next"] = _safe_next(request, form.cleaned_data.get("next"))
            request.POST = post
            if account_exists(email):
                return RequestLoginCodeView.as_view()(request)
            return SignupView.as_view()(request)
    return render(
        request,
        "people/signin.html",
        {"form": form, "routes": routes, "next": _safe_next(request, request.GET.get("next") or request.POST.get("next"))},
    )


@login_required
def profile(request):
    person = request.user
    form = ProfileForm(request.POST or None, instance=person)
    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next"))
    if request.method == "POST" and form.is_valid():
        person = form.save(commit=False)
        if not person.profile_completed_at:
            person.profile_completed_at = timezone.now()
        person.save()
        messages.success(request, "Profile saved.")
        return redirect(next_url or "my_registrations")
    verified_emails = EmailAddress.objects.filter(user=person, verified=True)
    return render(
        request,
        "people/profile.html",
        {
            "form": form,
            "next": next_url,
            "first_time": not person.profile_complete,
            "kl_domains": settings.KL_EMAIL_DOMAINS,
            "has_kl_email": any(is_kl_email(a.email) for a in verified_emails),
        },
    )
