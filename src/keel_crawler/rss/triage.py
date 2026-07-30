"""LLM triage seam — delegated to the host (keel-content), never done in-package.

Per the layer boundary, keel-crawler stops at "clean, deduped, staged, pre-filtered
item". The editorial LLM judgement (score + select/discard + reason) belongs to
keel-content's triage pipeline. ``run_triage`` resolves the host hook from
``KEEL_CRAWLER["rss"]["triage_hook"]`` (a dotted path) and hands it the survivors of
the deterministic filter. With no hook configured it is a documented no-op.

The hook signature: ``hook(queryset) -> dict`` (stats). The hook is responsible for
setting each item's ``status`` (SELECTED/DISCARDED), ``relevance_score``, and
``triage_reason``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from keel_crawler.config import rss_setting
from keel_crawler.models import FeedItemCandidate

logger = logging.getLogger(__name__)


def _resolve_hook(triage_hook: Optional[Callable | str]) -> Optional[Callable]:
    if callable(triage_hook):
        return triage_hook
    dotted = triage_hook if triage_hook is not None else rss_setting("triage_hook")
    if not dotted:
        return None
    try:
        from django.utils.module_loading import import_string

        return import_string(dotted)
    except Exception:
        logger.warning("run_triage: could not import triage_hook %r", dotted, exc_info=True)
        return None


def run_triage(queryset=None, *, triage_hook: Optional[Callable | str] = None) -> dict:
    """Hand FETCHED (post-filter) items to the host triage hook. No-op when unset."""
    if queryset is None:
        queryset = FeedItemCandidate.objects.filter(status=FeedItemCandidate.Status.FETCHED)

    hook = _resolve_hook(triage_hook)
    if hook is None:
        pending = queryset.count()
        logger.info("run_triage: no triage_hook configured; %d item(s) left FETCHED", pending)
        return {"triaged": 0, "skipped": pending, "note": "no triage_hook configured"}

    result = hook(queryset)
    return result if isinstance(result, dict) else {"triaged": None}
