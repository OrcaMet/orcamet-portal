from django.urls import path

from . import views

app_name = "sites"

urlpatterns = [
    # Self-service site management, for clients whose workspace was granted
    # it by the invite that provisioned it (Client.self_service_sites).
    path("add/", views.site_create, name="site_create"),
    path("import/", views.site_import, name="site_import"),
    path("import/confirm/", views.site_import_confirm, name="site_import_confirm"),
    path("<int:site_id>/edit/", views.site_edit, name="site_edit"),
    path("<int:site_id>/remove/", views.site_delete, name="site_delete"),
]
