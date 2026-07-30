"""System check that turns a mis-shaped ``KEEL_CRAWLER`` setting into a clean
``manage.py check`` failure (which CI runs) instead of a latent runtime error.
"""
from django.conf import settings
from django.core.checks import Error, register

_ALLOWED_KEYS = {
    # Layer 0
    "http_cache_db_table",
    "adopt_existing",
    "cache_ttl_seconds",
    "user_agent_text",
    "user_agent_html",
    "browser_user_agent",
    # Layer 1
    "proxy_url",
    "proxy_scoring_enabled",
    "proxy_scores_dir",
    "mihomo",
    "browser_headless",
    "browser_channel",
    "captcha_solver",
    # Layer 3
    "crawl_job_db_table",
    # Layer 4
    "rss",
}

_ALLOWED_RSS_KEYS = {
    "triage_hook",
    "allow_keywords",
    "deny_keywords",
    "recency_hours",
    "max_items_per_feed",
}


@register()
def check_keel_crawler_config(app_configs, **kwargs):
    errors = []
    cfg = getattr(settings, "KEEL_CRAWLER", None)
    if cfg is None:
        return errors
    if not isinstance(cfg, dict):
        errors.append(Error("KEEL_CRAWLER must be a dict.", id="keel_crawler.E001"))
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
    for dict_key in ("mihomo", "rss"):
        if dict_key in cfg and not isinstance(cfg[dict_key], dict):
            errors.append(
                Error(f"KEEL_CRAWLER['{dict_key}'] must be a dict.", id="keel_crawler.E003")
            )
    rss = cfg.get("rss")
    if isinstance(rss, dict):
        unknown_rss = set(rss) - _ALLOWED_RSS_KEYS
        if unknown_rss:
            errors.append(
                Error(
                    f"KEEL_CRAWLER['rss'] has unknown key(s): {sorted(unknown_rss)}.",
                    hint=f"Allowed keys: {sorted(_ALLOWED_RSS_KEYS)}.",
                    id="keel_crawler.E004",
                )
            )
    return errors
