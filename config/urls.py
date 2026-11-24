from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path

admin.site.site_header = "KL-CIIE event portal"
admin.site.site_title = "KL-CIIE admin"
admin.site.index_title = "Run events"
# No passwords: the admin's own login page sends people to the one-box sign-in.
admin.site.login = login_required(admin.site.login)

urlpatterns = [
    path("admin/", admin.site.urls),
    # people.urls comes first so the one-box sign-in replaces allauth's pages.
    path("", include("people.urls")),
    path("accounts/", include("allauth.urls")),
    path("", include("events.urls")),
    path("", include("registrations.urls")),
    path("door/", include("checkin.urls")),
    path("ops/", include("ops.urls")),
    path("", include("judging.urls")),
]
