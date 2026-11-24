from django.conf import settings

from .models import SiteSettings


def portal(request):
    site = getattr(request, "_site_settings", None)
    if site is None:
        site = SiteSettings.load()
        request._site_settings = site
    return {
        "site": site,
        "CLUB_NAME": settings.CLUB_NAME,
        "CLUB_CONTACT_EMAIL": settings.CLUB_CONTACT_EMAIL,
    }
