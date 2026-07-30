"""Layer 3 — orchestration primitives over the generic ``CrawlJob`` model.

A ``CrawlSpec`` describes one unit of work (a URL, a free-form ``label``, some
``metadata``, and an optional ``parse`` callback). ``run_job`` drives it through the
status machine — PENDING -> FETCHING -> PARSING -> SUCCEEDED/FAILED — using an
injected ``fetch_fn`` so the caller chooses the transport (Layer-0 ``HttpFetcher`` or
Layer-1 ``BrowserFetcher``). The parse step and its output schema stay in the host:
keel-crawler never interprets what the page *means*.

Two orchestration shapes:

* **Sequential** — ``run_batch(specs, fetch_fn=...)`` runs one job at a time (one URL
  fetched, parsed, saved, then the next). Simple; fine for a handful of URLs.
* **Parallel** — ``run_batch(specs, batch_fetch_fn=...)`` hands *all* URLs to a batch
  transport at once, so the underlying ``BrowserFetcher.fetch_many`` (bounded
  concurrency + evenly-spaced pacing + per-host politeness) actually runs, then parses
  each result. This is the path that makes the pacing/concurrency engine reachable
  from orchestration — use ``browser_batch_fetch_fn`` / ``http_batch_fetch_fn``.
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

# A batch transport: given many URLs, return one (text_or_None, metadata_dict) per URL,
# in the same order. Lets the fetcher run all URLs concurrently/paced in one call.
BatchFetchFn = Callable[["list[str]"], "list[tuple[Optional[str], dict]]"]

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


def _page_to_result(page: Any) -> tuple[Optional[str], dict]:
    meta = {
        "transport": "browser",
        "final_url": page.url,
        "title": page.title,
        "egress_proxy": page.egress_proxy,
        "egress_ip": page.egress_ip,
        "attempts": getattr(page, "attempts", 1),
    }
    if not page.ok():
        meta["error"] = page.error
        return (None, meta)
    return (page.text, meta)


def browser_fetch_fn(fetcher: Any) -> FetchFn:
    """Adapt a Layer-1 ``BrowserFetcher`` to a **sequential** ``FetchFn``.

    Warning: each call runs ``fetch_one``, which opens its own event loop and tears the
    whole browser pool down afterwards — so a sequential ``run_batch(fetch_fn=...)`` over
    this rebuilds Chromium per URL and gains nothing from the pool. For more than a
    couple of URLs prefer :func:`browser_batch_fetch_fn` with ``run_batch(batch_fetch_fn=...)``,
    which fetches the whole list through one paced, pooled ``fetch_many`` call.
    """

    def _fetch(url: str) -> tuple[Optional[str], dict]:
        return _page_to_result(fetcher.fetch_one(url))

    return _fetch


def browser_batch_fetch_fn(fetcher: Any) -> BatchFetchFn:
    """Adapt a Layer-1 ``BrowserFetcher`` to a ``BatchFetchFn`` via ``fetch_many``.

    This is the adapter that unlocks concurrency + pacing from orchestration: the
    whole URL list is fetched in one ``fetch_many`` call (bounded by ``concurrency``,
    spread by ``rate_per_minute``, polite per host).
    """

    def _fetch(urls: list[str]) -> list[tuple[Optional[str], dict]]:
        return [_page_to_result(p) for p in fetcher.fetch_many(list(urls))]

    return _fetch


def hybrid_batch_fetch_fn(fetcher: Any) -> BatchFetchFn:
    """Adapt a cheap-first ``HybridFetcher`` to a ``BatchFetchFn``.

    Same shape as :func:`browser_batch_fetch_fn` — the hybrid fetcher returns
    ``CrawledPage`` objects too — but most URLs are served over cheap HTTP and only the
    inadequate ones escalate to the browser, so a batch launches Chromium only for the
    pages that actually need it.
    """

    def _fetch(urls: list[str]) -> list[tuple[Optional[str], dict]]:
        return [_page_to_result(p) for p in fetcher.fetch_many(list(urls))]

    return _fetch


def http_batch_fetch_fn(
    fetcher: Any, *, mode: str = "html", max_workers: int | None = None
) -> BatchFetchFn:
    """Adapt a Layer-0 ``HttpFetcher`` to a ``BatchFetchFn`` via its concurrent
    ``fetch_many``.

    ``requests`` blocks per thread, so the whole URL list is fetched over a bounded
    thread pool on the **one shared** fetcher — the HTTP session (connection pool),
    the L1/DB cache, and the per-host throttle are all reused, and independent hosts
    fetch in parallel instead of strictly one-at-a-time.
    """

    def _fetch(urls: list[str]) -> list[tuple[Optional[str], dict]]:
        pairs = fetcher.fetch_many(list(urls), mode=mode, max_workers=max_workers)
        if mode == "text":
            return [(text, {"transport": "http", "mode": "text"}) for text, _ in pairs]
        return [
            (text, {"transport": "http", "mode": "html", "final_url": final_url or ""})
            for text, final_url in pairs
        ]

    return _fetch


def _new_job(spec: CrawlSpec, batch_id: uuid.UUID, now: Any) -> CrawlJob:
    return CrawlJob(
        batch_id=batch_id,
        label=spec.label,
        target_url=spec.url,
        input_snapshot=dict(spec.metadata or {}),
        status=CrawlJob.Status.FETCHING,
        started_at=now,
        attempts=1,
    )


def _create_job(spec: CrawlSpec, batch_id: uuid.UUID) -> CrawlJob:
    job = _new_job(spec, batch_id, timezone.now())
    job.save()
    return job


def _create_jobs_bulk(specs: list[CrawlSpec], batch_id: uuid.UUID) -> list[CrawlJob]:
    """Insert all FETCHING jobs in one query instead of N. Falls back to per-row
    creation if the backend doesn't return primary keys from ``bulk_create`` (needed
    because each job is updated in place afterwards).
    """
    now = timezone.now()
    jobs = CrawlJob.objects.bulk_create([_new_job(s, batch_id, now) for s in specs])
    if jobs and jobs[0].pk is None:  # backend didn't populate PKs — do it the slow way
        return [_create_job(s, batch_id) for s in specs]
    return jobs


# Fields written when a job reaches a terminal state. Shared by the single-row save
# (``run_job``) and the batched ``bulk_update`` (parallel ``run_batch``). ``updated_at``
# is set explicitly in staging because ``bulk_update`` does NOT trigger ``auto_now``.
_TERMINAL_FIELDS = ["status", "error_text", "result_payload", "attempts", "finished_at", "updated_at"]


def _stage_fetch_result(
    job: CrawlJob,
    spec: CrawlSpec,
    text: Optional[str],
    meta: Optional[dict],
    *,
    progress: Any = None,
) -> CrawlJob:
    """Mutate ``job`` to its terminal state from a fetch outcome (parse only; no save).

    Parse happens in-process between two states; the transient PARSING row was written
    and immediately overwritten, so we skip it and land straight on a terminal state.
    The caller persists — either a single ``save`` (sequential) or one ``bulk_update``
    for the whole batch (parallel).
    """
    meta = meta or {}
    attempts = meta.get("attempts")
    if isinstance(attempts, int) and attempts > 0:
        job.attempts = attempts
    now = timezone.now()
    job.finished_at = now
    job.updated_at = now

    if not text:
        job.status = CrawlJob.Status.FAILED
        job.error_text = str(meta.get("error") or "fetch returned no text")
        job.result_payload = {"_fetch": meta}
        return job

    try:
        payload = spec.parse(text, spec) if spec.parse else {"text": text}
        if not isinstance(payload, dict):
            payload = {"result": payload}
    except Exception as exc:
        logger.debug("run_job parse raised for %s", spec.url, exc_info=True)
        job.status = CrawlJob.Status.FAILED
        job.error_text = f"parse error: {exc}"
        job.result_payload = {"_fetch": meta}
        return job

    payload.setdefault("_fetch", meta)
    job.status = CrawlJob.Status.SUCCEEDED
    job.result_payload = payload
    if progress is not None:
        progress.step(f"ok {spec.url[:80]}")
    return job


def _apply_fetch_result(
    job: CrawlJob,
    spec: CrawlSpec,
    text: Optional[str],
    meta: Optional[dict],
    *,
    progress: Any = None,
) -> CrawlJob:
    """Stage one job to its terminal state and persist it in a single write."""
    _stage_fetch_result(job, spec, text, meta, progress=progress)
    job.save(update_fields=_TERMINAL_FIELDS)
    return job


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
    job = _create_job(spec, batch_id or new_batch_id())
    if progress is not None:
        progress.step(f"fetch {spec.url[:80]}")

    try:
        text, meta = fetch_fn(spec.url)
    except Exception as exc:
        logger.debug("run_job fetch raised for %s", spec.url, exc_info=True)
        text, meta = None, {"error": str(exc)}

    return _apply_fetch_result(job, spec, text, meta, progress=progress)


def run_batch(
    specs: list[CrawlSpec],
    *,
    fetch_fn: FetchFn | None = None,
    batch_fetch_fn: BatchFetchFn | None = None,
    batch_id: uuid.UUID | None = None,
    progress: Any = None,
) -> list[CrawlJob]:
    """Run a list of specs under one batch id; return the jobs (input order).

    Pass exactly one transport:

    * ``fetch_fn`` — sequential: fetch+parse each spec in turn.
    * ``batch_fetch_fn`` — parallel: fetch *all* URLs in one call (so a
      ``BrowserFetcher``'s bounded-concurrency, paced ``fetch_many`` actually runs),
      then parse each result.
    """
    if (fetch_fn is None) == (batch_fetch_fn is None):
        raise ValueError("run_batch requires exactly one of fetch_fn or batch_fetch_fn")

    bid = batch_id or new_batch_id()
    if progress is not None:
        progress.phase(f"crawl batch {bid} ({len(specs)} jobs)")

    if fetch_fn is not None:
        return [run_job(s, fetch_fn=fetch_fn, batch_id=bid, progress=progress) for s in specs]

    # Parallel path: create all jobs (one bulk insert), fetch the whole URL list at
    # once, then parse.
    jobs = _create_jobs_bulk(specs, bid)
    if progress is not None:
        progress.step(f"fetch {len(specs)} url(s) in parallel")
    try:
        results = batch_fetch_fn([s.url for s in specs])
    except Exception as exc:
        logger.debug("run_batch batch fetch raised", exc_info=True)
        results = [(None, {"error": str(exc)}) for _ in specs]
    if len(results) != len(specs):  # defensive: a misbehaving adapter
        results = (list(results) + [(None, {"error": "missing batch result"})] * len(specs))[: len(specs)]

    # Parse every result, then persist the whole batch in one bulk_update instead of N
    # individual UPDATEs (the insert was already one bulk_create above).
    staged = [
        _stage_fetch_result(job, spec, text, meta, progress=progress)
        for job, spec, (text, meta) in zip(jobs, specs, results)
    ]
    if staged:
        CrawlJob.objects.bulk_update(staged, _TERMINAL_FIELDS, batch_size=500)
    return staged
