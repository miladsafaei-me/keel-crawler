"""System check that turns a mis-shaped ``KEEL_CRAWLER`` setting into a clean
``manage.py check`` failure (which CI runs) instead of a latent runtime error.
"""
from django.conf import settings
from django.core.checks import Error, register

_ALLOWED_KEYS = {
    "http_cache_db_table",
    "adopt_existing",
    "cache_ttl_seconds",
    "user_agent_text",
    "user_agent_html",
    "browser_user_agent",
}


@register()
def check_keel_crawler_config(app_configs, **kwargs):
    errors = []
    cfg = getattr(settings, "KEEL_CRAWLER", None)
    if cfg is None:
        return errors
    if not isinstance(cfg, dict):
        errors.append(
            Error(
                "KEEL_CRAWLER must be a dict.",
                id="keel_crawler.E001",
            )
        )
        return errors
    unknown = set(cfg) - _ALLOWED_KEYS
    if unknown:
        errors.append(
            Error(
                f"KEEL_CRAWLER has unknown key(s): {sorted(unknown)}.",
                hint=f"Allowed keys: {sorted(_ALLOWED_KEYS)}.",
                id="keel_crawler.E002",
            )
        )
    return errors
