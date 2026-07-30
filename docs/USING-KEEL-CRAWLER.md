# Using keel-crawler — a playbook

This guide is for **directing an assistant (Claude Code) to use keel-crawler in a
project**. It shows how to phrase requests, what each capability does, and copy-paste
recipes. keel-crawler is a *reusable, business-blind* crawling toolkit: it fetches
pages past anti-bot defenses, turns them into clean LLM-ready text, stores snapshots,
runs crawl jobs, and monitors RSS feeds. What the data *means* (extraction schemas,
keyword vocabularies, feed lists, editorial selection) is always your project's job —
you inject it through config or hooks.

## Mental model — five layers

| Layer | You get | Extra needed |
|---|---|---|
| **0 Fetch** | `HttpFetcher` — cheap `requests` fetch + throttle + DB cache | none |
| **1 Anti-bot** | `BrowserFetcher` — real Chromium, Cloudflare wait, direct↔proxy swap, proxy rotation, captcha seam | `[browser]` |
| **2 Clean** | Markdown cleaning + `SnapshotStore` (`{domain}.md`) | none (`[browser]` for lxml link harvest) |
| **3 Orchestrate** | `CrawlJob` status machine + `CrawlSpec`/`run_batch` | none |
| **4 RSS** | Feed poll → dedup → stage → deterministic pre-filter; LLM triage is your hook | `[rss]` |

The golden rule: **start cheap, escalate only when blocked.** Try `HttpFetcher`
first; reach for `BrowserFetcher` when a site is JS-heavy or behind Cloudflare.

## How to ask Claude — request → what gets wired

Phrase the *goal*; the assistant picks the layer. Useful requests:

- **"Crawl these review pages and give me clean markdown for LLM extraction."**
  → `BrowserFetcher.from_config()` (JS/anti-bot) → `page.text`, optionally saved as a
  `{domain}.md` snapshot. You still own the extraction prompt/schema.
- **"These sites are behind Cloudflare / keep blocking me — get through."**
  → enable the proxy + Mihomo config, let the ladder swap egress and rotate proxies.
- **"Set up a crawl job over this list of URLs and store the results."**
  → `CrawlSpec` per URL + `run_batch(...)`, results land in the `CrawlJob` table.
- **"Monitor these RSS feeds and keep the finance-relevant items."**
  → seed `FeedSource` rows, run `crawler_rss_poll`, wire a `triage_hook` for the LLM
  selection (that hook lives in keel-content).
- **"Harvest all the menu/nav links from this site."**
  → `BrowserFetcher(run_profile="link_harvest")` → `page.discovery_hrefs`.
- **"The proxy scores look off — reset them"** / **"stop scoring proxies."**
  → `crawler_proxy_scores --reset`, or set `proxy_scoring_enabled=False`.

You do **not** need to name modules; "use keel-crawler to …" is enough. Name a layer
only to force a choice (e.g. "use the cheap HTTP fetcher, don't launch a browser").

## Install & configure

`requirements.txt`:

```
keel-crawler @ git+https://github.com/miladsafaei-me/keel-crawler@v0.3.0
# opt-in heavy backends:
#   keel-crawler[browser] @ git+...   # crawl4ai + Playwright + lxml
#   keel-crawler[rss]     @ git+...   # feedparser
```

Add `keel_crawler` to `INSTALLED_APPS`, run `migrate`. For the browser engine also run
`python -m playwright install chromium` once on the host/image.

`settings.py` — everything optional, defaults work standalone:

```python
KEEL_CRAWLER = {
    "user_agent_text": "MyAppCrawlBot/1.0 (+https://myapp.example; fetch)",
    "cache_ttl_seconds": 86_400,

    # anti-bot / proxy (secrets may instead come from .env: LOCAL_PROXY_URL, MIHOMO_*)
    "proxy_url": "http://127.0.0.1:7890",
    "proxy_scoring_enabled": True,
    "mihomo": {"api_url": "http://127.0.0.1:9090", "secret": "…", "group": "AUTO"},

    # last-resort captcha solver you provide (2Captcha/CapSolver/FlareSolverr wrapper)
    "captcha_solver": "myapp.crawl.solve_challenge",

    # RSS: the LLM "is this newsworthy?" hook (lives in keel-content)
    "rss": {"triage_hook": "myapp.news.triage", "recency_hours": 72},
}
```

## Recipes

### 1. Cheap fetch (Layer 0)

```python
from keel_crawler import HttpFetcher
f = HttpFetcher(min_interval_per_host=1.0)
html = f.get_html_document("https://example.com/")   # dual-UA, DB-cached
```

### 2. Anti-bot browser fetch + snapshot (Layers 1+2)

```python
from keel_crawler import BrowserFetcher
from keel_crawler.clean import SnapshotStore, build_merged_markdown, normalize_domain_from_url

fetcher = BrowserFetcher.from_config(run_profile="content")   # prunes nav/ads/images
pages = fetcher.fetch_many([
    "https://tradersunion.com/brokers/forex/view/broker-x/",
    "https://tradersunion.com/brokers/forex/view/broker-x/review/",
])
good = [p for p in pages if p.ok()]

store = SnapshotStore("/data/crawl_snapshots")
domain = normalize_domain_from_url(good[0].url)
store.save_markdown(domain, build_merged_markdown(good, domain=domain))
# -> /data/crawl_snapshots/{domain}.md  (clean, per-source sections, ready for your LLM)
```

The escalation ladder is automatic: cheap egress first → swap direct↔proxy on an
anti-bot block → rotate the Mihomo outbound if the proxy stays blocked → your
`captcha_solver` as the final fallback.

### 3. Crawl-job batch (Layer 3)

```python
from keel_crawler import HttpFetcher
from keel_crawler.orchestrate import CrawlSpec, run_batch, http_fetch_fn, browser_fetch_fn

def parse(text, spec):                      # your schema, your rules
    return {"broker": spec.metadata["name"], "len": len(text)}

specs = [CrawlSpec(url=u, label="broker_review", metadata={"name": n}, parse=parse)
         for u, n in targets]
jobs = run_batch(specs, fetch_fn=http_fetch_fn(HttpFetcher()))
#                              ^ or browser_fetch_fn(BrowserFetcher.from_config())
# each job is a CrawlJob row: status, result_payload, error_text, batch_id.
```

### 4. RSS monitoring (Layer 4)

```python
from keel_crawler.models import FeedSource
FeedSource.objects.create(url="https://www.fxstreet.com/rss/news", name="FXStreet", weight=5)
```
```bash
python manage.py crawler_rss_poll          # poll → stage → deterministic filter → triage hook
```
Deterministic filter keywords come from `KEEL_CRAWLER["rss"]` (`allow_keywords`,
`deny_keywords`, `recency_hours`). The LLM selection is your `triage_hook(queryset)` —
it sets each item's `status`/`relevance_score`/`triage_reason`. Keep that hook in
keel-content, not here.

### 5. Link harvest (Layer 1, `link_harvest` profile)

```python
fetcher = BrowserFetcher(run_profile="link_harvest",
                         link_match=my_partner_filter)   # optional relevance filter
page = fetcher.fetch_one("https://broker.com/")
nav_and_footer_links = page.discovery_hrefs
```

## Proxy operations

```bash
python manage.py crawler_proxy_scores --show               # ranked outbounds + delays
python manage.py crawler_proxy_scores --reset              # clear ALL scores + delays
python manage.py crawler_proxy_scores --reset-outbound X   # clear one
```
Disable persistence entirely: `KEEL_CRAWLER["proxy_scoring_enabled"] = False`
(or env `KEEL_CRAWLER_DISABLE_PROXY_SCORING=1`). Disable rotation: leave `mihomo`
empty, or `KEEL_CRAWLER_DISABLE_MIHOMO=1`.

## Gotchas

- **Captcha is bring-your-own.** keel-crawler ships the *seam*, not a solver — wire a
  paid service (2Captcha/CapSolver) or FlareSolverr behind `captcha_solver`.
- **Browser needs Playwright.** Install `[browser]` **and** run
  `python -m playwright install chromium` on the image; the engine degrades to a clear
  error if crawl4ai is missing, so a missing browser never 500s the whole app.
- **Extraction stays yours.** keel-crawler hands you clean text — the LLM prompt +
  schema that turn it into structured broker/exchange/prop-firm data live in your
  project (or keel-content), never in the package.
- **Adopting an existing cache table?** Set `http_cache_db_table` + `adopt_existing:
  True` so the initial migration is state-only (no `CREATE TABLE`).
