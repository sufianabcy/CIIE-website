from django.urls import path

from . import views

urlpatterns = [
    path("<slug:slug>/", views.door, name="door"),
    path("<slug:slug>/scan/", views.scan, name="door_scan"),
    path("<slug:slug>/found/<int:registration_id>/", views.check_in_found, name="door_check_in_found"),
    path("<slug:slug>/undo/<int:checkin_id>/", views.undo, name="door_undo"),
    path("<slug:slug>/walk-in/", views.walk_in, name="door_walk_in"),
    path("<slug:slug>/counts/", views.counts, name="door_counts"),
    path("<slug:slug>/list/", views.door_list, name="door_list"),
    path("<slug:slug>/join/<str:token>/", views.join_door_team, name="door_join"),
]
