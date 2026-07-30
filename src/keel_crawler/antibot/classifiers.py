"""Classify crawl error strings and challenge HTML.

Pure string heuristics extracted verbatim from Revenika's platform_crawler — no
Django, no network, no business logic. These drive the retry/egress ladder in
:mod:`keel_crawler.browser.engine`: an anti-bot block may clear on a different
egress, a proxy-connection error must switch to direct, a transient error is worth
another attempt, and a rate-limit backs off harder.
"""
from __future__ import annotations


def is_rate_limit_error(message: str) -> bool:
    """HTTP 429 / rate-limit style failures (incl. crawl4ai anti-bot wrapper text)."""
    m = (message or "").lower()
    if "too many requests" in m:
        return True
    if "rate limit" in m or "ratelimit" in m:
        return True
    if " 429" in m or m.startswith("429") or ": 429" in m or "http 429" in m:
        return True
    return False


def is_antibot_block_error(message: str) -> bool:
    """WAF / anti-bot block messages that may clear when using a different egress."""
    m = (message or "").lower()
    if is_rate_limit_error(message):
        return True
    if "blocked by anti-bot" in m:
        return True
    if "cloudflare" in m and ("challenge" in m or "js " in m or "blocked" in m):
        return True
    if "perimeterx" in m or "datadome" in m:
        return True
    if "akamai" in m and "challenge" in m:
        return True
    return False


def is_proxy_connection_error(message: str) -> bool:
    """Playwright / Chromium errors when the configured HTTP proxy is unreachable."""
    m = (message or "").lower()
    needles = (
        "err_proxy_connection_failed",
        "err_tunnel_connection_failed",
        "err_proxy_certificate_invalid",
        "proxy_connection_failed",
        "proxy connection",
        "failed to connect to the proxy",
        "net::err_proxy",
    )
    return any(n in m for n in needles)


def should_retry_error(message: str) -> bool:
    """True for transient failures worth another attempt (not proxy-unreachable)."""
    m = (message or "").lower()
    if is_proxy_connection_error(m):
        return False
    if is_rate_limit_error(m):
        return True
    needles = (
        "timeout",
        "timed out",
        "time out",
        "remote end closed",
        "connection aborted",
        "remotedisconnected",
        "econnreset",
        "connection reset",
        "network error",
        "navigating",
        "page.goto",
        "goto:",
        "target closed",
        "net::",
    )
    return any(n in m for n in needles)


def retry_sleep_seconds(error_message: str, attempt_index: int) -> float:
    """Backoff before the next attempt — exponential for rate limits, gentle otherwise."""
    if is_rate_limit_error(error_message):
        return min(90.0, 4.0 * (2**attempt_index))
    return min(30.0, 1.2 * (attempt_index + 1))


def looks_like_cloudflare_interstitial(html: str) -> bool:
    """Tiny challenge page with no real content — Playwright may still see it."""
    h = (html or "").lower()
    return ("just a moment" in h and "_cf_chl_opt" in h) or "cf-browser-verification" in h
