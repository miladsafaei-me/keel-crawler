"""Persist merged crawl text per domain as a single ``{domain}.md`` snapshot.

Extracted from Revenika's platform_crawler_snapshots and made business-blind:

* **Storage is separated from prompt-wrapping.** Revenika baked its forex Gemini
  extraction prompt into every saved file; here ``SnapshotStore.save_markdown`` writes
  exactly the body you give it (after light whitespace normalization), with an
  optional ``header`` you supply. The LLM prompt, if any, is the caller's to prepend.
* **Merge ordering is injectable.** ``build_merged_markdown`` keeps input order by
  default; pass ``order_key`` for domain priorities (Revenika ordered partnership/IB
  pages first — that heuristic stays in the consumer).

Path safety: a strict domain/basename allowlist + a realpath parent check guard
against traversal, so a hostile ``domain`` can never escape the snapshot root.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from keel_crawler.clean.markdown import format_raw_crawl_snapshot_markdown

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(\.(?!-)[a-z0-9-]{1,63})*$", re.IGNORECASE
)
_BASENAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$", re.IGNORECASE)


def normalize_domain_from_url(url: str) -> Optional[str]:
    """Registrable-ish host from a URL (drops ``www.`` + port), or ``None`` if invalid."""
    try:
        netloc = urlparse((url or "").strip()).netloc.lower()
    except Exception:
        return None
    if not netloc:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if not netloc or not _DOMAIN_RE.match(netloc):
        return None
    return netloc


def _page_url_text(page: Any) -> tuple[str, str]:
    """Accept a dict ``{url,text}`` or any object with ``.url``/``.text`` (e.g. CrawledPage)."""
    if isinstance(page, dict):
        url = page.get("url") if isinstance(page.get("url"), str) else ""
        text = page.get("text") if isinstance(page.get("text"), str) else ""
    else:
        url = getattr(page, "url", "") or ""
        text = getattr(page, "text", "") or ""
    return (url or ""), (text or "")


def build_merged_markdown(
    pages: Iterable[Any],
    *,
    domain: str,
    generator: str = "keel-crawler",
    order_key: Optional[Callable[[Any], Any]] = None,
) -> str:
    """Merge page texts into one Markdown doc with per-source sections + a header comment.

    ``pages`` items may be dicts (``{"url","text"}``) or ``CrawledPage`` objects.
    ``order_key`` (optional) is a stable sort key over the items (e.g. priority first).
    """
    items = [p for p in pages if p is not None]
    if order_key is not None:
        indexed = list(enumerate(items))
        indexed.sort(key=lambda it: (order_key(it[1]), it[0]))
        items = [p for _, p in indexed]

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"<!-- {generator} snapshot domain={domain} generated_utc={generated} -->",
        "",
        f"# {domain}",
        "",
        f"_Merged crawl output ({len(items)} page(s))._",
        "",
    ]
    for p in items:
        url, text = _page_url_text(p)
        lines.append(f"### Source: [{url}] ###")
        lines.append("")
        lines.append(text.strip() or "_[empty]_")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class SnapshotStore:
    """Read/write ``{domain}.md`` snapshots under a root directory (traversal-safe)."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)

    @property
    def root(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    @staticmethod
    def basename_for_domain(domain: str, *, suffix: str = "") -> str:
        base = (domain or "").strip().lower()
        if not base or not _DOMAIN_RE.match(base):
            raise ValueError(f"invalid domain for snapshot filename: {domain!r}")
        name = f"{base}{suffix}.md"
        if not _BASENAME_RE.match(name) or ".." in name:
            raise ValueError(f"invalid snapshot basename: {name!r}")
        return name

    def _resolved_path(self, basename: str) -> Path:
        if not _BASENAME_RE.match(basename) or ".." in basename:
            raise ValueError(f"unsafe snapshot basename: {basename!r}")
        root = self.root.resolve()
        path = (root / basename).resolve()
        if path.parent != root:
            raise ValueError("snapshot path escaped the root directory")
        return path

    def save_markdown(
        self, domain: str, body: str, *, suffix: str = "", header: str | None = None
    ) -> Path:
        """Write ``{domain}{suffix}.md``. ``header`` (optional) is prepended verbatim.

        The body is whitespace-normalized only (no content stripping) so the saved
        file is a faithful, readable snapshot.
        """
        basename = self.basename_for_domain(domain, suffix=suffix)
        path = self._resolved_path(basename)
        inner = format_raw_crawl_snapshot_markdown(body)
        content = f"{header}\n\n{inner}" if header else inner
        path.write_text(content, encoding="utf-8")
        return path

    def read_markdown(self, domain: str, *, suffix: str = "") -> Optional[str]:
        try:
            basename = self.basename_for_domain(domain, suffix=suffix)
            path = self._resolved_path(basename)
        except ValueError:
            return None
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def list_snapshots(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("*.md") if p.is_file())
