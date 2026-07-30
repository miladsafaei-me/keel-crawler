"""Poll the feed watchlist, dedup on guid, stage new items. No LLM, no selection.

Uses ``feedparser`` (``[rss]`` extra). When a Layer-0 ``HttpFetcher`` is passed, feed
bytes are fetched through it (shared throttle/UA/cache); otherwise feedparser fetches
the URL itself.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any, Iterable, Optional

from django.utils import timezone

from keel_crawler.config import rss_setting
from keel_crawler.models import FeedItemCandidate, FeedSource

logger = logging.getLogger(__name__)


def _parse_published(entry: Any) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st is not None:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=dt_timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _entry_guid(entry: Any) -> str:
    guid = (entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
    return guid[:800]


def _build_candidate(source: FeedSource, entry: Any, guid: str) -> FeedItemCandidate:
    """Build (unsaved) a candidate row from a feed entry."""
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    return FeedItemCandidate(
        guid=guid,
        source=source,
        title=(entry.get("title") or "").strip()[:500],
        link=(entry.get("link") or "").strip()[:1000],
        summary=summary,
        author=(entry.get("author") or "").strip()[:200],
        published_at=_parse_published(entry),
        raw={"tags": [t.get("term") for t in (entry.get("tags") or []) if t.get("term")]},
    )


def _stage_entries(source: FeedSource, entries: list[Any]) -> int:
    """Stage all unseen entries for one feed in a single query + one bulk insert.

    Replaces the per-entry ``get_or_create`` (N selects + N inserts) with one
    ``guid__in`` lookup and one ``bulk_create``. ``ignore_conflicts`` absorbs the
    race where a concurrent poll inserts the same guid between the lookup and write.
    """
    by_guid: dict[str, FeedItemCandidate] = {}
    for entry in entries:
        guid = _entry_guid(entry)
        if guid and guid not in by_guid:  # first spelling wins within one feed
            by_guid[guid] = _build_candidate(source, entry, guid)
    if not by_guid:
        return 0
    existing = set(
        FeedItemCandidate.objects.filter(guid__in=list(by_guid)).values_list("guid", flat=True)
    )
    fresh = [cand for guid, cand in by_guid.items() if guid not in existing]
    if fresh:
        FeedItemCandidate.objects.bulk_create(fresh, ignore_conflicts=True, batch_size=500)
    return len(fresh)


def poll_feeds(
    *,
    sources: Iterable[FeedSource] | None = None,
    http_fetcher: Any = None,
    max_items_per_feed: int | None = None,
) -> dict:
    """Poll active feeds and stage new items. Returns a small stats dict."""
    try:
        import feedparser
    except Exception as exc:  # pragma: no cover - only when [rss] extra missing
        raise RuntimeError(
            "feedparser is required for RSS monitoring: install keel-crawler[rss]"
        ) from exc

    if sources is None:
        sources = FeedSource.objects.filter(is_active=True)
    cap = int(max_items_per_feed if max_items_per_feed is not None else rss_setting("max_items_per_feed"))

    stats = {"feeds": 0, "new_items": 0, "seen_items": 0, "errors": 0}
    for source in sources:
        stats["feeds"] += 1
        status = "ok"
        try:
            if http_fetcher is not None:
                raw = http_fetcher.get_text(source.url)
                parsed = feedparser.parse(raw or "")
            else:
                parsed = feedparser.parse(source.url)
            entries = list(parsed.entries or [])[: max(0, cap)]
            new_count = _stage_entries(source, entries)
            stats["new_items"] += new_count
            stats["seen_items"] += max(0, len(entries) - new_count)
            if getattr(parsed, "bozo", 0) and not entries:
                status = f"parse warning: {getattr(parsed, 'bozo_exception', '')}"[:200]
        except Exception as exc:
            stats["errors"] += 1
            status = f"error: {exc}"[:200]
            logger.warning("poll_feeds: %s failed: %s", source.url, exc)
        source.last_polled_at = timezone.now()
        source.last_status = status
        source.save(update_fields=["last_polled_at", "last_status", "updated_at"])
    return stats
