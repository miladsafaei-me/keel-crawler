"""Optional structured progress reporting for crawl runs (stdout-friendly)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CrawlProgressReporter(Protocol):
    """Phases are high-level stages; steps are individual operations or outcomes."""

    def phase(self, title: str) -> None: ...

    def step(self, detail: str) -> None: ...


class StdoutCrawlProgressReporter:
    """Writes phases and indented steps to a Django command stdout (or any file-like)."""

    __slots__ = ("_stdout",)

    def __init__(self, stdout) -> None:
        self._stdout = stdout

    def phase(self, title: str) -> None:
        self._stdout.write(f"  ▸ {title}")

    def step(self, detail: str) -> None:
        self._stdout.write(f"      · {detail}")
