"""Deterministic (LLM-free) pre-filter over staged feed items.

Cheap-first: drop obviously irrelevant/stale items before any LLM cost, mirroring
the twitter pipeline's deterministic pre-filter. Business vocabulary (allow/deny
keywords) is injected — the package ships no domain words. Survivors stay
``FETCHED`` for the host triage hook; dropped items become ``FILTERED_OUT`` with a
short ``filter_reason``.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional

from django.utils import timezone

from keel_crawler.config import rss_setting
from keel_crawler.models import FeedItemCandidate


def _haystack(item: FeedItemCandidate) -> str:
    return f"{item.title}\n{item.summary}".lower()


def apply_deterministic_filter(
    queryset=None,
    *,
    allow_keywords: Optional[Iterable[str]] = None,
    deny_keywords: Optional[Iterable[str]] = None,
    recency_hours: Optional[int] = None,
    min_source_weight: Optional[int] = None,
) -> dict:
    """Mark stale/off-topic FETCHED items as FILTERED_OUT. Returns a stats dict.

    Any argument left ``None`` falls back to ``KEEL_CRAWLER["rss"]`` (allow/deny
    default to empty = no keyword gating; recency defaults to the configured window).
    """
    allow = [k.lower() for k in (allow_keywords if allow_keywords is not None else rss_setting("allow_keywords"))]
    deny = [k.lower() for k in (deny_keywords if deny_keywords is not None else rss_setting("deny_keywords"))]
    hours = int(recency_hours if recency_hours is not None else rss_setting("recency_hours"))
    min_weight = min_source_weight  # None => no source-weight gate

    if queryset is None:
        queryset = FeedItemCandidate.objects.filter(status=FeedItemCandidate.Status.FETCHED)

    cutoff = timezone.now() - timedelta(hours=hours) if hours > 0 else None
    stats = {"checked": 0, "filtered_out": 0, "passed": 0}

    to_filter: list[FeedItemCandidate] = []
    for item in queryset.select_related("source"):
        stats["checked"] += 1
        reason = ""
        if cutoff is not None and item.published_at is not None and item.published_at < cutoff:
            reason = "stale"
        elif min_weight is not None and item.source.weight < int(min_weight):
            reason = "low_source_weight"
        else:
            hay = _haystack(item)
            hit_deny = next((k for k in deny if k and k in hay), "")
            if hit_deny:
                reason = f"deny:{hit_deny}"
            elif allow and not any(k and k in hay for k in allow):
                reason = "no_allow_keyword"

        if reason:
            item.status = FeedItemCandidate.Status.FILTERED_OUT
            item.filter_reason = reason[:200]
            item.updated_at = timezone.now()
            to_filter.append(item)
            stats["filtered_out"] += 1
        else:
            stats["passed"] += 1

    if to_filter:
        FeedItemCandidate.objects.bulk_update(
            to_filter, ["status", "filter_reason", "updated_at"], batch_size=500
        )
    return stats
