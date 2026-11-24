from django.shortcuts import redirect
from django.urls import reverse


class PasswordChangeRequiredMiddleware:
    """Someone signed in with a temporary password must choose their own before any other page works."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and getattr(user, "must_change_password", False):
            allowed = {reverse("password_change"), reverse("account_logout")}
            if request.path not in allowed and not request.path.startswith(("/static/", "/ops/healthz/")):
                return redirect("password_change")
        return self.get_response(request)
