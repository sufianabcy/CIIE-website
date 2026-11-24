from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("signin/", views.signin, name="signin"),
    path("signin/password/", views.staff_signin, name="staff_signin"),
    path("me/profile/", views.profile, name="profile"),
    path("me/password/", views.password_change, name="password_change"),
    # allauth's own sign-in and sign-up pages are replaced by the one-box page.
    path("accounts/login/", RedirectView.as_view(pattern_name="signin", query_string=True)),
    path("accounts/signup/", RedirectView.as_view(pattern_name="signin", query_string=True)),
]
