"""Fetch a short list of URLs through the public-proxy pool, with retries.

The pool (:mod:`keel_crawler.proxy.pool`) hands out addresses one at a time and
leaves the retry policy to the caller. Small scheduled jobs (a few feeds or price
series every few hours) all want the same policy, so it lives here once:

    from keel_crawler.proxy.batch import fetch_many

    bodies = fetch_many(urls, accept=lambda status, body: status == 200 and "<rss" in body)
    # {url: body or None}

It holds the host-wide :func:`harvest_lock` for the whole run, verifies the pool
against the first URL (the real target, as the pool's rules ask), and tries each
URL on up to ``tries`` different addresses. A URL that never passes ``accept``
maps to ``None``; the caller decides what a missing answer means. Direct egress
is never used: with no live address the batch returns ``None`` for what is left.
"""
from __future__ import annotations

from keel_crawler.proxy.jsonstore import harvest_lock
from keel_crawler.proxy.pool import ProxyPool, fetch_through


def fetch_many(urls, *, accept, probe_url: str | None = None, tries: int = 4,
               timeout: float = 20.0, want: int = 8, candidates: int = 400,
               workers: int = 24, max_wait: float = 90.0, progress=None,
               store=None) -> dict:
    """Return ``{url: body or None}`` for every URL, fetched through the pool.

    ``want``, ``candidates`` and ``workers`` are kept small by default: a job of a
    dozen requests needs a handful of addresses, and a wide verification sweep
    costs memory on a busy host (each check is one ``curl`` process).
    """
    urls = list(dict.fromkeys(urls))
    out: dict = {u: None for u in urls}
    if not urls:
        return out

    def check(status, body):
        return bool(accept(status, body))

    with harvest_lock():
        kwargs = {"want": want, "candidates": candidates, "workers": workers,
                  "accept": check, "progress": progress}
        if store is not None:
            kwargs["store"] = store
        pool = ProxyPool.build(probe_url or urls[0], **kwargs)
        try:
            for url in urls:
                for _ in range(max(1, tries)):
                    proxy = pool.acquire(max_wait=max_wait)
                    if proxy is None:
                        break
                    try:
                        status, body = fetch_through(proxy, url, timeout=timeout)
                    finally:
                        pool.release(proxy)
                    if check(status, body):
                        pool.report_ok(proxy)
                        out[url] = body
                        break
                    if status in (401, 403, 429):
                        pool.report_blocked(proxy)
                    else:
                        pool.report_failure(proxy)
        finally:
            pool.stop()
    return out
