import base64
from email import message_from_bytes

import pytest
from django.core.mail import EmailMessage

from ops import gmail


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture
def google(monkeypatch, settings):
    settings.GMAIL_CLIENT_ID = "client"
    settings.GMAIL_CLIENT_SECRET = "secret"
    settings.GMAIL_REFRESH_TOKEN = "refresh"
    monkeypatch.setitem(gmail._cached, "token", "")
    monkeypatch.setitem(gmail._cached, "expires", 0.0)
    calls = []
    replies = {"send": [], "token": []}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        kind = "token" if url == gmail.TOKEN_URL else "send"
        queue = replies[kind]
        if queue:
            return queue.pop(0)
        if kind == "token":
            return FakeResponse(200, {"access_token": f"access-{len(calls)}", "expires_in": 3600})
        return FakeResponse(200, {"id": "abc"})

    monkeypatch.setattr(gmail.requests, "post", post)
    return calls, replies


def sent_mime(call):
    raw = call[1]["json"]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw))


def test_sends_raw_message_with_bearer_token(google):
    calls, _ = google
    message = EmailMessage("Hello", "Body text", "KL-CIIE <klciie@example.com>", ["a@example.com"], bcc=["b@example.com"])
    assert gmail.GmailAPIBackend().send_messages([message]) == 1
    token_call, send_call = calls
    assert token_call[1]["data"]["refresh_token"] == "refresh"
    assert send_call[0] == gmail.SEND_URL
    assert send_call[1]["headers"]["Authorization"] == "Bearer access-1"
    mime = sent_mime(send_call)
    assert mime["To"] == "a@example.com"
    assert mime["Bcc"] == "b@example.com"
    assert mime["Subject"] == "Hello"


def test_reuses_token_until_it_expires(google):
    calls, _ = google
    backend = gmail.GmailAPIBackend()
    backend.send_messages([EmailMessage("One", "x", "f@example.com", ["a@example.com"])])
    backend.send_messages([EmailMessage("Two", "x", "f@example.com", ["a@example.com"])])
    assert [url for url, _ in calls].count(gmail.TOKEN_URL) == 1


def test_refreshes_token_once_on_401(google):
    calls, replies = google
    replies["send"].append(FakeResponse(401, {"error": {"message": "expired"}}))
    assert gmail.GmailAPIBackend().send_messages([EmailMessage("Hi", "x", "f@example.com", ["a@example.com"])]) == 1
    assert [url for url, _ in calls].count(gmail.TOKEN_URL) == 2


def test_google_refusal_is_reported(google):
    _, replies = google
    replies["token"].append(FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    with pytest.raises(gmail.GmailAPIError, match="expired or revoked"):
        gmail.GmailAPIBackend().send_messages([EmailMessage("Hi", "x", "f@example.com", ["a@example.com"])])
    # The test command catches OSError, so the reason reaches the terminal.
    assert issubclass(gmail.GmailAPIError, OSError)


def test_fail_silently_counts_nothing(google):
    _, replies = google
    replies["send"].append(FakeResponse(500, {"error": {"message": "backend error"}}))
    backend = gmail.GmailAPIBackend(fail_silently=True)
    assert backend.send_messages([EmailMessage("Hi", "x", "f@example.com", ["a@example.com"])]) == 0
