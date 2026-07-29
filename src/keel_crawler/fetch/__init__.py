"""Layer 0 — fetch transport.

``HttpFetcher`` is the cheap-first backend: plain ``requests`` with a per-host
throttle and an optional DB-backed response cache. The browser/anti-bot backend
(``BrowserFetcher``) and the unified ``Fetcher`` protocol land in a later version.
"""
from keel_crawler.fetch.client import HttpFetcher

__all__ = ["HttpFetcher"]
