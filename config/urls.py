from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path
from django.views.generic import RedirectView

admin.site.site_header = "KL-CIIE event portal"
admin.site.site_title = "KL-CIIE admin"
admin.site.index_title = "Admin home"
# No passwords: the admin's own login page sends people to the one-box sign-in.
admin.site.login = login_required(admin.site.login)

from ops.admin_home import install as install_admin_home  # noqa: E402

install_admin_home()

urlpatterns = [
    path("admin/sql/", RedirectView.as_view(pattern_name="admin:sqlconsole_sqlrun_changelist")),
    path("admin/", admin.site.urls),
    # people.urls comes first so the one-box sign-in replaces allauth's pages.
    path("", include("people.urls")),
    path("accounts/", include("allauth.urls")),
    path("", include("events.urls")),
    path("", include("registrations.urls")),
    path("door/", include("checkin.urls")),
    path("ops/", include("ops.urls")),
    path("", include("hackathon.urls")),
]
