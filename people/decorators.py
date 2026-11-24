from functools import wraps
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import reverse


def profile_required(view):
    """Signed in, with the profile step done; otherwise send them there and back."""

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.profile_complete:
            url = reverse("profile") + "?" + urlencode({"next": request.get_full_path()})
            return redirect(url)
        return view(request, *args, **kwargs)

    return wrapper
