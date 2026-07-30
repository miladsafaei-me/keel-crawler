# keel-crawler

Reusable, **business-blind** web-crawling toolkit for Keel consumer projects —
extracted from the Revenika / Propopedia / Binarystyle crawlers.

The package is layered so a consumer pulls only what it needs — all five layers ship
today, plus parallel/paced fetching and automatic URL discovery.

| Layer | Concern | Status |
|---|---|---|
| **0 — Fetch** | Cheap-first `HttpFetcher`: `requests` + per-host throttle + DB response cache + dual-UA HTML. | ✅ |
| **1 — Resilience / anti-bot** | `BrowserFetcher` (crawl4ai) with the retry ladder (transient → egress swap → proxy rotation), Cloudflare/DataDome/429 classifiers, Mihomo proxy client + scoring (disable + reset), egress-IP probe, pluggable captcha-solver hook, `link_harvest` DOM link discovery. Behind the `browser` extra. | ✅ |
| **2 — Normalization** | HTML/Markdown → LLM-ready text (`clean/markdown.py`) + `SnapshotStore` (`{domain}.md`, traversal-safe, prompt-wrap separated from storage). | ✅ |
| **3 — Orchestration** | Generic `CrawlJob` status machine, `CrawlSpec`, `run_batch`, transport adapters, progress protocol. | ✅ |
| **4 — Source monitoring (RSS)** | `feedparser` poll → dedup → stage → deterministic pre-filter (`rss/`). LLM triage/selection is a host hook → **keel-content**. Behind the `rss` extra. | ✅ |

Cross-cutting: **parallel + paced fetching** (`BrowserFetcher.fetch_many` runs URLs
concurrently under a `concurrency` cap and an evenly-spaced `rate_per_minute` limiter,
so a big batch trickles out instead of spiking) and **URL discovery** (`discover.py` —
sitemap crawling + bounded deep crawl).

**Directing an assistant to use this?** See [docs/USING-KEEL-CRAWLER.md](docs/USING-KEEL-CRAWLER.md) — a request→wiring playbook with recipes.

## Install (consumer)

Pin by git tag in `requirements.txt`:

```
keel-crawler @ git+https://github.com/miladsafaei-me/keel-crawler@v0.4.0
# heavy backends are opt-in:
#   keel-crawler[browser] @ git+...   # crawl4ai + Playwright + lxml
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

## Use (Layer 1 — browser + anti-bot)

```python
from keel_crawler import BrowserFetcher

# Resolves proxy_url + Mihomo creds from KEEL_CRAWLER / .env:
fetcher = BrowserFetcher.from_config(run_profile="content")
page = fetcher.fetch_one("https://tradersunion.com/brokers/forex/view/...")
if page.ok():
    clean_text = page.text     # ready for your LLM extraction prompt (host-owned)
```

The ladder is automatic: cheap egress first, swap direct↔proxy on an anti-bot block,
rotate the Mihomo outbound if the proxy egress stays blocked, then (last resort) the
host captcha solver. Proxy scoring can be **disabled** and **reset**:

```bash
python manage.py crawler_proxy_scores --show
python manage.py crawler_proxy_scores --reset            # clear all scores + delays
python manage.py crawler_proxy_scores --reset-outbound "JP-01"
# disable persistence entirely: KEEL_CRAWLER["proxy_scoring_enabled"] = False
```

## Use (Layer 3 — orchestration)

```python
from keel_crawler import HttpFetcher
from keel_crawler.orchestrate import CrawlSpec, run_batch, http_fetch_fn

def parse_broker(text, spec):
    return {"name": spec.metadata["name"], "chars": len(text)}   # host-owned schema

specs = [CrawlSpec(url=u, label="broker_review", metadata={"name": n}, parse=parse_broker)
         for u, n in targets]
jobs = run_batch(specs, fetch_fn=http_fetch_fn(HttpFetcher()))   # -> CrawlJob rows
```

## Use (Layer 4 — RSS)

```bash
python manage.py crawler_rss_poll        # poll -> stage -> deterministic filter -> triage hook
```

The LLM "is this newsworthy?" step is the host `triage_hook`
(`KEEL_CRAWLER["rss"]["triage_hook"]`, a dotted path) — that logic lives in
keel-content, not here.

## Versioning

git-tag based, no PyPI. Tag `vX.Y.Z` must equal `pyproject` version; any `src/`
change must bump. Enforced by `.github/workflows/version-guard.yml`. Release with
`~/www/keel-kit/scripts/keel-release.sh <ver>`.
