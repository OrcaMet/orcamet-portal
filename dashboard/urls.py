from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("site/<int:site_id>/", views.site_detail, name="site_detail"),
]
