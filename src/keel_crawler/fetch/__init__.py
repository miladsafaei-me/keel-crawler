"""Layer 0 — fetch transport.

``HttpFetcher`` is the cheap-first backend: plain ``requests`` with a per-host
throttle and an optional DB-backed response cache. ``HybridFetcher`` composes it with
the browser/anti-bot backend (``BrowserFetcher``), trying HTTP first and escalating to
Chromium only when a page comes back empty, challenged, or too thin.
"""
from keel_crawler.fetch.client import HttpFetcher
from keel_crawler.fetch.hybrid import HybridFetcher

__all__ = ["HttpFetcher", "HybridFetcher"]
