from django.apps import AppConfig


class KeelCrawlerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "keel_crawler"
    verbose_name = "Keel Crawler — fetch, clean, monitor"

    def ready(self):
        from . import checks  # noqa: F401  (register the config system check)
