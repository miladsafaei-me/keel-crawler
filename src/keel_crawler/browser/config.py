"""crawl4ai ``BrowserConfig`` / ``CrawlerRunConfig`` builders + UA rotation.

Extracted from Revenika's ``default_browser_config`` / ``default_crawler_run_config``
and made business-blind: the forex-specific ``forex_deep_prune`` flag became a
neutral ``profile`` ("content" prunes page chrome, "raw" keeps everything,
"link_harvest" is tuned for menu/link discovery), and every tuning knob is a plain
argument instead of a ``CRAWL_*`` env read. crawl4ai is imported lazily so importing
this module never requires the ``[browser]`` extra.
"""
from __future__ import annotations

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

# Poll until Cloudflare's JS challenge clears (aligns with crawl4ai's Tier-1 signal).
CLOUDFLARE_WAIT_FOR = (
    "js:() => {"
    "const h=document.documentElement?.innerHTML||'';"
    "if(/cdn-cgi\\/challenge-platform\\/\\S+orchestrate/i.test(h))return false;"
    "const t=(document.title||'').toLowerCase();"
    "if(t.includes('just a moment')||t.includes('checking your browser'))return false;"
    "return true;"
    "}"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Page chrome dropped by the "content" profile to save extraction tokens.
_CONTENT_EXCLUDED_TAGS: tuple[str, ...] = (
    "nav", "footer", "aside", "header", "script", "style", "iframe", "noscript",
)
_CONTENT_EXCLUDED_SELECTOR = (
    ".advertisement, .advert, .adsbygoogle, [data-ad-slot], "
    "[class*='google-auto-placed'], [class*='ad-banner'], [id*='ad-container'], "
    "[class*='sponsored']"
)


def pick_user_agent(*, rotate: bool = True) -> str:
    """Rotate a desktop Chrome UA (major + build) per session to blur static fingerprints."""
    if not rotate:
        return DEFAULT_USER_AGENT
    chrome_major = random.choice((118, 120, 121, 124, 126, 128, 130, 131, 133))
    build = random.randint(5000, 7500)
    os_line = random.choice(
        (
            "Windows NT 10.0; Win64; x64",
            "Windows NT 10.0; Win64; x64",
            "Macintosh; Intel Mac OS X 10_15_7",
        )
    )
    return (
        f"Mozilla/5.0 ({os_line}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.{build}.0 Safari/537.36"
    )


def build_browser_config(
    *,
    proxy_url: str | None = None,
    user_agent: str | None = None,
    rotate_user_agent: bool = True,
    headless: bool = True,
    channel: str = "",
    viewport_width: int = 1366,
    viewport_height: int = 768,
    stealth: bool = True,
) -> Any:
    """Build a crawl4ai ``BrowserConfig``. Passing ``proxy_url`` routes Chromium through it."""
    from crawl4ai import BrowserConfig

    kwargs: dict[str, Any] = {
        "headless": headless,
        "verbose": False,
        "java_script_enabled": True,
        "user_agent": user_agent or pick_user_agent(rotate=rotate_user_agent),
        "enable_stealth": stealth,
        "ignore_https_errors": True,
        "viewport_width": max(400, int(viewport_width)),
        "viewport_height": max(400, int(viewport_height)),
    }
    if channel:
        kwargs["channel"] = channel
        kwargs["chrome_channel"] = channel
    if proxy_url:
        kwargs["proxy"] = proxy_url
        logger.info("keel-crawler browser: WITH PROXY")
    else:
        logger.info("keel-crawler browser: DIRECT")
    return BrowserConfig(**kwargs)


def build_run_config(
    page_timeout_sec: int,
    *,
    profile: str = "content",
    delay_before_return_html: float = 0.25,
    post_challenge_delay_sec: float = 1.25,
    cloudflare_wait: bool = True,
    wait_until: str = "load",
    locale: str = "en-US",
    timezone_id: str = "UTC",
    simulate_user: bool = True,
    override_navigator: bool = True,
    max_retries: int = 1,
) -> Any:
    """Build a crawl4ai ``CrawlerRunConfig`` for a named ``profile``.

    * ``content`` — prune nav/footer/ads/images (token-lean article text).
    * ``raw`` — keep everything (no tag/selector exclusion).
    * ``link_harvest`` — full-page scan + overlay scrub disabled (for menu/link discovery).
    """
    from crawl4ai import CacheMode, CrawlerRunConfig

    effective_delay = delay_before_return_html
    if cloudflare_wait:
        effective_delay = max(effective_delay, max(0.0, min(post_challenge_delay_sec, 60.0)))

    kwargs: dict[str, Any] = {
        "cache_mode": CacheMode.BYPASS,
        "word_count_threshold": 5,
        "wait_until": wait_until,
        "page_timeout": max(5_000, int(page_timeout_sec) * 1000),
        "delay_before_return_html": effective_delay,
        "remove_overlay_elements": True,
        "remove_consent_popups": True,
        "verbose": False,
        "simulate_user": simulate_user,
        "override_navigator": override_navigator,
        "max_retries": max(0, int(max_retries)),
        "locale": locale,
        "timezone_id": timezone_id,
    }
    if cloudflare_wait:
        kwargs["wait_for"] = CLOUDFLARE_WAIT_FOR
    if profile == "content":
        kwargs["excluded_tags"] = list(_CONTENT_EXCLUDED_TAGS)
        kwargs["excluded_selector"] = _CONTENT_EXCLUDED_SELECTOR
        kwargs["exclude_all_images"] = True
        kwargs["wait_for_images"] = False
    elif profile == "link_harvest":
        from keel_crawler.browser.harvest import NAV_EXPAND_JS_AFTER, NAV_EXPAND_JS_BEFORE

        # Keep expanded nav/mega-menu nodes visible for the DOM-harvest hooks.
        kwargs["remove_overlay_elements"] = False
        kwargs["remove_consent_popups"] = False
        kwargs["simulate_user"] = False
        kwargs["scan_full_page"] = True
        kwargs["scroll_delay"] = 0.22
        kwargs["max_scroll_steps"] = 32
        kwargs["js_code_before_wait"] = NAV_EXPAND_JS_BEFORE.strip()
        kwargs["js_code"] = NAV_EXPAND_JS_AFTER.strip()
    return CrawlerRunConfig(**kwargs)
