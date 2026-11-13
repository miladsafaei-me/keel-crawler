# CLAUDE.md - keel-crawler

Guidance for Claude Code in **keel-crawler**, a reusable Keel **package** (not a
consumer app). Inherits the global rules in `~/.claude/CLAUDE.md` and the Keel
methodology in `~/www/keel-kit/methodology/`.

## Task tracking

Remaining and follow-up work for this project is tracked in [TODO.md](TODO.md), not in chat memory. Every pending task - priority, prerequisites/dependencies, enough context to resume cold - goes there before starting new work; remove a task from TODO.md the moment it's done.

## What this package is

A **business-blind** web-crawling toolkit, extracted from the
Revenika/Propopedia/Binarystyle crawlers. It answers *how to fetch bytes past
defenses and turn them into clean, LLM-ready text*, and knows nothing about
brokers, forex, prop firms, or any consumer's data model. All domain knowledge
(extraction schemas, keyword vocabularies, page-priority heuristics, feed lists)
is injected by the host through config or callable hooks - **never** hardcoded here.

## The boundary - what belongs here vs the consumer

**Proxy rotation is owned here, for every Keel project.** Two shapes, one home:
`proxy/pool.py` for many disposable public addresses (self-harvesting from 16
published lists, target-verified, self-pruning store, per-address budgets), and
`proxy/mihomo.py` + `proxy/scores.py` for a few configured outbounds. A consumer
that needs rotation imports it - keel-seo does, via its `proxies` extra - and
must not grow its own. `proxy/jsonstore.py` is the shared on-disk primitive both
use; do not add a third copy of it.

**And rotation is not optional for list-scale fetching.** A crawl never leaves
from the machine running it, laptop or production host, because the punishment
is IP-wide and outlasts the run: measured 2026-09-04, a keyword harvest asking
Google directly at a throttled 6 q/s was refused after 3,909 requests, and the
refusal covered every request from that address - a host carrying six live
sites - for over an hour. Throttling buys nothing here; the per-address budgets
in `proxy/pool.py` exist to keep *rotated* addresses alive, not to make a single
address safe. keel-seo's harvester enforces this in code (v0.28.0: direct egress
refused, no override), and anything else built on this package is expected to
default the same way. The standing exception is a single long-lived browser
session that has already cleared a challenge - rotating under it re-triggers the
challenge - which is a session at human pace, not a sweep. Cross-project
statement of the rule: `keel-kit/methodology/seo-core.md`.

**Here (generic):** the fetch transport + cache, the anti-bot/proxy escalation
engine, error classifiers, browser config builders, Markdown cleaning, snapshot
storage plumbing, the generic `CrawlJob` status machine, and the RSS *transport*
(poll + dedup + stage + deterministic pre-filter).

**Consumer (business logic):** every LLM extraction prompt + schema, keyword
vocabularies (e.g. forex `cpa/pips/mt4`), page-priority scoring, feed URL lists,
and the editorial "is this newsworthy?" judgement. That last one - LLM triage +
article generation from a staged item - lives in **keel-content** (generalize its
`twitter/` monitor→triage→route pipeline), not here. keel-crawler stops at "clean,
deduped, staged raw item".

**Known issue: the YouTube Data API client exists twice.** `youtube/api.py` here and
`keel_seo/youtube/api.py` in keel-seo are two separate quota-aware clients for the same API
with the same quota logic. The ownership map in keel-kit names neither as the owner, and
the consolidation is not done (decision of 2026-11-13: record the duplication, no code change yet).
Before adding a YouTube fetch or quota feature, check both copies and say in the change
which one you extended.

## Layer map

| Layer | Module | Status |
|---|---|---|
| 0 Fetch | `fetch/client.py` (`HttpFetcher`, concurrent `fetch_many`) + `fetch/hybrid.py` (`HybridFetcher`, cheap-first) | done |
| 1 Resilience | `antibot/classifiers.py`, `proxy/scores.py` + `proxy/mihomo.py`, `proxy/pool.py` + `proxy/sources.py` + `proxy/jsonstore.py`, `browser/{config,extract,engine}.py`, `captcha.py` | done |
| 2 Normalize | `clean/markdown.py` + `clean/snapshot.py` (`SnapshotStore`, `build_merged_markdown`) | done |
| 3 Orchestrate | `models.CrawlJob` + `orchestrate.py` (`CrawlSpec`, `run_batch`, transport adapters); `progress.py` | done |
| 4 RSS | `models.FeedSource`/`FeedItemCandidate` + `rss/{monitor,filters,triage}.py` + `crawler_rss_poll` (behind `[rss]`) | done |
| 5 YouTube research | `youtube/{api,quota,suggest,velocity,patterns,analytics,reporting,oauth,consent}.py`: quota-accounted Data API client, autocomplete snapshots, per-channel outlier maths, OAuth-gated Analytics and Reporting; pure `requests`, no Django (see README "Layer 5") | done |
| 6 Registers | `registries/{base,companies_house,ares}.py`: `lookup(jurisdiction, number)` against an official company register, `jurisdiction_for` a free-text country, `SEARCH_PAGES` for registers with no page by number (v0.21.1, 2026-10-14; the v0.21.0 tag was cut on the commit before the layer and is deleted) | done |
| 7 Shots | `shots.py`: page screenshots kept by version (one WebP plus a map of text blocks with rectangles) | done |

Modules outside the layer table: `contacts.py` (emails, phones, websites and social handles
pulled from free text, no model), `archive.py` (earliest Wayback capture of a site that
already says something), `normalize.py` (URL normalization shared by the fetch cache and
the per-host throttle), `throttle.py` (per-host tracking bounds), `pace.py` (global rate
limiter and host throttle), `discover.py` (sitemap and link-follow URL discovery) and
`proxy/__main__.py` (`python -m keel_crawler.proxy refresh|check|prune|stats|sources`,
maintenance of the shared proxy store).

Design notes for layers 1 and 2 and the v0.4.0 to v0.8.0 perf and logic passes (parallel
fetching, level-parallel discovery, cheap-first escalation, conditional GETs, body caps,
`canonical_url_key` rules) live in `docs/history.md`. Before changing fetch, cache,
discovery or escalation behaviour, read it first. Four contracts from those notes hold
today and stay here:

- `canonical_url_key` strips tracking params (`utm_*`, `gclid`, `fbclid`, ...) and sorts the
  rest, so tracking-tagged copies collapse, while pagination and faceted URLs (`page=`,
  `p=`, ids, sort/filter) stay distinct and still enter the frontier.
- Custom `needs_browser` predicates keep their `(html, final_url)` contract.
- `probe_egress_ip` is opt-in, off by default.
- `max_download_bytes` defaults to 16 MiB (64 MiB on `throttled_get`).

User playbook: `docs/USING-KEEL-CRAWLER.md`. Remaining ideas: wire the RSS
`triage_hook` on the keel-content side; a `robots.txt` disallow/politeness gate for
fetches (intentionally skipped for now, per owner - discovery already reads robots for
sitemaps); observability/metrics.

## Extension pattern (mirror keel-seo / keel-content)

- **Value config:** a `KEEL_CRAWLER` settings dict resolved via
  `config.crawler_setting(key)` with neutral defaults. Validated by `checks.py`.
- **Callable hooks:** future host reaches (captcha solver, snapshot header-wrap,
  page ordering) are dotted-path strings resolved with `import_string`, degrading
  to a safe no-op when unset.
- **Swappable model / db_table:** `CrawlHttpCache.Meta.db_table` comes from config
  so a host adopts an existing table; the `0001` migration reads the same setting
  and flips greenfield↔adopt via `SeparateDatabaseAndState`.
- Never rename a released app label, `db_table`, or index name - additive only.

## Optional dependencies

Keep the core import-light. Heavy backends are extras: `browser` (crawl4ai +
playwright), `rss` (feedparser), `markdown` (beautifulsoup4). Degrade gracefully
when an extra is absent - never hard-import a heavy dep at module top level in the
core path.

## Self-check before shipping

- `python3 -m py_compile` the `src/` tree.
- `manage.py check` passes in a host that installs the app (config check clean).
- `makemigrations --check` is clean in both greenfield and adopt modes.
- Any `src/` change bumps `pyproject` version (version-guard enforces in CI).
