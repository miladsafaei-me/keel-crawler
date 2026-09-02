# keel-crawler

Reusable, **business-blind** web-crawling toolkit for Keel consumer projects —
extracted from the Revenika / Propopedia / Binarystyle crawlers.

The package is layered so a consumer pulls only what it needs — all five layers ship
today, plus parallel/paced fetching and automatic URL discovery.

| Layer | Concern | Status |
|---|---|---|
| **0 — Fetch** | Cheap-first `HttpFetcher`: `requests` + per-host throttle + DB response cache + dual-UA HTML. | ✅ |
| **1 — Resilience / anti-bot** | `BrowserFetcher` (crawl4ai) with the retry ladder (transient → egress swap → proxy rotation), Cloudflare/DataDome/429 classifiers, Mihomo proxy client + scoring (disable + reset), the **self-maintaining public-proxy pool** (`proxy.pool`: 16 published lists, target-verified, per-address budgets, self-pruning store — stdlib only, no extra needed), egress-IP probe, pluggable captcha-solver hook, `link_harvest` DOM link discovery. Browser pieces behind the `browser` extra. | ✅ |
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

### The public-proxy pool (`keel_crawler.proxy.pool`)

The Mihomo client above rotates a **configured** egress. This is the other case:
no configured egress, an IP that is already blocked, and a need for many
disposable addresses. **Any Keel project needing proxy rotation should use this
rather than growing its own** — keel-seo's keyword crawler is the first consumer,
via `pip install 'keel-seo[proxies]'`.

```python
from keel_crawler.proxy import ProxyPool, fetch_through

pool = ProxyPool.build("https://example.com/api", want=60, accept=my_accept_fn)
proxy = pool.acquire()          # blocks until an address is within its budget
try:
    status, body = fetch_through(proxy, url)
finally:
    pool.release(proxy)         # always, on every path
pool.report_blocked(proxy)      # the target refused this address
```

Stdlib only, no key. It shells out to `curl`, which speaks SOCKS5 without a
Python dependency and — critically — accepts `--noproxy ""`. That flag is not
optional: **with `NO_PROXY=*` in the environment, both urllib and curl silently
ignore an explicit proxy and connect directly**, so a rotating pool appears to
work while every request leaves from the one address it exists to avoid.

**Where addresses come from.** Sixteen published lists across ten publishers
(`keel_crawler.proxy.sources`), returning ~14,000 entries that de-duplicate to
~8,400 addresses. Diversity is availability, not tidiness: two evaluated
candidates (proxyscrape, geonode) are unroutable from some networks entirely, so
depending on any single list is a silent single point of failure.

**How the store stays healthy.** `ProxyStore` is one lock-guarded JSON file,
shared across projects, and **every write prunes** — there is no maintenance job
to forget:

| Rule | Why |
|---|---|
| Dead addresses kept 24h, then dropped | a negative cache; without it every refresh re-imports and re-tests the same corpses |
| Never-answered addresses dropped after 3 days | the lists publish far more than will ever be checked |
| Once-good addresses retired after 7 days idle | they stop working without ever failing consecutively |
| Per-target blocks forgotten after 24h | a site strict yesterday can be tried again |
| Hard ceiling of 20,000 records | dead before unverified before live, then fewest successes |

**Liveness is per target, not global.** A refusal is recorded against the target
that issued it. Treating one site's block as global would discard most of a good
pool the first time one site got strict — and since the store is shared, it would
discard *other projects'* working addresses too.

**Verification asks the real target**, never a liveness URL: a proxy that
cheerfully fetches `httpbin.org` may still be refused by the endpoint that
matters. Pass `accept=` to say what a real answer looks like, because a captive
portal also returns 200.

**Pacing is per address, on three timescales at once** — 1.5/s, 90/minute,
1,500/hour by default, and those are measured rather than guessed. A burn test
put 30 addresses through five rate cohorts (0.2/s up to unthrottled) and pushed
15,553 requests: **not one was blocked**, which retired an earlier guess that was
18x too conservative. It also showed free proxies self-limit before the endpoint
does — the unthrottled cohort achieved 0.18-1.45 req/s, the same as the 2/s
cohort, because proxy latency sets the pace. The hourly figure is the largest
volume actually tested per address, not an extrapolation: nothing blocked, so
this is a floor, and the true ceiling is still unknown. A single global rate does not achieve this: as addresses are
evicted the survivors absorb the whole rate and inherit exactly the traffic that
got the first ones blocked. Work spreads rather than queues — an address
mid-request is never handed out again, selection is least-recently-used rather
than round-robin, and nothing is issued before its own budget allows. Ceiling ≈
pool size × per-address rate.

**There is no cap on the pool.** `want` defaults to 0, meaning *keep every
address that answers*. Discarding a verified address makes no sense: throughput
is `live addresses x per-address budget`, so a cap on the pool is a cap on the
crawl. The real spend is `candidates` — how many are *tested* — not how many
pass. Set `want` to a number only when a small pool is genuinely wanted.

**It starts before the pool is full.** Verification used to be a blocking phase —
check every candidate, *then* begin — which left the caller idle for minutes and
got worse the more addresses were asked for, because the store hands out its
best-evidence addresses first and each extra one comes from a thinner part of the
queue. `build()` now returns as soon as `start_at` (default 10) addresses answer
and keeps filling to `want` in a background thread, adding each to rotation the
moment it passes. Throughput ramps instead of being paid up front, and an empty
pool that is still filling makes `acquire()` wait rather than declaring a dead
end.

Maintenance and inspection:

```bash
python -m keel_crawler.proxy sources          # which lists are reachable today
python -m keel_crawler.proxy refresh          # pull every list into the store
python -m keel_crawler.proxy check <url>      # verify against a real target
python -m keel_crawler.proxy stats            # counts by status
python -m keel_crawler.proxy prune            # force the ageing policy now
```

Measured 2026-09-02: of 600 sampled addresses 37 answered the target (6%), but
among those actually *alive* two thirds got through — the lists are stale, not
poisoned. A 37-address pool harvested 652 phrases while the host machine's own IP
was still refused.

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
