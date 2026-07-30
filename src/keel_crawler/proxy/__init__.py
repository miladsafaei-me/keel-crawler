"""Layer 1 — proxy scoring + Mihomo rotation.

``ProxyScoreStore`` persists per-outbound success/failure scores (and last probe
delays) to a lock-guarded JSON file, and supports **disabling** scoring and
**resetting** scores. ``MihomoClient`` drives a Clash.Meta control API to rotate
the active outbound between crawl retries. Both resolve their config from
``KEEL_CRAWLER`` with environment-variable fallback for deployment secrets.
"""
from keel_crawler.proxy.mihomo import MihomoClient, mihomo_from_config
from keel_crawler.proxy.scores import ProxyScoreStore, default_score_store

__all__ = [
    "ProxyScoreStore",
    "default_score_store",
    "MihomoClient",
    "mihomo_from_config",
]
