from django.urls import path
from . import views

app_name = "forecasts"

urlpatterns = [
    path("risk-map/", views.risk_map_detail, name="risk_map_detail"),
]
