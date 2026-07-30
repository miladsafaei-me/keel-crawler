"""Async pacing primitives: a global rate limiter and a per-host throttle.

These let a parallel crawl run several pages at once **without** a sudden burst: the
rate limiter spaces the *start* of each unit of work evenly over time (so a day's
budget is spread across the day rather than spent in one spike), while the per-host
throttle keeps a minimum gap between hits to the same site (politeness / 429
avoidance). Both are opt-in — a rate of 0 (or interval of 0) disables them.

Time comes from ``time.monotonic()``; there is no wall-clock dependency, so behaviour
is stable regardless of timezone or clock changes.
"""
from __future__ import annotations

import asyncio
import time

from keel_crawler.normalize import hostname_of


class AsyncRateLimiter:
    """Evenly-spaced global throttle: at most ``rate_per_minute`` acquisitions per minute.

    Spacing (not bucketing) is intentional — it turns a batch of N URLs into a steady
    trickle (one every ``60/rate`` seconds) instead of a burst, so downstream LLM
    token spend is spread over time. Raise ``rate_per_minute`` to go faster; set 0 to
    disable pacing entirely.
    """

    def __init__(self, rate_per_minute: float = 0.0) -> None:
        self._rate = max(0.0, float(rate_per_minute))
        self._interval = 60.0 / self._rate if self._rate > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    @property
    def rate_per_minute(self) -> float:
        return self._rate

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
            delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)


class AsyncHostThrottle:
    """Minimum interval between requests to the same hostname (async, per-host lock)."""

    _MAX_TRACKED_HOSTS = 8192

    def __init__(self, min_interval_seconds: float = 0.0) -> None:
        self._min = max(0.0, float(min_interval_seconds))
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def _prune(self) -> None:
        keep = sorted(self._last.items(), key=lambda kv: kv[1])[len(self._last) // 2 :]
        keep_hosts = {h for h, _ in keep}
        self._last = dict(keep)
        self._locks = {h: lk for h, lk in self._locks.items() if h in keep_hosts}

    async def wait(self, url: str) -> None:
        if self._min <= 0:
            return
        host = hostname_of(url)
        if not host:
            return
        if host not in self._locks and len(self._locks) >= self._MAX_TRACKED_HOSTS:
            self._prune()
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait_s = self._min - (now - self._last.get(host, 0.0))
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            self._last[host] = time.monotonic()
