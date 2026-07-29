from __future__ import annotations

import threading
import time
from collections import defaultdict


class HostThrottle:
    """Minimum interval between requests to the same hostname (process-local)."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min = max(0.0, float(min_interval_seconds))
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last: dict[str, float] = {}

    def wait(self, hostname: str) -> None:
        if self._min <= 0:
            return
        host = (hostname or "").lower().strip()
        if not host:
            return
        with self._locks[host]:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            wait_s = self._min - (now - last)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last[host] = time.monotonic()
