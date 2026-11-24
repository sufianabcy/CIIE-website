from django.urls import path

from checkin.views import healthz

from . import views

urlpatterns = [
    path("status/", views.status, name="ops_status"),
    path("tick/", views.tick, name="ops_tick"),
    path("healthz/", healthz, name="healthz"),
]
