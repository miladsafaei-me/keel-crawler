"""keel-crawler Django models.

Today this is only the shared HTTP response cache consumed by :class:`HttpFetcher`.
The generic ``CrawlJob`` status-machine model (Layer 3) lands in a later version.
"""
from __future__ import annotations

from django.db import models

from keel_crawler.config import crawler_setting


class CrawlHttpCache(models.Model):
    """Shared HTTP response cache for crawl fetchers (per normalized URL).

    Business-blind: a plain ``normalized_url -> response`` store with a TTL. The
    table name comes from ``KEEL_CRAWLER["http_cache_db_table"]`` so a host can
    adopt an existing cache table (e.g. Revenika's ``core_crawl_http_cache``)
    without a data migration.
    """

    normalized_url = models.CharField(max_length=2048, unique=True)
    hostname = models.CharField(max_length=253, db_index=True)
    status_code = models.PositiveSmallIntegerField()
    final_url = models.CharField(max_length=2048, blank=True, default="")
    headers_json = models.JSONField(default=dict, blank=True)
    body_text = models.TextField(blank=True, default="")
    body_truncated = models.BooleanField(default=False)
    sha256_hex = models.CharField(max_length=64, blank=True, default="")
    fetched_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = crawler_setting("http_cache_db_table")
        indexes = [
            models.Index(fields=["hostname", "expires_at"]),
        ]
        verbose_name = "Crawl HTTP cache entry"
        verbose_name_plural = "Crawl HTTP cache entries"

    def __str__(self) -> str:
        return self.normalized_url[:80]
