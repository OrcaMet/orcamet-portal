from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("map/", views.weather_map, name="weather_map"),
    path("map/sites.json", views.map_sites_json, name="map_sites_json"),
    path("site/<int:site_id>/", views.site_detail, name="site_detail"),
    path("site/<int:site_id>/chart-data/", views.forecast_chart_data, name="chart_data"),
]
