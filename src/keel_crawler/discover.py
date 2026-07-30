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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin

from keel_crawler.browser.harvest import canonical_url_key, extract_hrefs_regex
from keel_crawler.clean.snapshot import normalize_domain_from_url
from keel_crawler.normalize import origin_of

logger = logging.getLogger(__name__)


def _user_agent() -> str:
    try:
        from keel_crawler.config import crawler_setting

        return crawler_setting("user_agent_text")
    except Exception:
        return "keel-crawler"


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


def _fetch_sitemaps(batch: list[str], http_fetcher: Any, *, workers: int) -> list[bytes | None]:
    """Fetch (and gunzip) a group of sitemaps, concurrently when there is more than one.

    ``throttled_get`` shares the fetcher's thread-safe session + per-host throttle, so
    a same-level group of sitemaps can be pulled in parallel instead of one at a time.
    """
    if len(batch) == 1:
        return [_maybe_gunzip(batch[0], _get_bytes(batch[0], http_fetcher))]
    with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as ex:
        raw = list(ex.map(lambda s: _get_bytes(s, http_fetcher), batch))
    return [_maybe_gunzip(sm, content) for sm, content in zip(batch, raw)]


def discover_sitemap_urls(
    base_url: str,
    *,
    http_fetcher: Any = None,
    max_urls: int = 5000,
    max_sitemaps: int = 50,
    sitemap_workers: int = 8,
    include: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Discover page URLs from a site's sitemap(s). Returns a deduped, capped list.

    Sitemaps at the same level are fetched concurrently (up to ``sitemap_workers`` at a
    time); a big site with dozens of sitemap files no longer pays one serial round-trip
    per file. The ``max_sitemaps`` and ``max_urls`` caps still bound total work.
    """
    origin = origin_of(base_url)
    if not origin:
        return []
    queue: deque[str] = deque(_robots_sitemaps(origin, http_fetcher) or [f"{origin}/sitemap.xml"])
    workers = max(1, int(sitemap_workers))
    seen_sm: set[str] = set()
    page_urls: list[str] = []
    seen_pages: set[str] = set()  # dedup as we go so the max_urls cap counts UNIQUE urls
    fetched = 0

    while queue and fetched < max_sitemaps and len(page_urls) < max_urls:
        # Pull a group of not-yet-seen sitemaps from the front of the queue (bounded by
        # the worker count and the remaining max_sitemaps budget) and fetch them at once.
        allowed = max_sitemaps - fetched
        batch: list[str] = []
        while queue and len(batch) < workers and len(batch) < allowed:
            sm = queue.popleft()
            if sm in seen_sm:
                continue
            seen_sm.add(sm)
            batch.append(sm)
        if not batch:
            break
        fetched += len(batch)

        for content in _fetch_sitemaps(batch, http_fetcher, workers=workers):
            if not content:
                continue
            children, urls = parse_sitemap(content)
            for c in children:
                if c not in seen_sm:
                    queue.append(c)
            for u in urls:
                if include is not None and not include(u):
                    continue
                k = canonical_url_key(u)
                if not k or k in seen_pages:
                    continue
                seen_pages.add(k)
                page_urls.append(u)
                if len(page_urls) >= max_urls:
                    break
            if len(page_urls) >= max_urls:
                break

    if fetched >= max_sitemaps and queue:
        logger.info("discover_sitemap_urls: stopped at max_sitemaps=%d (more remained)", max_sitemaps)
    return page_urls[:max_urls]


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


def http_links_many_fetcher(
    http_fetcher: Any,
    *,
    match: Optional[Callable[[str, str, str], bool]] = None,
    deny: Optional[Callable[[str], bool]] = None,
) -> Callable[[list[str]], list[list[str]]]:
    """A batch ``fetch_links_many`` for :func:`deep_crawl` over cheap concurrent HTTP.

    The whole frontier level is fetched in one ``HttpFetcher.fetch_many`` call (shared
    session/cache/throttle, independent hosts in parallel), then hrefs are extracted
    per page — so BFS follows a level at a time instead of a page at a time.
    """

    def _links_many(urls: list[str]) -> list[list[str]]:
        pairs = http_fetcher.fetch_many(list(urls), mode="html")
        return [
            extract_hrefs_regex(html or "", url, match=match, deny=deny)
            for url, (html, _final) in zip(urls, pairs)
        ]

    return _links_many


def browser_links_many_fetcher(browser_fetcher: Any) -> Callable[[list[str]], list[list[str]]]:
    """A batch ``fetch_links_many`` for :func:`deep_crawl` using the browser
    ``link_harvest`` profile — the level is crawled through ``BrowserFetcher.fetch_many``
    (bounded concurrency + pacing + per-host politeness), reusing one browser pool.
    """

    def _links_many(urls: list[str]) -> list[list[str]]:
        pages = browser_fetcher.fetch_many(list(urls))
        return [list(getattr(p, "discovery_hrefs", None) or []) for p in pages]

    return _links_many


def deep_crawl(
    seeds: Iterable[str],
    *,
    fetch_links: Callable[[str], list[str]] | None = None,
    fetch_links_many: Callable[[list[str]], list[list[str]]] | None = None,
    max_pages: int = 200,
    max_depth: int = 2,
    same_domain: bool = True,
    match: Optional[Callable[[str], bool]] = None,
    deny: Optional[Callable[[str], bool]] = None,
    progress: Any = None,
) -> list[str]:
    """Breadth-first link discovery from ``seeds``. Returns visited URLs in order.

    Pass exactly one link source:

    * ``fetch_links(url) -> list[str]`` — sequential: one page fetched per step (use
      :func:`http_link_fetcher` / :func:`browser_link_fetcher`).
    * ``fetch_links_many(urls) -> list[list[str]]`` — parallel: the whole frontier level
      is fetched in one call, so the underlying concurrent/paced ``fetch_many`` engine
      actually runs (use :func:`http_links_many_fetcher` / :func:`browser_links_many_fetcher`).

    ``max_pages`` caps how many pages are fetched; ``max_depth`` caps link-following
    depth (0 = seeds only). Visited order is stable BFS regardless of the source.
    """
    if (fetch_links is None) == (fetch_links_many is None):
        raise ValueError("deep_crawl requires exactly one of fetch_links or fetch_links_many")

    seed_list = [s for s in seeds if s and s.strip()]
    seed_domains = {normalize_domain_from_url(s) for s in seed_list}
    seed_domains.discard(None)

    # Mark a URL seen when it is ENQUEUED, not when it is popped: a popular link (e.g.
    # the homepage) appears on nearly every page, so pop-time marking would let it
    # queue once per referring page and bloat the queue. Seeds are pre-marked too.
    seen: set[str] = set()
    visited: list[str] = []
    # The frontier is processed one whole depth-level at a time so a batch link source
    # can fetch the level concurrently. Within a level, BFS input order is preserved.
    frontier: list[tuple[str, int]] = []
    for s in seed_list:
        sk = canonical_url_key(s)
        if sk and sk not in seen:
            seen.add(sk)
            frontier.append((s, 0))

    def _expand(url: str, depth: int, links: list[str]) -> None:
        for link in links:
            if same_domain and normalize_domain_from_url(link) not in seed_domains:
                continue
            if deny is not None and deny(link):
                continue
            if match is not None and not match(link):
                continue
            lk = canonical_url_key(link)
            if lk and lk not in seen:
                seen.add(lk)
                next_frontier.append((link, depth + 1))

    while frontier and len(visited) < max_pages:
        # Cap this level to the remaining page budget (keeps visited <= max_pages and
        # mirrors the old one-at-a-time stop point).
        level = frontier[: max_pages - len(visited)]
        next_frontier: list[tuple[str, int]] = []
        for url, depth in level:
            visited.append(url)
            if progress is not None:
                progress.step(f"depth {depth}: {url[:80]}")

        expandable = [(u, d) for (u, d) in level if d < max_depth]
        if expandable:
            exp_urls = [u for u, _ in expandable]
            if fetch_links_many is not None:
                try:
                    results = fetch_links_many(exp_urls)
                except Exception:
                    logger.debug("deep_crawl fetch_links_many failed", exc_info=True)
                    results = [[] for _ in exp_urls]
                if len(results) != len(exp_urls):  # defensive: a misbehaving adapter
                    results = (list(results) + [[]] * len(exp_urls))[: len(exp_urls)]
            else:
                results = []
                for u in exp_urls:
                    try:
                        results.append(fetch_links(u))
                    except Exception:
                        logger.debug("deep_crawl fetch_links failed for %s", u, exc_info=True)
                        results.append([])
            for (url, depth), links in zip(expandable, results):
                _expand(url, depth, links or [])

        frontier = next_frontier

    return visited
