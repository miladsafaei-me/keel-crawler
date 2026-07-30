"""Host configuration surface for keel-crawler.

A consuming project configures keel-crawler through a ``KEEL_CRAWLER`` settings
dict; every key is optional and the defaults make the package work standalone.
Secrets that are usually deployed via ``.env`` (the proxy URL, Mihomo API creds)
also fall back to environment variables — see :mod:`keel_crawler.proxy`.

    KEEL_CRAWLER = {
        # --- Layer 0: fetch cache ---
        "http_cache_db_table": "core_crawl_http_cache",   # adopt an existing table
        "adopt_existing": True,                            # state-only initial migration
        "cache_ttl_seconds": 86_400,
        "user_agent_text": "MyAppCrawlBot/1.0 (+https://myapp.example; fetch)",
        "user_agent_html": "MyAppCrawlBot/1.0 (+https://myapp.example; html)",
        "browser_user_agent": "Mozilla/5.0 (...) Chrome/122.0.0.0 Safari/537.36",

        # --- Layer 1: proxy + anti-bot ---
        "proxy_url": "http://127.0.0.1:7890",   # or env LOCAL_PROXY_URL
        "proxy_scoring_enabled": True,          # master switch for score persistence
        "proxy_scores_dir": None,               # dir for the JSON score files (None -> XDG)
        "mihomo": {                             # empty/None -> proxy rotation disabled
            "api_url": "http://127.0.0.1:9090", # or env MIHOMO_API_URL
            "secret": "...",                    # or env MIHOMO_SECRET
            "group": "AUTO",                    # or env MIHOMO_PROXY_GROUP
        },
        "browser_headless": True,
        "browser_channel": "",                  # Playwright channel (e.g. "chrome")
        "captcha_solver": None,                 # dotted path: (url, page) -> CrawledPage | None

        # --- Layer 3: orchestration ---
        "crawl_job_db_table": "keel_crawler_crawl_job",

        # --- Layer 4: RSS source monitoring ---
        "rss": {
            "triage_hook": "myapp.news.triage",  # dotted path; LLM selection lives in keel-content
            "allow_keywords": [],
            "deny_keywords": [],
            "recency_hours": 72,
            "max_items_per_feed": 50,
        },
    }
"""
from django.conf import settings

_DEFAULTS = {
    # Layer 0
    "http_cache_db_table": "keel_crawler_http_cache",
    "adopt_existing": False,
    "cache_ttl_seconds": 86_400,
    "user_agent_text": "KeelCrawlBot/1.0 (+https://github.com/miladsafaei-me/keel-crawler; fetch)",
    "user_agent_html": "KeelCrawlBot/1.0 (+https://github.com/miladsafaei-me/keel-crawler; html)",
    "browser_user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    # Layer 1
    "proxy_url": None,
    "proxy_scoring_enabled": True,
    "proxy_scores_dir": None,
    "mihomo": {},
    "browser_headless": True,
    "browser_channel": "",
    "captcha_solver": None,
    # Layer 3
    "crawl_job_db_table": "keel_crawler_crawl_job",
    # Layer 4
    "rss": {},
}

# Keys whose default is a dict; ``crawler_subconfig`` returns a copy so callers
# never mutate the shared default.
_DICT_KEYS = {"mihomo", "rss"}

_RSS_DEFAULTS = {
    "triage_hook": None,
    "allow_keywords": [],
    "deny_keywords": [],
    "recency_hours": 72,
    "max_items_per_feed": 50,
}


def crawler_setting(key):
    """Return ``KEEL_CRAWLER[key]`` or the package default."""
    return getattr(settings, "KEEL_CRAWLER", {}).get(key, _DEFAULTS[key])


def crawler_subconfig(key):
    """Return a dict-valued setting (``mihomo`` / ``rss``) as a plain dict copy."""
    if key not in _DICT_KEYS:
        raise KeyError(f"{key!r} is not a dict-valued KEEL_CRAWLER key")
    val = getattr(settings, "KEEL_CRAWLER", {}).get(key)
    return dict(val) if isinstance(val, dict) else {}


def rss_setting(key):
    """Return ``KEEL_CRAWLER['rss'][key]`` or the RSS default."""
    return crawler_subconfig("rss").get(key, _RSS_DEFAULTS[key])
