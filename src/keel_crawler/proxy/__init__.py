"""Layer 1 — proxy rotation, in two shapes for two situations.

**A pool of public addresses** (:mod:`keel_crawler.proxy.pool`) — for when the
answer to "we are blocked" is *use a different IP*, and there is no configured
egress to point at. ``ProxyStore`` harvests sixteen published lists across nine
publishers, verifies addresses **against the real target**, remembers what
worked, and ages out what did not, so the file never becomes a junk drawer.
``ProxyPool`` rotates over what survived, giving every address its own
per-second, per-minute and per-hour budget so a pool is not spent in one run.
Maintenance runs itself on every build; ``python -m keel_crawler.proxy`` exposes
it for inspection and pre-warming.

**A managed set of named outbounds** (:mod:`keel_crawler.proxy.mihomo` +
:mod:`keel_crawler.proxy.scores`) — for when there *is* a configured egress.
``MihomoClient`` drives a Clash.Meta control API to switch the active outbound
between crawl retries, and ``ProxyScoreStore`` persists per-outbound
success/failure scores and probe delays to choose the order. Both resolve their
config from ``KEEL_CRAWLER`` with environment-variable fallback.

Which to use: the pool when you need many disposable addresses and per-address
quality is negotiable; Mihomo when you have a few good ones and want the best of
them chosen. They share :mod:`keel_crawler.proxy.jsonstore` for on-disk state and
are otherwise independent.
"""
from keel_crawler.proxy.mihomo import MihomoClient, mihomo_from_config
from keel_crawler.proxy.pool import (Budget, Proxy, ProxyPool, ProxyStore,
                                     fetch_through, looks_usable)
from keel_crawler.proxy.scores import ProxyScoreStore, default_score_store
from keel_crawler.proxy.sources import SOURCES, Source

__all__ = [
    "Budget",
    "MihomoClient",
    "Proxy",
    "ProxyPool",
    "ProxyScoreStore",
    "ProxyStore",
    "SOURCES",
    "Source",
    "default_score_store",
    "fetch_through",
    "looks_usable",
    "mihomo_from_config",
]
