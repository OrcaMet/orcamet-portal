from django.apps import AppConfig


class SitesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sites"
    verbose_name = "Client Sites"

    def ready(self):
        import sites.signals  # noqa — registers the post_save signal
