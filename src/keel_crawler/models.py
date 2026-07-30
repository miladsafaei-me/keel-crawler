"""keel-crawler Django models: the shared HTTP response cache (Layer 0), the
generic ``CrawlJob`` status machine (Layer 3), and the RSS staging models (Layer 4).
"""
from __future__ import annotations

import uuid

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


class CrawlJob(models.Model):
    """A generic, business-blind unit of crawl work + its lifecycle.

    Deliberately carries **no** foreign key to any consumer model — a host links a
    job to its own data by storing an external reference in ``input_snapshot`` (or by
    adding its own FK in its own app). ``label`` is a free-form "kind" tag
    (e.g. ``"broker_review"``, ``"rss_article"``) so one table serves every crawl
    type. The table name comes from ``KEEL_CRAWLER["crawl_job_db_table"]``.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FETCHING = "fetching", "Fetching"
        PARSING = "parsing", "Parsing"
        SUCCEEDED = "succeeded", "Succeeded"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    label = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Free-form crawl kind, e.g. 'broker_review' or 'rss_article'.",
    )
    target_url = models.CharField(max_length=2048, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    input_snapshot = models.JSONField(default=dict, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    error_text = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = crawler_setting("crawl_job_db_table")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["batch_id", "status"]),
            models.Index(fields=["label", "status"]),
        ]
        verbose_name = "Crawl job"
        verbose_name_plural = "Crawl jobs"

    def __str__(self) -> str:
        return f"[{self.label or 'job'}] {self.target_url[:60]} ({self.status})"


class FeedSource(models.Model):
    """An RSS/Atom feed to monitor. The watchlist :func:`poll_feeds` pulls from."""

    url = models.URLField(max_length=1000, unique=True)
    name = models.CharField(max_length=200, blank=True, default="")
    category = models.CharField(
        max_length=100, blank=True, default="", help_text="Free-text grouping tag."
    )
    is_active = models.BooleanField(default=True, db_index=True)
    weight = models.IntegerField(
        default=0, help_text="Source trust weight; used by the deterministic pre-filter."
    )
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "keel_crawler_feed_source"
        ordering = ["name", "url"]
        verbose_name = "Feed source"
        verbose_name_plural = "Feed sources"

    def __str__(self) -> str:
        return self.name or self.url


class FeedItemCandidate(models.Model):
    """One staged feed entry + its selection verdict.

    ``guid`` is the dedup spine (feed entry id, else link) — an item is staged once.
    keel-crawler fills the transport fields and the deterministic pre-filter verdict;
    the LLM selection (score/route/reason) is written by the host triage hook, which
    is where the editorial judgement lives (keel-content).
    """

    class Status(models.TextChoices):
        FETCHED = "fetched", "Fetched (staged, not triaged)"
        FILTERED_OUT = "filtered_out", "Dropped by deterministic pre-filter"
        SELECTED = "selected", "Selected (worth processing)"
        DISCARDED = "discarded", "Discarded by triage"
        PROCESSED = "processed", "Handed off downstream"

    guid = models.CharField(max_length=800, unique=True)
    source = models.ForeignKey(FeedSource, on_delete=models.CASCADE, related_name="items")
    title = models.CharField(max_length=500, blank=True, default="")
    link = models.URLField(max_length=1000, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    author = models.CharField(max_length=200, blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    raw = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=14, choices=Status.choices, default=Status.FETCHED, db_index=True
    )
    relevance_score = models.FloatField(null=True, blank=True)
    triage_reason = models.TextField(blank=True, default="")
    filter_reason = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "keel_crawler_feed_item"
        ordering = ["-published_at", "-created_at"]
        indexes = [models.Index(fields=["status", "published_at"])]
        verbose_name = "Feed item candidate"
        verbose_name_plural = "Feed item candidates"

    def __str__(self) -> str:
        return self.title[:70] or self.guid[:70]
