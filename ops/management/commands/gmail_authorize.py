"""Get the Gmail API refresh token once, with the sending account's approval.

    python manage.py gmail_authorize

Needs GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (a "Desktop app" OAuth client). Prints a
link; after "Allow", the browser lands on a http://localhost page that doesn't load.
Paste that page's address back here and the command prints the refresh token.
"""

from urllib.parse import parse_qs, urlencode, urlparse

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ops.gmail import SCOPE, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_URI = "http://localhost"


class Command(BaseCommand):
    help = "Approve the Gmail account once and print GMAIL_REFRESH_TOKEN."

    def handle(self, *args, **options):
        if not settings.GMAIL_CLIENT_ID or not settings.GMAIL_CLIENT_SECRET:
            raise CommandError("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first.")
        query = urlencode(
            {
                "client_id": settings.GMAIL_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        self.stdout.write("1. Open this link, signed in as the sending Gmail account, and click Allow:\n")
        self.stdout.write(f"{AUTH_URL}?{query}\n")
        self.stdout.write("2. The browser then shows an error page at http://localhost/... That is expected.")
        pasted = input("   Copy that page's full address from the address bar and paste it here: ").strip()
        code = parse_qs(urlparse(pasted).query).get("code", [pasted])[0]
        if not code:
            raise CommandError("No code found in what was pasted.")
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GMAIL_CLIENT_ID,
                "client_secret": settings.GMAIL_CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        if not response.ok:
            raise CommandError(f"Google refused the code ({response.status_code}): {response.text[:300]}")
        token = response.json().get("refresh_token")
        if not token:
            raise CommandError("Google sent no refresh token. Remove the app's access in the Google account and try again.")
        self.stdout.write(self.style.SUCCESS("\nApproved. The refresh token is on the next line:"))
        self.stdout.write(token)
