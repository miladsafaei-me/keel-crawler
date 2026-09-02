"""A small lock-guarded JSON file, shared by everything in this subpackage.

Extracted from ``proxy/scores.py`` when ``proxy/pool.py`` needed the same thing:
several processes reading and writing one JSON file without losing each other's
updates. Copying it would have been two implementations of a concurrency
primitive, which is the kind of duplication that diverges quietly and is then
very hard to debug.

The lock is an exclusive ``flock`` held across the whole read-modify-write, not
just the write, because the interesting races are read-then-write ones: two
processes that both read the old set of proxies and both write their own version
of it, and the second silently discards the first's work.

Falls back to plain file access where ``fcntl`` does not exist (non-POSIX). That
fallback is not safe against concurrent writers and is not pretending to be — it
keeps single-process use working on platforms without the primitive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_READ_BYTES = 8_000_000


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True)


def data_dir(namespace: str = "keel_crawler") -> Path:
    """The XDG data directory this package keeps its state in."""
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / namespace


class locked:
    """Context manager: exclusive-lock a JSON file, yield a read/write handle."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fd = None
        self._fcntl = None

    def __enter__(self):
        try:
            import fcntl

            self._fcntl = fcntl
        except ImportError:
            self._fcntl = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._fcntl is None:
            return PlainHandle(self._path)
        self._fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        self._fcntl.flock(self._fd, self._fcntl.LOCK_EX)
        return FdHandle(self._fd)

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
        return False


class FdHandle:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def read(self) -> object:
        os.lseek(self._fd, 0, os.SEEK_SET)
        raw_b = os.read(self._fd, MAX_READ_BYTES)
        raw = raw_b.decode("utf-8", errors="replace") if raw_b else ""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 - a corrupt store must not be fatal
            return {}

    def write(self, data: dict) -> None:
        payload = dumps(data).encode("utf-8")
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, payload)


class PlainHandle:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def read(self) -> object:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def write(self, data: dict) -> None:
        self._path.write_text(dumps(data), encoding="utf-8")
