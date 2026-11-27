"""Send email through the Gmail API over HTTPS.

Used when GMAIL_REFRESH_TOKEN is set. Some hosts can't sign in to Gmail's SMTP
servers (Google closes the connection at login), but the HTTPS API still works.
`manage.py gmail_authorize` gets the refresh token once, with the account's approval.
"""

import base64
import threading
import time

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPE = "https://www.googleapis.com/auth/gmail.send"

_lock = threading.Lock()
_cached = {"token": "", "expires": 0.0}


class GmailAPIError(requests.RequestException):
    """Google refused a request; the message carries Google's own reason."""


def _raise_for(response, what):
    if response.ok:
        return
    try:
        body = response.json()
        detail = body.get("error_description") or body.get("error", {}).get("message") or body.get("error")
    except ValueError:
        detail = response.text[:300]
    raise GmailAPIError(f"{what} failed ({response.status_code}): {detail}")


def access_token(force_refresh=False):
    with _lock:
        if not force_refresh and _cached["token"] and _cached["expires"] > time.time() + 60:
            return _cached["token"]
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": settings.GMAIL_CLIENT_ID,
                "client_secret": settings.GMAIL_CLIENT_SECRET,
                "refresh_token": settings.GMAIL_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        _raise_for(response, "Gmail sign-in")
        data = response.json()
        _cached["token"] = data["access_token"]
        _cached["expires"] = time.time() + int(data.get("expires_in", 3600))
        return _cached["token"]


def raw_message(message):
    mime = message.message()
    if message.bcc:
        # message() leaves Bcc out; Gmail reads recipients from the headers and strips Bcc itself.
        mime["Bcc"] = ", ".join(message.bcc)
    return base64.urlsafe_b64encode(mime.as_bytes(linesep="\r\n")).decode("ascii")


class GmailAPIBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            try:
                self._send(message)
            except Exception:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent

    def _send(self, message):
        payload = {"raw": raw_message(message)}
        response = requests.post(SEND_URL, json=payload, headers=self._headers(), timeout=30)
        if response.status_code == 401:
            response = requests.post(SEND_URL, json=payload, headers=self._headers(force_refresh=True), timeout=30)
        _raise_for(response, "Gmail send")

    @staticmethod
    def _headers(force_refresh=False):
        return {"Authorization": f"Bearer {access_token(force_refresh)}"}
