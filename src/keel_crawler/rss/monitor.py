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


def _stage_entry(source: FeedSource, entry: Any) -> bool:
    """Create one candidate if unseen. Returns True when a new row was created."""
    guid = _entry_guid(entry)
    if not guid:
        return False
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    _, created = FeedItemCandidate.objects.get_or_create(
        guid=guid,
        defaults={
            "source": source,
            "title": (entry.get("title") or "").strip()[:500],
            "link": (entry.get("link") or "").strip()[:1000],
            "summary": summary,
            "author": (entry.get("author") or "").strip()[:200],
            "published_at": _parse_published(entry),
            "raw": {
                "tags": [t.get("term") for t in (entry.get("tags") or []) if t.get("term")],
            },
        },
    )
    return created


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
            for entry in entries:
                if _stage_entry(source, entry):
                    stats["new_items"] += 1
                else:
                    stats["seen_items"] += 1
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
