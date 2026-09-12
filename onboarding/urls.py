from django.urls import path

from . import views

app_name = "onboarding"

urlpatterns = [
    path("", views.start, name="start"),
    path("welcome/", views.welcome, name="welcome"),
    path("operation/", views.operation, name="operation"),
    path("sites/", views.sites, name="sites"),
    path("thresholds/", views.thresholds, name="thresholds"),
    path("done/", views.done, name="done"),
    path("skip/", views.skip, name="skip"),
]
