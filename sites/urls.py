from django.urls import path

from . import views

app_name = "sites"

urlpatterns = [
    # Self-service site management for invite-provisioned trial accounts.
    path("add/", views.site_create, name="site_create"),
    path("<int:site_id>/edit/", views.site_edit, name="site_edit"),
    path("<int:site_id>/remove/", views.site_delete, name="site_delete"),
]
