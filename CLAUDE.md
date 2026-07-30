# CLAUDE.md — keel-crawler

Guidance for Claude Code in **keel-crawler**, a reusable Keel **package** (not a
consumer app). Inherits the global rules in `~/.claude/CLAUDE.md` and the Keel
methodology in `~/www/keel-kit/methodology/`.

## What this package is

A **business-blind** web-crawling toolkit, extracted from the
Revenika/Propopedia/Binarystyle crawlers. It answers *how to fetch bytes past
defenses and turn them into clean, LLM-ready text*, and knows nothing about
brokers, forex, prop firms, or any consumer's data model. All domain knowledge
(extraction schemas, keyword vocabularies, page-priority heuristics, feed lists)
is injected by the host through config or callable hooks — **never** hardcoded here.

## The boundary — what belongs here vs the consumer

**Here (generic):** the fetch transport + cache, the anti-bot/proxy escalation
engine, error classifiers, browser config builders, Markdown cleaning, snapshot
storage plumbing, the generic `CrawlJob` status machine, and the RSS *transport*
(poll + dedup + stage + deterministic pre-filter).

**Consumer (business logic):** every LLM extraction prompt + schema, keyword
vocabularies (e.g. forex `cpa/pips/mt4`), page-priority scoring, feed URL lists,
and the editorial "is this newsworthy?" judgement. That last one — LLM triage +
article generation from a staged item — lives in **keel-content** (generalize its
`twitter/` monitor→triage→route pipeline), not here. keel-crawler stops at "clean,
deduped, staged raw item".

## Layer map

| Layer | Module | Status |
|---|---|---|
| 0 Fetch | `fetch/client.py` (`HttpFetcher`, concurrent `fetch_many`) + `fetch/hybrid.py` (`HybridFetcher`, cheap-first) | done |
| 1 Resilience | `antibot/classifiers.py`, `proxy/scores.py` + `proxy/mihomo.py`, `browser/{config,extract,engine}.py`, `captcha.py` | done |
| 2 Normalize | `clean/markdown.py` + `clean/snapshot.py` (`SnapshotStore`, `build_merged_markdown`) | done |
| 3 Orchestrate | `models.CrawlJob` + `orchestrate.py` (`CrawlSpec`, `run_batch`, transport adapters); `progress.py` | done |
| 4 RSS | `models.FeedSource`/`FeedItemCandidate` + `rss/{monitor,filters,triage}.py` + `crawler_rss_poll` (behind `[rss]`) | done |

Layer 1 design notes (already applied): the forex `deep_prune` flag became a neutral
`profile` ("content"/"raw"/"link_harvest") in `browser/config.py`; Mihomo score
side-effects are injected via the `on_result` hook / an injected `MihomoClient` on
`BrowserFetcher`; `crawler_factory` is injectable so the ladder is testable without
crawl4ai. Proxy scoring is disable-able (`proxy_scoring_enabled` / env) and
resettable (`ProxyScoreStore.reset` + `crawler_proxy_scores --reset`).

Layer 2 snapshot: `clean/snapshot.py` separates storage from prompt-wrapping (the
forex Gemini prompt Revenika baked into every file is now the caller's optional
`header`); merge ordering is an injectable `order_key`. Layer 1 `link_harvest`:
`browser/harvest.py` (DOM-harvest JS hooks + lxml static fallback with an injectable
`link_match`/`link_deny`); `BrowserFetcher(run_profile="link_harvest")` fills
`page.discovery_hrefs`.

Cross-cutting (v0.4.0): `pace.py` (`AsyncRateLimiter` evenly-spaced global rate +
`AsyncHostThrottle`) powers a **parallel** `BrowserFetcher.fetch_many` — bounded by
`concurrency`, paced by `rate_per_minute`, polite per host, order-preserving; all from
`KEEL_CRAWLER["fetch"]`. `discover.py` does **URL discovery**: `discover_sitemap_urls`
(robots → sitemap index → children, gz-aware) and `deep_crawl` (BFS link-follow with
`http_link_fetcher`/`browser_link_fetcher` adapters); `crawler_discover` command.

Cheap-first (v0.6.0): `HybridFetcher` (`fetch/hybrid.py`) tries a cheap HTTP GET first
and escalates to the browser engine only when a page comes back empty, challenged, or
below a visible-text floor (`needs_browser` predicate, injectable) — so a batch launches
Chromium only for the pages that actually need it. When the browser *is* used,
`BrowserFetcher` now reuses a small pool of Chromium sessions keyed by egress
(direct/proxy) across the whole batch instead of launching one per URL, and the
egress-IP probe (an extra ipify round-trip per page) is opt-in (`probe_egress_ip`, off
by default). `HttpFetcher.fetch_many` is threaded (bounded, order-preserving, shared
session/cache/throttle); `orchestrate.http_batch_fetch_fn`/`hybrid_batch_fetch_fn` drive
the parallel `run_batch` path.

User playbook: `docs/USING-KEEL-CRAWLER.md`. Remaining ideas: wire the RSS
`triage_hook` on the keel-content side; a `robots.txt` disallow/politeness gate for
fetches (discovery already reads robots for sitemaps); observability/metrics.

## Extension pattern (mirror keel-seo / keel-content)

- **Value config:** a `KEEL_CRAWLER` settings dict resolved via
  `config.crawler_setting(key)` with neutral defaults. Validated by `checks.py`.
- **Callable hooks:** future host reaches (captcha solver, snapshot header-wrap,
  page ordering) are dotted-path strings resolved with `import_string`, degrading
  to a safe no-op when unset.
- **Swappable model / db_table:** `CrawlHttpCache.Meta.db_table` comes from config
  so a host adopts an existing table; the `0001` migration reads the same setting
  and flips greenfield↔adopt via `SeparateDatabaseAndState`.
- Never rename a released app label, `db_table`, or index name — additive only.

## Optional dependencies

Keep the core import-light. Heavy backends are extras: `browser` (crawl4ai +
playwright), `rss` (feedparser), `markdown` (beautifulsoup4). Degrade gracefully
when an extra is absent — never hard-import a heavy dep at module top level in the
core path.

## Self-check before shipping

- `python3 -m py_compile` the `src/` tree.
- `manage.py check` passes in a host that installs the app (config check clean).
- `makemigrations --check` is clean in both greenfield and adopt modes.
- Any `src/` change bumps `pyproject` version (version-guard enforces in CI).
