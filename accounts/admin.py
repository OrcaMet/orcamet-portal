from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Custom admin for the OrcaMet User model."""

    list_display = ("username", "email", "role", "client", "is_active")
    list_filter = ("role", "client", "is_active")
    search_fields = ("username", "email", "first_name", "last_name")

    fieldsets = BaseUserAdmin.fieldsets + (
        ("OrcaMet", {
            "fields": ("role", "auth0_id", "client"),
        }),
    )

    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("OrcaMet", {
            "fields": ("role", "client"),
        }),
    )
