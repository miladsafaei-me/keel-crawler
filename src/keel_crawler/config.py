"""Host configuration surface for keel-crawler.

A consuming project configures keel-crawler through a ``KEEL_CRAWLER`` settings
dict; every key is optional and the defaults make the package work standalone::

    KEEL_CRAWLER = {
        # Reuse an existing HTTP-cache table so adoption needs no data migration
        # (only a metadata-level AlterModelTable). Omit for a fresh project.
        "http_cache_db_table": "core_crawl_http_cache",
        # Set True ONLY when adopting a host's pre-existing cache table: the
        # initial migration then records model state without emitting CREATE TABLE.
        # Default False -> a fresh project's initial migration creates the table.
        "adopt_existing": True,
        # Default response-cache lifetime (seconds) when a fetcher does not override.
        "cache_ttl_seconds": 86_400,
        # User-Agent strings. Keep the bot UAs polite + attributable; the browser UA
        # is what dual-UA HTML fetches fall back to. A host brands these so cache
        # rows stay attributable to it, not to keel-crawler.
        "user_agent_text": "MyAppCrawlBot/1.0 (+https://myapp.example; fetch)",
        "user_agent_html": "MyAppCrawlBot/1.0 (+https://myapp.example; html)",
        "browser_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
    }
"""
from django.conf import settings

_DEFAULTS = {
    "http_cache_db_table": "keel_crawler_http_cache",
    "adopt_existing": False,
    "cache_ttl_seconds": 86_400,
    "user_agent_text": "KeelCrawlBot/1.0 (+https://github.com/miladsafaei-me/keel-crawler; fetch)",
    "user_agent_html": "KeelCrawlBot/1.0 (+https://github.com/miladsafaei-me/keel-crawler; html)",
    "browser_user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}


def crawler_setting(key):
    """Return ``KEEL_CRAWLER[key]`` or the package default."""
    return getattr(settings, "KEEL_CRAWLER", {}).get(key, _DEFAULTS[key])
