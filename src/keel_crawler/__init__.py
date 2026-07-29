"""Keel Crawler — reusable, business-blind web-crawling toolkit.

Layer 0 (fetch) and Layer 2 (Markdown cleaning) are available today; the browser
anti-bot engine, proxy rotation, generic crawl-job model, and RSS source layer land
in later versions. See ``CLAUDE.md`` for the layer map and extension seams.

``HttpFetcher`` and ``normalize_request_url`` are re-exported lazily (PEP 562) so a
bare ``import keel_crawler`` never pulls in the Django model before settings are
configured — the submodule loads only on first attribute access.
"""

__version__ = "0.1.0"

__all__ = ["HttpFetcher", "normalize_request_url"]


def __getattr__(name):
    if name == "HttpFetcher":
        from keel_crawler.fetch.client import HttpFetcher

        return HttpFetcher
    if name == "normalize_request_url":
        from keel_crawler.normalize import normalize_request_url

        return normalize_request_url
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
