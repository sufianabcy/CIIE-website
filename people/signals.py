"""Keep KL status, and whether someone still has to choose a password, in step with what the person has done."""

from allauth.account.models import EmailAddress
from allauth.account.signals import email_confirmed, password_reset, user_logged_in, user_signed_up
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


@receiver(user_signed_up)
def on_signed_up(request, user, **kwargs):
    """A new registration by email chooses a password straight after the code (social sign-ups don't need one)."""
    if kwargs.get("sociallogin") is None and not user.has_usable_password():
        type(user).objects.filter(pk=user.pk).update(must_change_password=True)
        user.must_change_password = True


@receiver(password_reset)
def on_password_reset(request, user, **kwargs):
    """A password set by emailed code is their own: no temporary password left, and any lockout is lifted."""
    type(user).objects.filter(pk=user.pk).update(must_change_password=False, temp_password_expires_at=None, failed_password_signins=0)
    user.must_change_password = False
    user.temp_password_expires_at = None
    user.failed_password_signins = 0
