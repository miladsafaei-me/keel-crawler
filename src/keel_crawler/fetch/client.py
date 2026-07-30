"""Cheap-first HTTP fetch backend: per-host throttle, optional DB response cache,
shared Session, dual-UA HTML fetching.

Extracted from Revenika's ``core.crawl.client.CrawlHttpClient`` and made
business-blind: User-Agent strings and the default cache TTL come from the
``KEEL_CRAWLER`` settings dict (see :mod:`keel_crawler.config`).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from django.utils import timezone

from keel_crawler.config import crawler_setting
from keel_crawler.models import CrawlHttpCache
from keel_crawler.normalize import normalize_request_url
from keel_crawler.throttle import HostThrottle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _L1Entry:
    """In-process HTTP cache row (avoids repeated DB round-trips per URL in one run)."""

    expires_at: datetime
    body_text: str
    headers_json: dict[str, str]
    status_code: int
    final_url: str


@dataclass(frozen=True)
class _Fetched:
    text: str
    final_url: str
    status_code: int
    headers: dict[str, str]


class HttpFetcher:
    """
    Fetch layer: per-host throttle, optional DB-backed HTTP cache, shared Session.
    Used directly for light crawls and (via ``http_client=``) by discovery services.
    """

    def __init__(
        self,
        *,
        cache_ttl_seconds: int | None = None,
        min_interval_per_host: float = 0.0,
        max_body_chars: int = 1_500_000,
        enable_cache: bool = True,
        l1_max_entries: int = 8192,
        l1_max_bytes: int = 256 * 1024 * 1024,
        pool_maxsize: int = 16,
        max_download_bytes: int = 16 * 1024 * 1024,
        transient_retries: int = 1,
    ) -> None:
        if cache_ttl_seconds is None:
            cache_ttl_seconds = int(crawler_setting("cache_ttl_seconds"))
        self._ttl = max(60, int(cache_ttl_seconds))
        self._enable = enable_cache
        self._max_body = max(1_000, int(max_body_chars))
        # Hard ceiling on how many bytes a single response body is read into memory,
        # BEFORE the char-based ``max_body`` truncation. Without it a pathological or
        # hostile URL (a 500 MB "page") would be fully downloaded and decoded.
        self._max_download = max(64 * 1024, int(max_download_bytes))
        # One cheap retry for transient network failures on the HTTP path keeps a
        # momentary reset/timeout from needlessly escalating a page to the browser.
        self._transient_retries = max(0, int(transient_retries))
        self._session = requests.Session()
        # Size the urllib3 connection pool for concurrent fetch_many: the default of
        # 10 would otherwise serialize connections once more than 10 threads run.
        self._pool_maxsize = max(1, int(pool_maxsize))
        adapter = HTTPAdapter(pool_connections=self._pool_maxsize, pool_maxsize=self._pool_maxsize)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._throttle = HostThrottle(min_interval_per_host)
        self.requests_made = 0
        self.cache_hits = 0
        # Guards the L1 OrderedDict, its byte tally, and the two counters so
        # ``fetch_many`` can drive the shared fetcher from several threads without
        # corrupting the cache.
        self._lock = threading.Lock()
        self._l1_max = max(256, int(l1_max_entries))
        # Bound the L1 cache by total body bytes as well as entry count: 8192 entries
        # at ~1.5 MB each could otherwise pin gigabytes for one long crawl.
        self._l1_max_bytes = max(16 * 1024 * 1024, int(l1_max_bytes))
        self._l1: OrderedDict[str, _L1Entry] = OrderedDict()
        self._l1_bytes = 0
        # Negative cache TTL (permanent 404/410 only): capped so a page that later
        # comes back is re-checked within the hour rather than the full body TTL.
        self._neg_ttl = min(self._ttl, 3600)

    def metrics(self) -> dict[str, int]:
        return {"http_requests": self.requests_made, "http_cache_hits": self.cache_hits}

    def _bump_requests(self) -> None:
        with self._lock:
            self.requests_made = self.requests_made + 1

    def _bump_hits(self) -> None:
        with self._lock:
            self.cache_hits = self.cache_hits + 1

    def _ua_text(self) -> str:
        return crawler_setting("user_agent_text")

    def _ua_html(self) -> str:
        return crawler_setting("user_agent_html")

    def _ua_browser(self) -> str:
        return crawler_setting("browser_user_agent")

    def _l1_pop_stale(self, key: str) -> _L1Entry | None:
        now = timezone.now()
        with self._lock:
            entry = self._l1.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del self._l1[key]
                self._l1_bytes -= len(entry.body_text)
                return None
            self._l1.move_to_end(key)
            return entry

    def _l1_put(self, key: str, entry: _L1Entry) -> None:
        with self._lock:
            old = self._l1.get(key)
            if old is not None:
                self._l1_bytes -= len(old.body_text)
            self._l1[key] = entry
            self._l1.move_to_end(key)
            self._l1_bytes += len(entry.body_text)
            # Evict least-recently-used entries until both bounds hold. Keep at least
            # the just-inserted entry so a single oversized body never empties the cache.
            while len(self._l1) > 1 and (
                len(self._l1) > self._l1_max or self._l1_bytes > self._l1_max_bytes
            ):
                _k, evicted = self._l1.popitem(last=False)
                self._l1_bytes -= len(evicted.body_text)

    def _l1_negative_hit(self, l1: _L1Entry | None) -> bool:
        """A fresh, non-200 L1 entry marks a URL as known-dead — skip the refetch."""
        return l1 is not None and l1.status_code != 200

    def _remember_dead(self, key: str, code: int) -> None:
        """In-process negative cache for permanent HTTP failures (404/410 only).

        Transient errors (timeouts, 5xx, connection resets) are never cached — they
        must be retried. Only definitively-gone resources are remembered, L1-only, so
        a batch that references the same dead URL many times fetches it once.
        """
        if code not in (404, 410) or not key:
            return
        self._l1_put(
            key,
            _L1Entry(
                expires_at=timezone.now() + timedelta(seconds=self._neg_ttl),
                body_text="",
                headers_json={},
                status_code=code,
                final_url="",
            ),
        )

    def _load_row(self, key: str) -> CrawlHttpCache | None:
        """Load the single cache row for ``key`` (unique), fresh **or** stale.

        A stale row is still useful: its ``ETag``/``Last-Modified`` let the next fetch
        be a conditional GET (a 304 avoids re-downloading the body).
        """
        if not self._enable:
            return None
        return (
            CrawlHttpCache.objects.filter(normalized_url=key)
            .only(
                "body_text",
                "body_truncated",
                "status_code",
                "final_url",
                "headers_json",
                "expires_at",
            )
            .first()
        )

    @staticmethod
    def _row_fresh(row: CrawlHttpCache) -> bool:
        return row.expires_at is not None and row.expires_at > timezone.now()

    @staticmethod
    def _cond_headers(row: CrawlHttpCache | None) -> dict[str, str]:
        """Conditional-GET validators from a (possibly stale) row's stored headers."""
        if row is None:
            return {}
        hdrs = row.headers_json or {}
        cond: dict[str, str] = {}
        etag = hdrs.get("ETag") or hdrs.get("Etag") or hdrs.get("etag")
        last_mod = hdrs.get("Last-Modified") or hdrs.get("last-modified")
        if etag:
            cond["If-None-Match"] = str(etag)
        if last_mod:
            cond["If-Modified-Since"] = str(last_mod)
        return cond

    @staticmethod
    def _is_html(headers: dict[str, str]) -> bool:
        return "html" in str(headers.get("Content-Type", "")).lower()

    def _write_cache(
        self,
        *,
        key: str,
        hostname: str,
        status_code: int,
        final_url: str,
        headers: dict[str, str],
        text: str,
    ) -> None:
        truncated = len(text) > self._max_body
        body = text[: self._max_body] if truncated else text
        exp = timezone.now() + timedelta(seconds=self._ttl)
        hdrs = {str(k): str(v) for k, v in headers.items()}
        self._l1_put(
            key,
            _L1Entry(
                expires_at=exp,
                body_text=body,
                headers_json=hdrs,
                status_code=status_code,
                final_url=(final_url or "")[:2048],
            ),
        )
        if not self._enable:
            return
        sha = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
        row = CrawlHttpCache(
            normalized_url=key,
            hostname=hostname[:253],
            status_code=status_code,
            final_url=(final_url or "")[:2048],
            headers_json=hdrs,
            body_text=body,
            body_truncated=truncated,
            sha256_hex=sha,
            expires_at=exp,
        )
        # One statement (INSERT ... ON CONFLICT DO UPDATE) instead of update_or_create's
        # SELECT + INSERT/UPDATE. Falls back for any backend without upsert support.
        try:
            CrawlHttpCache.objects.bulk_create(
                [row],
                update_conflicts=True,
                unique_fields=["normalized_url"],
                update_fields=[
                    "hostname",
                    "status_code",
                    "final_url",
                    "headers_json",
                    "body_text",
                    "body_truncated",
                    "sha256_hex",
                    "expires_at",
                ],
            )
        except Exception:
            CrawlHttpCache.objects.update_or_create(
                normalized_url=key,
                defaults={
                    "hostname": hostname[:253],
                    "status_code": status_code,
                    "final_url": (final_url or "")[:2048],
                    "headers_json": hdrs,
                    "body_text": body,
                    "body_truncated": truncated,
                    "sha256_hex": sha,
                    "expires_at": exp,
                },
            )

    def _consume_capped(self, r: requests.Response) -> bool:
        """Read at most ``max_download_bytes`` of a *streamed* response into ``r``.

        Streaming + a byte ceiling means a huge/hostile body can't exhaust memory: we
        stop reading past the cap and let the normal ``.text``/``.content`` decoding run
        over what we kept. Returns True if the body was truncated at the cap.
        """
        total = 0
        chunks: list[bytes] = []
        truncated = False
        try:
            for chunk in r.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= self._max_download:
                    truncated = True
                    break
        except requests.RequestException:
            # A mid-stream read error yields what we have so far (possibly empty).
            pass
        r._content = b"".join(chunks)
        r._content_consumed = True
        return truncated

    # Retriable HTTP status codes on the cheap path (transient server/limit states).
    _RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def _request_once(
        self,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str],
    ) -> _Fetched | None:
        key, host = normalize_request_url(url)
        if not host:
            return None
        for attempt in range(self._transient_retries + 1):
            last = attempt >= self._transient_retries
            self._throttle.wait(host)
            try:
                r = self._session.get(
                    url, timeout=timeout, headers=headers, allow_redirects=True, stream=True
                )
            except requests.RequestException as exc:
                logger.debug("crawl get %s: %s", url, exc)
                if last:
                    return None
                time.sleep(min(1.5 * (attempt + 1), 3.0))
                continue
            self._bump_requests()
            try:
                r.raise_for_status()
            except requests.HTTPError as exc:
                resp = exc.response
                code = resp.status_code if resp is not None else 0
                r.close()
                logger.debug("crawl get %s HTTP %s", url, code)
                if code in (404, 410):
                    self._remember_dead(key, code)
                    return None
                if code in self._RETRIABLE_STATUS and not last:
                    time.sleep(min(1.5 * (attempt + 1), 3.0))
                    continue
                return None
            self._consume_capped(r)
            text = r.text or ""
            final = str(r.url)
            hdrs = {str(k): str(v) for k, v in r.headers.items()}
            r.close()
            return _Fetched(
                text=text, final_url=final, status_code=r.status_code, headers=hdrs
            )
        return None

    def throttled_get(
        self,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str],
        max_bytes: int | None = None,
    ) -> requests.Response | None:
        """
        Single GET with session + per-host throttle only (no L1/DB cache).
        For binary responses, logos, or HTML that should not share the text cache.

        The body is streamed and capped (``max_bytes``, default 64 MiB — generous
        enough for a large uncompressed sitemap) so a runaway response can't exhaust
        memory. Callers read ``.content``/``.text`` as usual.
        """
        _, host = normalize_request_url(url)
        if not host:
            return None
        cap = max(64 * 1024, int(max_bytes)) if max_bytes is not None else 64 * 1024 * 1024
        self._throttle.wait(host)
        try:
            r = self._session.get(
                url, timeout=timeout, headers=headers, allow_redirects=True, stream=True
            )
        except requests.RequestException as exc:
            logger.debug("crawl throttled_get %s: %s", url, exc)
            return None
        self._bump_requests()
        total = 0
        chunks: list[bytes] = []
        try:
            for chunk in r.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= cap:
                    break
        except requests.RequestException as exc:
            logger.debug("crawl throttled_get %s read: %s", url, exc)
        r._content = b"".join(chunks)
        r._content_consumed = True
        return r

    def _serve_from_row(self, key: str, row: CrawlHttpCache) -> _Fetched:
        """Populate L1 from a fresh DB row and return it as a ``_Fetched``."""
        self._l1_put(
            key,
            _L1Entry(
                expires_at=row.expires_at,
                body_text=row.body_text or "",
                headers_json=dict(row.headers_json or {}),
                status_code=row.status_code,
                final_url=row.final_url or key,
            ),
        )
        return _Fetched(
            text=row.body_text or "",
            final_url=row.final_url or key,
            status_code=200,
            headers=dict(row.headers_json or {}),
        )

    def _touch_cache_ttl(self, key: str, row: CrawlHttpCache) -> _Fetched:
        """A 304 means the stored body is still current — refresh only its TTL.

        Re-running the full ``_write_cache`` here would re-ship the (possibly
        multi-hundred-KB) body to the DB and recompute its SHA on every revalidation,
        which defeats the point of the conditional GET. Instead we bump ``expires_at``
        with a single narrow UPDATE and mutate the in-memory row so the L1 re-seed
        (via :meth:`_serve_from_row`) carries the *new* expiry, not the stale one.
        """
        exp = timezone.now() + timedelta(seconds=self._ttl)
        row.expires_at = exp
        if self._enable:
            CrawlHttpCache.objects.filter(normalized_url=key).update(expires_at=exp)
        return self._serve_from_row(key, row)

    def _get_cached(
        self,
        url: str,
        *,
        ua_list: tuple[str, ...],
        accept: str,
        require_html: bool,
        timeout: int,
    ) -> _Fetched | None:
        """Shared fetch core: L1 → fresh DB serve → conditional/network GET → cache.

        Returns the current document as a ``_Fetched`` (from cache or network), or
        ``None`` when there is nothing usable. When ``require_html`` is set, a known
        non-HTML 200 yields ``None`` (no wasted refetch/return), matching the old
        per-method behaviour; a fresh cached page is served without a network hit, and a
        stale one is revalidated with ``If-None-Match``/``If-Modified-Since`` so an
        unchanged page comes back as a cheap 304 instead of a full re-download.
        """
        key, host = normalize_request_url(url)
        if not key or not host:
            return None

        l1 = self._l1_pop_stale(key)
        if self._l1_negative_hit(l1):
            return None
        if l1 is not None and l1.status_code == 200:
            self._bump_hits()
            if require_html and not self._is_html(l1.headers_json or {}):
                return None
            return _Fetched(
                text=l1.body_text or "",
                final_url=l1.final_url or key,
                status_code=200,
                headers=dict(l1.headers_json or {}),
            )

        row = self._load_row(key)
        if row is not None and row.status_code == 200 and self._row_fresh(row):
            self._bump_hits()
            served = self._serve_from_row(key, row)  # populate L1 even if non-HTML
            if require_html and not self._is_html(served.headers):
                return None
            return served

        cond = self._cond_headers(row)
        base = {"Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
        for ua in ua_list:
            fetched = self._request_once(
                url, timeout=timeout, headers={**base, "User-Agent": ua, **cond}
            )
            if fetched is None:
                continue
            if fetched.status_code == 304 and row is not None:
                # Unchanged since the cached copy — refresh only the TTL and serve the
                # stored body; the conditional GET already avoided the re-download, and
                # this avoids re-writing the body back to the DB too.
                served = self._touch_cache_ttl(key, row)
                if require_html and not self._is_html(row.headers_json or {}):
                    return None
                return served
            if fetched.status_code == 200:
                # Cache every 200 (even non-HTML) so a later fetch short-circuits.
                self._write_cache(
                    key=key,
                    hostname=host,
                    status_code=fetched.status_code,
                    final_url=fetched.final_url,
                    headers=fetched.headers,
                    text=fetched.text,
                )
                if require_html and not self._is_html(fetched.headers):
                    logger.debug("crawl html %s non-html Content-Type", url)
                    continue  # try the next UA in case it serves HTML (anti-cloaking)
                return fetched
            return fetched
        return None

    def get_text(self, url: str, *, timeout: int = 18) -> str | None:
        fetched = self._get_cached(
            url,
            ua_list=(self._ua_text(),),
            accept="*/*, application/xml;q=0.9",
            require_html=False,
            timeout=timeout,
        )
        return (fetched.text or None) if fetched is not None else None

    def _get_html_dual_ua(self, url: str, *, timeout: int) -> tuple[str, str] | None:
        fetched = self._get_cached(
            url,
            ua_list=(self._ua_html(), self._ua_browser()),
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            require_html=True,
            timeout=timeout,
        )
        return (fetched.text, fetched.final_url) if fetched is not None else None

    def get_html_document_browser_single(
        self, url: str, *, timeout: int = 8
    ) -> tuple[str | None, str | None]:
        """
        One GET with browser UA only (no bot attempt, no second UA retry).
        Uses the same HTML cache keys as dual-UA fetches. For --fast discovery only.
        """
        fetched = self._get_cached(
            url,
            ua_list=(self._ua_browser(),),
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            require_html=True,
            timeout=timeout,
        )
        if fetched is None:
            return None, None
        return (fetched.text, fetched.final_url)

    def get_html_document(self, url: str, *, timeout: int = 18) -> str | None:
        """Dual-UA HTML fetch; returns the document text (or ``None``)."""
        pair = self._get_html_dual_ua(url, timeout=timeout)
        return pair[0] if pair else None

    def get_html_document_with_final_url(
        self, url: str, *, timeout: int = 20
    ) -> tuple[str | None, str | None]:
        """Dual-UA HTML fetch; returns ``(text, final_url)`` after redirects."""
        pair = self._get_html_dual_ua(url, timeout=timeout)
        if not pair:
            return None, None
        return pair

    def fetch_many(
        self,
        urls: list[str],
        *,
        mode: str = "html",
        timeout: int = 18,
        max_workers: int | None = None,
    ) -> list[tuple[str | None, str | None]]:
        """Fetch many URLs concurrently over the **one shared** session/cache/throttle.

        Returns one ``(text_or_None, final_url_or_None)`` per input URL, in input
        order. ``mode`` = ``"html"`` (dual-UA HTML, final_url after redirects) or
        ``"text"`` (raw text; final_url is always ``None``).

        ``requests`` blocks per thread, so a thread pool gives real parallelism across
        hosts while the per-host throttle keeps each individual site polite and the L1
        cache is shared (one fetch of a repeated URL serves the whole batch). Bounded by
        ``max_workers`` (default: the connection-pool size).
        """
        if not urls:
            return []
        workers = int(max_workers if max_workers is not None else self._pool_maxsize)
        workers = max(1, min(workers, len(urls)))

        def _one(u: str) -> tuple[str | None, str | None]:
            if mode == "text":
                return self.get_text(u, timeout=timeout), None
            return self.get_html_document_with_final_url(u, timeout=timeout)

        if workers == 1:
            return [_one(u) for u in urls]

        def _worker(u: str) -> tuple[str | None, str | None]:
            # Each pool thread gets its own thread-local Django DB connection; close it
            # at the end of the task so the pool doesn't leak connections.
            from django.db import connection

            try:
                return _one(u)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_worker, urls))
