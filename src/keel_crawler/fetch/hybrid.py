"""Cheap-first hybrid fetcher: try HTTP, escalate to the browser only when needed.

This is the "cheap-first behind one Fetcher" idea the package is built around, made
concrete. Most pages on most sites are served by a single cheap HTTP GET; only the
ones that come back empty, challenged, or suspiciously thin (JS-rendered shells,
anti-bot interstitials) are escalated to the heavyweight :class:`BrowserFetcher`
(crawl4ai + Chromium + the anti-bot ladder). The browser is built **lazily** — a batch
whose pages all succeed over HTTP never launches Chromium at all.

The escalation decision is a plain predicate ``needs_browser(html, final_url) -> bool``
with a conservative default (empty / Cloudflare interstitial / below a visible-text
floor). It's injectable so a host can tighten or loosen it without touching this code.

Output is uniform: every result is a :class:`CrawledPage`, whether it came from the
cheap path (:func:`keel_crawler.browser.extract.page_from_html`) or the browser.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from keel_crawler.antibot.classifiers import looks_like_cloudflare_interstitial

logger = logging.getLogger(__name__)

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def estimate_visible_text_len(html: str) -> int:
    """Rough count of visible characters in ``html`` (strip script/style + tags).

    Dependency-free and deliberately coarse — it only needs to separate a real
    article from an almost-empty JS shell, not to extract content.
    """
    if not html:
        return 0
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = _TAG_RE.sub(" ", stripped)
    return len(" ".join(stripped.split()))


class HybridFetcher:
    """Fetch HTML cheaply first, fall back to the browser engine on inadequate pages."""

    def __init__(
        self,
        *,
        http_fetcher: Any,
        browser_fetcher: Any = None,
        browser_factory: Optional[Callable[[], Any]] = None,
        needs_browser: Optional[Callable[[str, str], bool]] = None,
        min_text_chars: int = 500,
        http_timeout: int = 18,
        max_http_workers: int | None = None,
    ) -> None:
        self._http = http_fetcher
        self._browser = browser_fetcher
        self._browser_factory = browser_factory
        self._needs_browser = needs_browser or self._default_needs_browser
        self._min_text = max(0, int(min_text_chars))
        self._http_timeout = int(http_timeout)
        self._max_http_workers = max_http_workers
        # Observability: how the batch was served.
        self.http_served = 0
        self.browser_escalated = 0

    @classmethod
    def from_config(cls, *, http_fetcher: Any = None, **kwargs: Any) -> "HybridFetcher":
        """Convenience builder: a default ``HttpFetcher`` + a lazy ``BrowserFetcher``.

        Chromium is only constructed on the first escalation (the factory closes over
        ``BrowserFetcher.from_config``), so importing/using this without the
        ``[browser]`` extra is fine as long as nothing actually escalates.
        """
        from keel_crawler.fetch.client import HttpFetcher

        http = http_fetcher or HttpFetcher()

        def _factory() -> Any:
            from keel_crawler.browser.engine import BrowserFetcher

            return BrowserFetcher.from_config()

        return cls(http_fetcher=http, browser_factory=_factory, **kwargs)

    def _default_needs_browser(self, html: str, final_url: str) -> bool:
        if not html or not html.strip():
            return True
        if looks_like_cloudflare_interstitial(html):
            return True
        if estimate_visible_text_len(html) < self._min_text:
            return True
        return False

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        if self._browser_factory is not None:
            self._browser = self._browser_factory()
            return self._browser
        return None

    def _page_from_html(self, url: str, html: str, final_url: str | None) -> Any:
        from keel_crawler.browser.extract import page_from_html

        return page_from_html(url, html or "", final_url or "")

    def fetch_one(self, url: str) -> Any:
        """Fetch one URL cheap-first; escalate to the browser only if inadequate."""
        html, final_url = self._http.get_html_document_with_final_url(
            url, timeout=self._http_timeout
        )
        if html and not self._needs_browser(html, final_url or url):
            self.http_served += 1
            return self._page_from_html(url, html, final_url)
        browser = self._ensure_browser()
        if browser is not None:
            self.browser_escalated += 1
            return browser.fetch_one(url)
        # No browser available: return the best-effort cheap page (may be thin/empty).
        self.http_served += 1
        return self._page_from_html(url, html or "", final_url)

    def fetch_many(self, urls: list[str]) -> list[Any]:
        """Fetch many URLs: cheap HTTP concurrently, then one browser batch for the
        pages that need it. Results preserve input order.
        """
        urls = list(urls)
        if not urls:
            return []

        pairs = self._http.fetch_many(
            urls, mode="html", timeout=self._http_timeout, max_workers=self._max_http_workers
        )
        results: list[Any] = [None] * len(urls)
        escalate_idx: list[int] = []
        for i, (u, (html, final_url)) in enumerate(zip(urls, pairs)):
            if html and not self._needs_browser(html, final_url or u):
                results[i] = self._page_from_html(u, html, final_url)
            else:
                escalate_idx.append(i)

        self.http_served += len(urls) - len(escalate_idx)

        if escalate_idx:
            browser = self._ensure_browser()
            if browser is not None:
                self.browser_escalated += len(escalate_idx)
                pages = browser.fetch_many([urls[i] for i in escalate_idx])
                for i, page in zip(escalate_idx, pages):
                    results[i] = page
            else:
                logger.info(
                    "HybridFetcher: %d page(s) need the browser but none is configured; "
                    "returning best-effort HTTP output",
                    len(escalate_idx),
                )
                self.http_served += len(escalate_idx)
                for i in escalate_idx:
                    html, final_url = pairs[i]
                    results[i] = self._page_from_html(urls[i], html or "", final_url)

        return results

    def metrics(self) -> dict[str, int]:
        return {"http_served": self.http_served, "browser_escalated": self.browser_escalated}
