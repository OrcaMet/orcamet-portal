from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import Invite, User


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


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    """Create and revoke the signup links handed to trial users."""

    list_display = ("__str__", "signup_link", "status", "uses", "max_uses", "expires_at")
    list_filter = ("is_active",)
    search_fields = ("label", "code")
    readonly_fields = ("code", "uses", "created_at", "created_by", "signup_link")

    fieldsets = (
        (None, {
            "fields": ("label", "signup_link", "code"),
        }),
        ("Limits", {
            "fields": ("is_active", "max_uses", "uses", "expires_at"),
            "description": (
                "Untick 'is active' to revoke a link immediately. "
                "Set max uses to 0 for an unlimited link."
            ),
        }),
        ("Audit", {
            "fields": ("created_at", "created_by"),
        }),
    )

    @admin.display(description="Signup link")
    def signup_link(self, obj):
        """
        The path to send someone.

        Relative rather than absolute: a ModelAdmin is a single shared
        instance across worker threads, so stashing the request on `self` to
        build an origin would let concurrent admin requests read each other's
        host. save_model messages the full URL instead, where the request is
        in hand.
        """
        if not obj.pk:
            return "Save to generate the link."
        path = f"/signup/?invite={obj.code}"
        return format_html('<a href="{}">{}</a>', path, path)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        if not change:
            url = request.build_absolute_uri(f"/signup/?invite={obj.code}")
            messages.info(request, f"Send this link: {url}")
