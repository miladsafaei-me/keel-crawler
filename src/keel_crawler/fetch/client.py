"""Cheap-first HTTP fetch backend: per-host throttle, optional DB response cache,
shared Session, dual-UA HTML fetching.

Extracted from Revenika's ``core.crawl.client.CrawlHttpClient`` and made
business-blind: User-Agent strings and the default cache TTL come from the
``KEEL_CRAWLER`` settings dict (see :mod:`keel_crawler.config`).
"""
from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
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
    ) -> None:
        if cache_ttl_seconds is None:
            cache_ttl_seconds = int(crawler_setting("cache_ttl_seconds"))
        self._ttl = max(60, int(cache_ttl_seconds))
        self._enable = enable_cache
        self._max_body = max(1_000, int(max_body_chars))
        self._session = requests.Session()
        self._throttle = HostThrottle(min_interval_per_host)
        self.requests_made = 0
        self.cache_hits = 0
        self._l1_max = max(256, int(l1_max_entries))
        self._l1: OrderedDict[str, _L1Entry] = OrderedDict()
        # Negative cache TTL (permanent 404/410 only): capped so a page that later
        # comes back is re-checked within the hour rather than the full body TTL.
        self._neg_ttl = min(self._ttl, 3600)

    def metrics(self) -> dict[str, int]:
        return {"http_requests": self.requests_made, "http_cache_hits": self.cache_hits}

    def _ua_text(self) -> str:
        return crawler_setting("user_agent_text")

    def _ua_html(self) -> str:
        return crawler_setting("user_agent_html")

    def _ua_browser(self) -> str:
        return crawler_setting("browser_user_agent")

    def _l1_pop_stale(self, key: str) -> _L1Entry | None:
        entry = self._l1.get(key)
        if entry is None:
            return None
        if entry.expires_at <= timezone.now():
            del self._l1[key]
            return None
        self._l1.move_to_end(key)
        return entry

    def _l1_put(self, key: str, entry: _L1Entry) -> None:
        self._l1[key] = entry
        self._l1.move_to_end(key)
        while len(self._l1) > self._l1_max:
            self._l1.popitem(last=False)

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

    def _load_cache_row(self, key: str) -> CrawlHttpCache | None:
        if not self._enable:
            return None
        now = timezone.now()
        return (
            CrawlHttpCache.objects.filter(normalized_url=key, expires_at__gt=now)
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
        self._throttle.wait(host)
        try:
            r = self._session.get(url, timeout=timeout, headers=headers, allow_redirects=True)
        except requests.RequestException as exc:
            logger.debug("crawl get %s: %s", url, exc)
            return None
        self.requests_made += 1
        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            code = resp.status_code if resp is not None else 0
            logger.debug("crawl get %s HTTP %s", url, code)
            self._remember_dead(key, code)
            return None
        text = r.text or ""
        final = str(r.url)
        hdrs = {str(k): str(v) for k, v in r.headers.items()}
        return _Fetched(
            text=text, final_url=final, status_code=r.status_code, headers=hdrs
        )

    def throttled_get(
        self,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str],
    ) -> requests.Response | None:
        """
        Single GET with session + per-host throttle only (no L1/DB cache).
        For binary responses, logos, or HTML that should not share the text cache.
        """
        _, host = normalize_request_url(url)
        if not host:
            return None
        self._throttle.wait(host)
        try:
            r = self._session.get(url, timeout=timeout, headers=headers, allow_redirects=True)
        except requests.RequestException as exc:
            logger.debug("crawl throttled_get %s: %s", url, exc)
            return None
        self.requests_made += 1
        return r

    def get_text(self, url: str, *, timeout: int = 18) -> str | None:
        key, host = normalize_request_url(url)
        if not key or not host:
            return None
        l1 = self._l1_pop_stale(key)
        if self._l1_negative_hit(l1):
            return None
        if l1 is not None and l1.status_code == 200:
            self.cache_hits += 1
            return l1.body_text or None
        row = self._load_cache_row(key)
        if row is not None and row.status_code == 200:
            self.cache_hits += 1
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
            return row.body_text or None
        headers = {"User-Agent": self._ua_text(), "Accept": "*/*, application/xml;q=0.9"}
        fetched = self._request_once(url, timeout=timeout, headers=headers)
        if fetched is None:
            return None
        self._write_cache(
            key=key,
            hostname=host,
            status_code=fetched.status_code,
            final_url=fetched.final_url,
            headers=fetched.headers,
            text=fetched.text,
        )
        return fetched.text

    def _get_html_dual_ua(self, url: str, *, timeout: int) -> tuple[str, str] | None:
        key, host = normalize_request_url(url)
        if not key or not host:
            return None
        l1 = self._l1_pop_stale(key)
        if self._l1_negative_hit(l1):
            return None
        if l1 is not None and l1.status_code == 200:
            ct = str((l1.headers_json or {}).get("Content-Type", "")).lower()
            if "html" in ct:
                self.cache_hits += 1
                return (l1.body_text or "", l1.final_url or key)
        row = self._load_cache_row(key)
        if row is not None and row.status_code == 200:
            ct = str((row.headers_json or {}).get("Content-Type", "")).lower()
            if "html" in ct:
                self.cache_hits += 1
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
                return (row.body_text or "", row.final_url or key)

        headers_base = {
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        for ua in (self._ua_html(), self._ua_browser()):
            fetched = self._request_once(
                url,
                timeout=timeout,
                headers={**headers_base, "User-Agent": ua},
            )
            if fetched is None:
                continue
            ct = fetched.headers.get("Content-Type", "")
            if "html" not in ct.lower():
                logger.debug("crawl html %s non-html Content-Type: %s", url, ct)
                continue
            self._write_cache(
                key=key,
                hostname=host,
                status_code=fetched.status_code,
                final_url=fetched.final_url,
                headers=fetched.headers,
                text=fetched.text,
            )
            return (fetched.text, fetched.final_url)
        return None

    def get_html_document_browser_single(
        self, url: str, *, timeout: int = 8
    ) -> tuple[str | None, str | None]:
        """
        One GET with browser UA only (no bot attempt, no second UA retry).
        Uses the same HTML cache keys as dual-UA fetches. For --fast discovery only.
        """
        key, host = normalize_request_url(url)
        if not key or not host:
            return None, None
        l1 = self._l1_pop_stale(key)
        if self._l1_negative_hit(l1):
            return None, None
        if l1 is not None and l1.status_code == 200:
            ct = str((l1.headers_json or {}).get("Content-Type", "")).lower()
            if "html" in ct:
                self.cache_hits += 1
                return (l1.body_text or "", l1.final_url or key)
        row = self._load_cache_row(key)
        if row is not None and row.status_code == 200:
            ct = str((row.headers_json or {}).get("Content-Type", "")).lower()
            if "html" in ct:
                self.cache_hits += 1
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
                return (row.body_text or "", row.final_url or key)

        headers_base = {
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": self._ua_browser(),
        }
        fetched = self._request_once(url, timeout=timeout, headers=headers_base)
        if fetched is None:
            return None, None
        ct = fetched.headers.get("Content-Type", "")
        if "html" not in ct.lower():
            logger.debug("crawl html fast %s non-html Content-Type: %s", url, ct)
            return None, None
        self._write_cache(
            key=key,
            hostname=host,
            status_code=fetched.status_code,
            final_url=fetched.final_url,
            headers=fetched.headers,
            text=fetched.text,
        )
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
