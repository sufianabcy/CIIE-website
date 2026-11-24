from django.urls import path

from . import views

urlpatterns = [
    path("manage/<slug:slug>/judging/", views.dashboard, name="judging_dashboard"),
    path("manage/<slug:slug>/judging/faculty/", views.upload_faculty, name="judging_upload_faculty"),
    path("manage/<slug:slug>/judging/mapping/", views.upload_mapping, name="judging_upload_mapping"),
    path("manage/<slug:slug>/judging/auto-map/", views.auto_assign, name="judging_auto_assign"),
    path("manage/<slug:slug>/judging/logins/", views.issue_credentials, name="judging_issue_credentials"),
    path("manage/<slug:slug>/judging/template/<slug:kind>/", views.template, name="judging_template"),
    path("faculty/", views.signin, name="judge_signin"),
    path("faculty/students/", views.home, name="judge_home"),
    path("faculty/students/<int:assignment_id>/", views.mark, name="judge_mark"),
    path("faculty/sign-out/", views.signout, name="judge_signout"),
]
