from django.urls import path

from . import views

urlpatterns = [
    path("manage/<slug:slug>/hackathon/", views.dashboard, name="hackathon_dashboard"),
    path("manage/<slug:slug>/hackathon/upload/<slug:kind>/", views.upload, name="hackathon_upload"),
    path("manage/<slug:slug>/hackathon/template/<slug:kind>/", views.template, name="hackathon_template"),
    path("manage/<slug:slug>/hackathon/allocate/", views.allocate, name="hackathon_allocate"),
    path("manage/<slug:slug>/hackathon/rounds/<int:pk>/<slug:action>/", views.round_action, name="hackathon_round_action"),
    path("manage/<slug:slug>/hackathon/teams/<int:pk>/room/", views.move_team, name="hackathon_move_team"),
    path("manage/<slug:slug>/hackathon/solo-teams/", views.solo_teams, name="hackathon_solo_teams"),
    path("manage/<slug:slug>/hackathon/logins/", views.logins, name="hackathon_logins"),
    path("manage/<slug:slug>/hackathon/marks/", views.marks, name="hackathon_marks"),
    path("me/hackathon/<slug:slug>/", views.my_hackathon, name="my_hackathon"),
    path("manage/faculty-requests/", views.faculty_requests, name="faculty_requests"),
    path("faculty/register/", views.faculty_register, name="faculty_register"),
    path("faculty/", views.staff_home, name="staff_home"),
    path("faculty/<slug:slug>/", views.judge_event, name="hackathon_judge_event"),
    path("faculty/marks/<int:pk>/", views.mark, name="hackathon_mark"),
]
