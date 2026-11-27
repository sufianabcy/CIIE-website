import base64
import json
import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from mailer.services import enqueue_email
from ops.models import SiteSettings

log = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    def send_mail(self, template_prefix, email, context):
        """Route allauth's mail (codes) through the outbox at top priority."""
        # allauth's own send_mail adds current_site; without it the emails read "Hello from !".
        context = {"email": email, "current_site": _club_site(), **context}
        msg = self.render_mail(template_prefix, email, context)
        html = None
        for content, mimetype in getattr(msg, "alternatives", []) or []:
            if mimetype == "text/html":
                html = content
        enqueue_email(
            to=email,
            subject=msg.subject,
            text=msg.body,
            html=html,
            kind="code",
            send_now=True,
        )

    def is_open_for_signup(self, request):
        return SiteSettings.load().signin_email_code_enabled

    def get_login_redirect_url(self, request):
        # First sign-in goes through the profile step (certificate name etc.).
        if request.user.is_authenticated and not request.user.profile_complete:
            return reverse("profile")
        if request.user.is_authenticated:
            from .views import _staff_landing

            return reverse(_staff_landing(request.user))
        return super().get_login_redirect_url(request)

    def get_signup_redirect_url(self, request):
        return reverse("profile")


def _club_site():
    from types import SimpleNamespace
    from urllib.parse import urlparse

    return SimpleNamespace(name=settings.CLUB_NAME, domain=urlparse(settings.SITE_URL).netloc)


def _jwt_claims(token: str) -> dict:
    """Read the claims of a token received directly from Microsoft over TLS."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return self._route_enabled(sociallogin.account.provider)

    def _route_enabled(self, provider: str) -> bool:
        site = SiteSettings.load()
        return {
            "google": site.signin_google_enabled,
            "microsoft": site.signin_microsoft_enabled,
        }.get(provider, False)

    def pre_social_login(self, request, sociallogin):
        provider = sociallogin.account.provider
        if not self._route_enabled(provider):
            messages.info(request, "That sign-in route is switched off. Use your email instead.")
            raise ImmediateHttpResponse(redirect("signin"))
        if provider == "microsoft":
            claims = _jwt_claims(getattr(sociallogin.token, "token", "") or "")
            tid = claims.get("tid")
            sociallogin.account.extra_data["tid"] = tid
            expected = settings.KL_MICROSOFT_TENANT_ID
            if not expected or tid != expected:
                log.warning("Microsoft sign-in from tenant %s refused", tid)
                messages.error(
                    request,
                    "Microsoft sign-in is for KL accounts only. Use your email instead.",
                )
                raise ImmediateHttpResponse(redirect("signin"))
            # KL's own directory vouches for the address.
            for address in sociallogin.email_addresses:
                address.verified = True
