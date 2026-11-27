from django.urls import path
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("events/", views.event_list, name="event_list"),
    path("privacy/", TemplateView.as_view(template_name="pages/privacy.html"), name="privacy"),
    path("terms/", TemplateView.as_view(template_name="pages/terms.html"), name="terms"),
    path("e/<slug:slug>/", views.event_detail, name="event_detail"),
    path("manage/", views.manage, name="manage"),
    path("manage/<slug:slug>/report/", views.report, name="event_report"),
    path("manage/<slug:slug>/registration/", views.toggle_registration, name="event_toggle_registration"),
]

from . import exports  # noqa: E402

urlpatterns += [
    path("manage/<slug:slug>/export/<slug:kind>/", exports.export, name="event_export"),
]
