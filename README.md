# keel-crawler

Reusable, **business-blind** web-crawling toolkit for Keel consumer projects —
extracted from the Revenika / Propopedia / Binarystyle crawlers.

The package is layered so a consumer pulls only what it needs. Layers 0 and 2 ship
today; the rest land in later versions (see the roadmap).

| Layer | Concern | Status |
|---|---|---|
| **0 — Fetch** | Cheap-first `HttpFetcher`: `requests` + per-host throttle + DB response cache + dual-UA HTML. Browser/anti-bot `BrowserFetcher` behind the `browser` extra. | 0 ✅ · browser 🚧 |
| **1 — Resilience / anti-bot** | Retry ladder (transient → egress swap → proxy rotation), Cloudflare/DataDome/429 classifiers, Mihomo proxy client + scoring, egress-IP probe, pluggable captcha solver hook. | 🚧 |
| **2 — Normalization** | HTML/Markdown → LLM-ready text; snapshot storage (store separated from prompt-wrap). | markdown ✅ · snapshot 🚧 |
| **3 — Orchestration** | Generic `CrawlJob` status-machine model, batch, `CrawlSpec`, progress protocol. | progress ✅ · rest 🚧 |
| **4 — Source monitoring (RSS)** | `feedparser` poll → dedup → stage → deterministic pre-filter. LLM triage/selection stays in **keel-content** (generalized twitter pipeline). | 🚧 |

## Install (consumer)

Pin by git tag in `requirements.txt`:

```
keel-crawler @ git+https://github.com/miladsafaei-me/keel-crawler@v0.1.0
# heavy backends are opt-in:
#   keel-crawler[browser] @ git+...   # crawl4ai + Playwright
#   keel-crawler[rss]     @ git+...   # feedparser
```

Add `keel_crawler` to `INSTALLED_APPS`, then `migrate`.

## Configure

Everything is optional; the defaults make the package run standalone. See
`keel_crawler/config.py`:

```python
KEEL_CRAWLER = {
    "user_agent_text": "MyAppCrawlBot/1.0 (+https://myapp.example; fetch)",
    "cache_ttl_seconds": 86_400,
    # Adopt a host's pre-existing cache table with no data migration:
    "http_cache_db_table": "core_crawl_http_cache",
    "adopt_existing": True,
}
```

## Use (Layer 0 + 2)

```python
from keel_crawler import HttpFetcher
from keel_crawler.clean import optimize_markdown_for_llm

fetcher = HttpFetcher(min_interval_per_host=1.0)
html = fetcher.get_html_document("https://example.com/")

# LLM-ready pruning; pass your own domain vocabulary to rescue short data lines:
md = optimize_markdown_for_llm(raw_markdown, vital_keywords={"cpa", "pips", "mt4"})
```

## Versioning

git-tag based, no PyPI. Tag `vX.Y.Z` must equal `pyproject` version; any `src/`
change must bump. Enforced by `.github/workflows/version-guard.yml`. Release with
`~/www/keel-kit/scripts/keel-release.sh <ver>`.
