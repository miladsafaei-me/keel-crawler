"""Layer 2 — content normalization (HTML/Markdown -> LLM-ready text) + snapshot storage."""
from keel_crawler.clean.markdown import (
    clean_crawl_markdown_for_llm,
    clean_crawl_markdown_for_snapshot,
    format_raw_crawl_snapshot_markdown,
    optimize_markdown_for_llm,
)
from keel_crawler.clean.snapshot import (
    SnapshotStore,
    build_merged_markdown,
    normalize_domain_from_url,
)

__all__ = [
    "clean_crawl_markdown_for_llm",
    "clean_crawl_markdown_for_snapshot",
    "format_raw_crawl_snapshot_markdown",
    "optimize_markdown_for_llm",
    "SnapshotStore",
    "build_merged_markdown",
    "normalize_domain_from_url",
]
