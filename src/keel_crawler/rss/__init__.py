"""Layer 4 — RSS/Atom source monitoring (transport + deterministic pre-filter).

keel-crawler owns the *transport*: poll the feed watchlist, dedup on guid, stage
candidates, and apply a deterministic (LLM-free) pre-filter. The editorial "is this
worth publishing?" LLM judgement is a host hook (:func:`run_triage`) — that logic
lives in keel-content (its generalized twitter monitor->triage pipeline), not here.

``feedparser`` is behind the ``[rss]`` extra and imported lazily.
"""
from keel_crawler.rss.filters import apply_deterministic_filter
from keel_crawler.rss.monitor import poll_feeds
from keel_crawler.rss.triage import run_triage

__all__ = ["poll_feeds", "apply_deterministic_filter", "run_triage"]
