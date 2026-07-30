"""Automatic URL discovery — two complementary strategies.

* **Sitemap** (``discover_sitemap_urls``): read ``robots.txt`` for ``Sitemap:`` lines
  (falling back to ``/sitemap.xml``), follow sitemap-index files into their children,
  and collect every ``<loc>``. Handles gzipped sitemaps. Cheap and complete when a
  site publishes one.
* **Deep crawl** (``deep_crawl``): breadth-first link following from seed URLs, bounded
  by ``max_pages`` and ``max_depth``, same-domain by default. Uses a ``fetch_links``
  callable so the caller picks the transport — the light HTTP+regex adapter
  (``http_link_fetcher``) or the browser ``link_harvest`` adapter
  (``browser_link_fetcher``) for JS-rendered nav.

Both are business-blind: an optional ``include``/``match``/``deny`` predicate lets the
host scope discovery to the paths it cares about.
"""
from __future__ import annotations

import gzip
import logging
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

from keel_crawler.browser.harvest import dedupe_urls_preserve_order, extract_hrefs_regex
from keel_crawler.clean.snapshot import normalize_domain_from_url

logger = logging.getLogger(__name__)


def _user_agent() -> str:
    try:
        from keel_crawler.config import crawler_setting

        return crawler_setting("user_agent_text")
    except Exception:
        return "keel-crawler"


def _origin(url: str) -> str:
    p = urlparse((url or "").strip())
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _get_bytes(url: str, http_fetcher: Any, *, timeout: int = 20) -> Optional[bytes]:
    headers = {"User-Agent": _user_agent()}
    if http_fetcher is not None and hasattr(http_fetcher, "throttled_get"):
        resp = http_fetcher.throttled_get(url, timeout=timeout, headers=headers)
        if resp is None or not getattr(resp, "ok", False):
            return None
        return resp.content
    import requests

    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        return r.content if r.ok else None
    except Exception:
        return None


def _maybe_gunzip(url: str, content: bytes | None) -> bytes | None:
    if not content:
        return content
    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except Exception:
            return content
    return content


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def parse_sitemap(content: bytes) -> tuple[list[str], list[str]]:
    """Return ``(child_sitemaps, page_urls)`` from one sitemap document.

    A ``<sitemapindex>`` yields child sitemaps; a ``<urlset>`` (or anything else with
    ``<loc>`` entries) yields page URLs.
    """
    try:
        root = ET.fromstring(content)
    except Exception:
        return [], []
    locs = [
        (e.text or "").strip()
        for e in root.iter()
        if _local(e.tag) == "loc" and e.text and e.text.strip()
    ]
    if _local(root.tag) == "sitemapindex":
        return locs, []
    return [], locs


def _robots_sitemaps(origin: str, http_fetcher: Any) -> list[str]:
    content = _get_bytes(urljoin(origin + "/", "robots.txt"), http_fetcher)
    if not content:
        return []
    out: list[str] = []
    for line in content.decode("utf-8", "replace").splitlines():
        if line.lower().startswith("sitemap:"):
            val = line.split(":", 1)[1].strip()
            if val:
                out.append(val)
    return out


def discover_sitemap_urls(
    base_url: str,
    *,
    http_fetcher: Any = None,
    max_urls: int = 5000,
    max_sitemaps: int = 50,
    include: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Discover page URLs from a site's sitemap(s). Returns a deduped, capped list."""
    origin = _origin(base_url)
    if not origin:
        return []
    queue: deque[str] = deque(_robots_sitemaps(origin, http_fetcher) or [f"{origin}/sitemap.xml"])
    seen_sm: set[str] = set()
    page_urls: list[str] = []
    fetched = 0

    while queue and fetched < max_sitemaps and len(page_urls) < max_urls:
        sm = queue.popleft()
        if sm in seen_sm:
            continue
        seen_sm.add(sm)
        fetched += 1
        content = _maybe_gunzip(sm, _get_bytes(sm, http_fetcher))
        if not content:
            continue
        children, urls = parse_sitemap(content)
        for c in children:
            if c not in seen_sm:
                queue.append(c)
        for u in urls:
            if include is not None and not include(u):
                continue
            page_urls.append(u)

    if fetched >= max_sitemaps and queue:
        logger.info("discover_sitemap_urls: stopped at max_sitemaps=%d (more remained)", max_sitemaps)
    return dedupe_urls_preserve_order(page_urls)[:max_urls]


def http_link_fetcher(
    http_fetcher: Any,
    *,
    match: Optional[Callable[[str, str, str], bool]] = None,
    deny: Optional[Callable[[str], bool]] = None,
) -> Callable[[str], list[str]]:
    """A ``fetch_links`` for :func:`deep_crawl` using cheap HTTP + regex href extraction."""

    def _links(url: str) -> list[str]:
        html = http_fetcher.get_html_document(url)
        return extract_hrefs_regex(html or "", url, match=match, deny=deny)

    return _links


def browser_link_fetcher(browser_fetcher: Any) -> Callable[[str], list[str]]:
    """A ``fetch_links`` for :func:`deep_crawl` using the browser ``link_harvest`` profile."""

    def _links(url: str) -> list[str]:
        page = browser_fetcher.fetch_one(url)
        return list(page.discovery_hrefs or [])

    return _links


def deep_crawl(
    seeds: Iterable[str],
    *,
    fetch_links: Callable[[str], list[str]],
    max_pages: int = 200,
    max_depth: int = 2,
    same_domain: bool = True,
    match: Optional[Callable[[str], bool]] = None,
    deny: Optional[Callable[[str], bool]] = None,
    progress: Any = None,
) -> list[str]:
    """Breadth-first link discovery from ``seeds``. Returns visited URLs in order.

    ``fetch_links(url) -> list[str]`` supplies the links found on a page (use
    :func:`http_link_fetcher` or :func:`browser_link_fetcher`). ``max_pages`` caps how
    many pages are fetched; ``max_depth`` caps link-following depth (0 = seeds only).
    """
    seed_list = [s for s in seeds if s and s.strip()]
    seed_domains = {normalize_domain_from_url(s) for s in seed_list}
    seed_domains.discard(None)

    def key(u: str) -> str:
        return dedupe_urls_preserve_order([u])[0] if u else ""

    seen: set[str] = set()
    visited: list[str] = []
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seed_list)

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        k = key(url)
        if not k or k in seen:
            continue
        seen.add(k)
        visited.append(url)
        if progress is not None:
            progress.step(f"depth {depth}: {url[:80]}")
        if depth >= max_depth:
            continue
        try:
            links = fetch_links(url)
        except Exception:
            logger.debug("deep_crawl fetch_links failed for %s", url, exc_info=True)
            continue
        for link in links:
            if same_domain and normalize_domain_from_url(link) not in seed_domains:
                continue
            if deny is not None and deny(link):
                continue
            if match is not None and not match(link):
                continue
            lk = key(link)
            if lk and lk not in seen:
                queue.append((link, depth + 1))

    return visited
