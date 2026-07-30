"""Keel Crawler — reusable, business-blind web-crawling toolkit.

Layer 0 (fetch) and Layer 2 (Markdown cleaning) are available today; the browser
anti-bot engine, proxy rotation, generic crawl-job model, and RSS source layer land
in later versions. See ``CLAUDE.md`` for the layer map and extension seams.

``HttpFetcher`` and ``normalize_request_url`` are re-exported lazily (PEP 562) so a
bare ``import keel_crawler`` never pulls in the Django model before settings are
configured — the submodule loads only on first attribute access.
"""

__version__ = "0.7.0"

__all__ = [
    "HttpFetcher",
    "HybridFetcher",
    "normalize_request_url",
    "BrowserFetcher",
    "CrawledPage",
]

# Lazy so a bare import never pulls in Django models / crawl4ai before settings load.
_LAZY = {
    "HttpFetcher": ("keel_crawler.fetch.client", "HttpFetcher"),
    "HybridFetcher": ("keel_crawler.fetch.hybrid", "HybridFetcher"),
    "normalize_request_url": ("keel_crawler.normalize", "normalize_request_url"),
    "BrowserFetcher": ("keel_crawler.browser.engine", "BrowserFetcher"),
    "CrawledPage": ("keel_crawler.browser.extract", "CrawledPage"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])
