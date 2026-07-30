from __future__ import annotations

import threading
import time
from typing import TypeVar

_MAX_TRACKED_HOSTS = 8192

_LockT = TypeVar("_LockT")


def prune_host_tracking(
    last: dict[str, float], locks: dict[str, _LockT]
) -> tuple[dict[str, float], dict[str, _LockT]]:
    """Keep the most-recently-seen half of the per-host tracking maps.

    Shared by the sync :class:`HostThrottle` and the async ``AsyncHostThrottle`` (which
    hold ``threading.Lock`` vs ``asyncio.Lock`` respectively, hence the generic lock
    type). Evicting a host that is momentarily mid-wait only restarts its interval — a
    politeness approximation, never a correctness issue.
    """
    if not last:
        return {}, {}
    keep = sorted(last.items(), key=lambda kv: kv[1])[len(last) // 2 :]
    keep_hosts = {h for h, _ in keep}
    return dict(keep), {h: lk for h, lk in locks.items() if h in keep_hosts}


class HostThrottle:
    """Minimum interval between requests to the same hostname (process-local).

    Thread-safe: a master lock guards creation of per-host locks (a bare
    ``defaultdict`` race could hand two threads different lock objects for the same
    host). The tracking dicts are bounded so a long-running crawl over many distinct
    hosts cannot grow them without limit.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self._min = max(0.0, float(min_interval_seconds))
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(host)
            if lock is None:
                if len(self._locks) >= _MAX_TRACKED_HOSTS:
                    self._prune_locked()
                lock = threading.Lock()
                self._locks[host] = lock
            return lock

    def _prune_locked(self) -> None:
        """Drop the least-recently-seen half of the tracking dicts (guard held)."""
        self._last, self._locks = prune_host_tracking(self._last, self._locks)

    def wait(self, hostname: str) -> None:
        if self._min <= 0:
            return
        host = (hostname or "").lower().strip()
        if not host:
            return
        with self._lock_for(host):
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            wait_s = self._min - (now - last)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last[host] = time.monotonic()
