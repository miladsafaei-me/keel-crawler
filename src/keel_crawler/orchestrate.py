"""Layer 3 — orchestration primitives over the generic ``CrawlJob`` model.

A ``CrawlSpec`` describes one unit of work (a URL, a free-form ``label``, some
``metadata``, and an optional ``parse`` callback). ``run_job`` drives it through the
status machine — PENDING -> FETCHING -> PARSING -> SUCCEEDED/FAILED — using an
injected ``fetch_fn`` so the caller chooses the transport (Layer-0 ``HttpFetcher`` or
Layer-1 ``BrowserFetcher``). The parse step and its output schema stay in the host:
keel-crawler never interprets what the page *means*.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from django.utils import timezone

from keel_crawler.models import CrawlJob

logger = logging.getLogger(__name__)

# A transport: given a URL, return (text_or_None, metadata_dict). None text == failure.
FetchFn = Callable[[str], "tuple[Optional[str], dict]"]

# A parser: given (text, spec), return the result_payload dict to persist.
ParseFn = Callable[[str, "CrawlSpec"], dict]


def new_batch_id() -> uuid.UUID:
    return uuid.uuid4()


@dataclass
class CrawlSpec:
    """One crawl unit. ``parse`` (optional) turns fetched text into a result payload."""

    url: str
    label: str = ""
    metadata: dict = field(default_factory=dict)
    parse: Optional[ParseFn] = None


def http_fetch_fn(fetcher: Any, *, mode: str = "html") -> FetchFn:
    """Adapt a Layer-0 ``HttpFetcher`` to a ``FetchFn`` (``mode`` = 'html' or 'text')."""

    def _fetch(url: str) -> tuple[Optional[str], dict]:
        if mode == "text":
            text = fetcher.get_text(url)
            return (text, {"transport": "http", "mode": "text"})
        text, final_url = fetcher.get_html_document_with_final_url(url)
        return (text, {"transport": "http", "mode": "html", "final_url": final_url or ""})

    return _fetch


def browser_fetch_fn(fetcher: Any) -> FetchFn:
    """Adapt a Layer-1 ``BrowserFetcher`` to a ``FetchFn``."""

    def _fetch(url: str) -> tuple[Optional[str], dict]:
        page = fetcher.fetch_one(url)
        meta = {
            "transport": "browser",
            "final_url": page.url,
            "title": page.title,
            "egress_proxy": page.egress_proxy,
            "egress_ip": page.egress_ip,
        }
        if not page.ok():
            meta["error"] = page.error
            return (None, meta)
        return (page.text, meta)

    return _fetch


def run_job(
    spec: CrawlSpec,
    *,
    fetch_fn: FetchFn,
    batch_id: uuid.UUID | None = None,
    progress: Any = None,
) -> CrawlJob:
    """Create and drive one ``CrawlJob`` to a terminal state. Never raises for a
    fetch/parse failure — the failure is recorded on the job and returned.
    """
    job = CrawlJob.objects.create(
        batch_id=batch_id or new_batch_id(),
        label=spec.label,
        target_url=spec.url,
        input_snapshot=dict(spec.metadata or {}),
        status=CrawlJob.Status.FETCHING,
        started_at=timezone.now(),
        attempts=1,
    )
    if progress is not None:
        progress.step(f"fetch {spec.url[:80]}")

    try:
        text, meta = fetch_fn(spec.url)
    except Exception as exc:
        logger.debug("run_job fetch raised for %s", spec.url, exc_info=True)
        text, meta = None, {"error": str(exc)}

    if not text:
        job.status = CrawlJob.Status.FAILED
        job.error_text = str((meta or {}).get("error") or "fetch returned no text")
        job.result_payload = {"_fetch": meta or {}}
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_text", "result_payload", "finished_at", "updated_at"])
        return job

    job.status = CrawlJob.Status.PARSING
    job.save(update_fields=["status", "updated_at"])
    try:
        payload = spec.parse(text, spec) if spec.parse else {"text": text}
        if not isinstance(payload, dict):
            payload = {"result": payload}
    except Exception as exc:
        logger.debug("run_job parse raised for %s", spec.url, exc_info=True)
        job.status = CrawlJob.Status.FAILED
        job.error_text = f"parse error: {exc}"
        job.result_payload = {"_fetch": meta or {}}
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_text", "result_payload", "finished_at", "updated_at"])
        return job

    payload.setdefault("_fetch", meta or {})
    job.status = CrawlJob.Status.SUCCEEDED
    job.result_payload = payload
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "result_payload", "finished_at", "updated_at"])
    if progress is not None:
        progress.step(f"ok {spec.url[:80]}")
    return job


def run_batch(
    specs: list[CrawlSpec],
    *,
    fetch_fn: FetchFn,
    batch_id: uuid.UUID | None = None,
    progress: Any = None,
) -> list[CrawlJob]:
    """Run a list of specs sequentially under one batch id; return the jobs."""
    bid = batch_id or new_batch_id()
    if progress is not None:
        progress.phase(f"crawl batch {bid} ({len(specs)} jobs)")
    return [run_job(s, fetch_fn=fetch_fn, batch_id=bid, progress=progress) for s in specs]
