"""Layer 1 — browser engine (crawl4ai + Playwright) behind the ``[browser]`` extra.

Nothing here imports crawl4ai/lxml at module load; the heavy deps load lazily inside
the functions that need them, so a host that only uses Layer 0 never pays for them.
"""
from keel_crawler.browser.extract import CrawledPage

__all__ = ["CrawledPage"]
