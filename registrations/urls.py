from django.urls import path

from . import views

urlpatterns = [
    path("e/<slug:slug>/register/", views.register_view, name="register"),
    path("me/", views.my_registrations, name="my_registrations"),
    path("pass/<str:code>/", views.pass_view, name="pass"),
    path("pass/<str:code>/cancel/", views.cancel_view, name="cancel_registration"),
]
