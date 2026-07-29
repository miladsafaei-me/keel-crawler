"""Normalize crawled Markdown for LLM consumption and for on-disk snapshots.

Pure regex, zero Django/business imports. Two families:

* ``clean_crawl_markdown_for_llm`` / ``clean_crawl_markdown_for_snapshot`` /
  ``format_raw_crawl_snapshot_markdown`` — fully generic; move as-is.
* ``optimize_markdown_for_llm`` — token-oriented pruning. The set of
  domain-vital keywords that rescue otherwise-short paragraphs is a **parameter**
  (``vital_keywords``); the host passes its own vocabulary (Revenika passed the
  forex set: cpa, revshare, pips, mt4, cysec, ...). Default: no keyword rescue.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>[\s\S]*?</style>", re.IGNORECASE)
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# ![alt](url) — keep alt text when non-empty
_LINKED_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_STANDALONE_IMAGE_LINE_RE = re.compile(
    r"^\s*!\[[^\]]*\]\([^)]+\)\s*$",
    re.MULTILINE,
)
_REF_LINK_DEF_RE = re.compile(
    r"^\s*\[[^\]]+\]:\s*\S+\s*$",
    re.MULTILINE,
)
_HTML_TAG_LINE_RE = re.compile(r"^\s*<[^>]+>\s*$")


def _linked_image_repl(match: re.Match[str]) -> str:
    alt = (match.group(1) or "").strip()
    return alt


def _norm_block_key(block: str) -> str:
    return re.sub(r"\s+", " ", block.strip().lower())


def _dedupe_lines_globally(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        key = _norm_block_key(line) if line.strip() else ""
        if not key:
            out.append(line)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def _dedupe_paragraphs_globally(text: str) -> str:
    parts = re.split(r"\n{2,}", text)
    seen: set[str] = set()
    kept: list[str] = []
    for p in parts:
        key = _norm_block_key(p) if p.strip() else ""
        if not key:
            kept.append(p)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(p)
    return "\n\n".join(kept)


def clean_crawl_markdown_for_llm(text: str, *, strip_html_tag_lines: bool = True) -> str:
    """
    Strip images, script/style, optional bare HTML tag lines; dedupe repeated lines / paragraphs.

    For per-URL LLM prompt chunks. Raw snapshot files intentionally skip this pass
    (see :func:`format_raw_crawl_snapshot_markdown`).
    """
    if not isinstance(text, str) or not text.strip():
        return ""

    out = text
    out = _SCRIPT_BLOCK_RE.sub("", out)
    out = _STYLE_BLOCK_RE.sub("", out)
    out = _LINKED_IMAGE_RE.sub(_linked_image_repl, out)
    out = _STANDALONE_IMAGE_LINE_RE.sub("", out)
    out = _REF_LINK_DEF_RE.sub("", out)
    out = _HTML_IMG_RE.sub("", out)

    if strip_html_tag_lines:
        lines = []
        for ln in out.split("\n"):
            if _HTML_TAG_LINE_RE.match(ln):
                continue
            lines.append(ln)
        out = "\n".join(lines)

    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]+\n", "\n", out)

    out = _dedupe_lines_globally(out)
    out = re.sub(r"\n{3,}", "\n\n", out)

    out = _dedupe_paragraphs_globally(out)
    out = re.sub(r"\n{3,}", "\n\n", out)

    return out.strip() + "\n"


def _format_snapshot_markdown(text: str) -> str:
    """Trim trailing spaces per line; collapse long blank runs for readable ``.md`` files."""
    if not (text or "").strip():
        return ""
    lines = [ln.rstrip() for ln in text.split("\n")]
    body = "\n".join(lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip() + "\n"


def optimize_markdown_for_llm(
    merged_md: str,
    *,
    vital_keywords: Iterable[str] = (),
    min_paragraph_len: int = 15,
    dedupe_over_len: int = 100,
) -> str:
    """
    Token-oriented cleanup for merged crawl Markdown before LLM extraction.

    Strips markdown images, removes common UTM query keys, drops breadcrumb-style
    lines, filters very short non-data paragraphs, and deduplicates long repeated
    paragraphs (stable hash, process-safe).

    ``vital_keywords`` rescues an otherwise-too-short paragraph if it contains one
    of them — the host supplies its own domain vocabulary (leave empty for none).
    """
    if not isinstance(merged_md, str) or not merged_md.strip():
        return ""

    vital = frozenset(k.lower() for k in vital_keywords)

    cleaned_md = re.sub(r"!\[.*?\]\(.*?\)", "", merged_md)
    cleaned_md = re.sub(
        r"(\?|&)utm_[a-zA-Z0-9_]+=[a-zA-Z0-9_\-%]+", "", cleaned_md
    )
    cleaned_md = re.sub(
        r"^(?:\s*[\w\s]+\s*(?:>|\|)\s*)+[\w\s]+$", "", cleaned_md, flags=re.MULTILINE
    )

    paragraphs = cleaned_md.split("\n\n")
    seen_hashes: set[str] = set()
    optimized_paragraphs: list[str] = []

    for p in paragraphs:
        p_stripped = p.strip()
        if not p_stripped:
            continue

        p_lower = p_stripped.lower()
        if (
            len(p_stripped) < min_paragraph_len
            and not any(kw in p_lower for kw in vital)
            and not re.search(r"\d", p_stripped)
        ):
            continue

        if len(p_stripped) > dedupe_over_len:
            p_hash = hashlib.sha256(p_stripped.encode("utf-8")).hexdigest()
            if p_hash in seen_hashes:
                continue
            seen_hashes.add(p_hash)

        optimized_paragraphs.append(p_stripped)

    return "\n\n".join(optimized_paragraphs)


def format_raw_crawl_snapshot_markdown(text: str) -> str:
    """
    Whitespace-only normalization for persisted raw crawls (no content stripping or dedupe).

    Use for ``{domain}.md`` merged snapshots.
    """
    return _format_snapshot_markdown(text)


def clean_crawl_markdown_for_snapshot(text: str) -> str:
    """
    Full Python clean + format; kept for admin previews and optional future reuse.

    Not applied when writing the default merged crawl snapshot to disk.
    """
    cleaned = clean_crawl_markdown_for_llm(text, strip_html_tag_lines=False)
    return _format_snapshot_markdown(cleaned)
