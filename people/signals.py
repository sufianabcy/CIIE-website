"""Keep KL status in step with whatever the person has proven."""

from allauth.account.models import EmailAddress
from allauth.account.signals import email_confirmed, user_logged_in
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.dispatch import receiver

from .models import is_kl_email


def refresh_kl_status(person) -> None:
    if person.is_kl_verified:
        return
    for address in EmailAddress.objects.filter(user=person, verified=True):
        if is_kl_email(address.email):
            person.mark_kl_verified(address.email)
            return
    # A Microsoft sign-in counts only when it came from KL's own tenant.
    tenant = settings.KL_MICROSOFT_TENANT_ID
    if tenant:
        for account in SocialAccount.objects.filter(user=person, provider="microsoft"):
            data = account.extra_data or {}
            email = data.get("mail") or data.get("userPrincipalName")
            if data.get("tid") == tenant and is_kl_email(email):
                person.mark_kl_verified(email)
                return


@receiver(email_confirmed)
def on_email_confirmed(request, email_address, **kwargs):
    refresh_kl_status(email_address.user)


@receiver(user_logged_in)
def on_logged_in(request, user, **kwargs):
    refresh_kl_status(user)
