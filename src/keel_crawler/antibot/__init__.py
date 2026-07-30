"""Layer 1 — anti-bot signal classification (pure, no Django/network)."""
from keel_crawler.antibot.classifiers import (
    is_antibot_block_error,
    is_proxy_connection_error,
    is_rate_limit_error,
    looks_like_cloudflare_interstitial,
    retry_sleep_seconds,
    should_retry_error,
)

__all__ = [
    "is_antibot_block_error",
    "is_proxy_connection_error",
    "is_rate_limit_error",
    "looks_like_cloudflare_interstitial",
    "retry_sleep_seconds",
    "should_retry_error",
]
