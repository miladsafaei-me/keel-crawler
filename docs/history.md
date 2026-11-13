# keel-crawler: design notes by version

Moved verbatim out of `CLAUDE.md` (layer map section). These paragraphs describe how the
layers were shaped in v0.4.0 to v0.8.0 and why. Read before changing fetch, cache,
discovery, cheap-first escalation, snapshot or link-harvest behaviour.

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
`AsyncHostThrottle`) powers a **parallel** `BrowserFetcher.fetch_many` - bounded by
`concurrency`, paced by `rate_per_minute`, polite per host, order-preserving; all from
`KEEL_CRAWLER["fetch"]`. `discover.py` does **URL discovery**: `discover_sitemap_urls`
(robots → sitemap index → children, gz-aware) and `deep_crawl` (BFS link-follow with
`http_link_fetcher`/`browser_link_fetcher` adapters); `crawler_discover` command.

Cheap-first (v0.6.0): `HybridFetcher` (`fetch/hybrid.py`) tries a cheap HTTP GET first
and escalates to the browser engine only when a page comes back empty, challenged, or
below a visible-text floor (`needs_browser` predicate, injectable) - so a batch launches
Chromium only for the pages that actually need it. When the browser *is* used,
`BrowserFetcher` now reuses a small pool of Chromium sessions keyed by egress
(direct/proxy) across the whole batch instead of launching one per URL, and the
egress-IP probe (an extra ipify round-trip per page) is opt-in (`probe_egress_ip`, off
by default). `HttpFetcher.fetch_many` is threaded (bounded, order-preserving, shared
session/cache/throttle); `orchestrate.http_batch_fetch_fn`/`hybrid_batch_fetch_fn` drive
the parallel `run_batch` path.

Perf/logic pass (v0.7.0): URL **discovery is now level-parallel** - `deep_crawl`
accepts a batch `fetch_links_many` (adapters `http_links_many_fetcher` /
`browser_links_many_fetcher`) and fetches a whole BFS level through one paced
`fetch_many`; `discover_sitemap_urls` fetches same-level sitemaps concurrently
(`sitemap_workers`). The HTTP cache does **conditional GETs** (`If-None-Match` /
`If-Modified-Since` from stored validators → a `304` reuses the body and refreshes the
TTL, no re-download); the three fetch methods now share one `_get_cached` core.
`_write_cache` upserts in a single statement (`bulk_create(update_conflicts=True)`);
parallel `run_batch` persists the whole batch with one `bulk_update` instead of N saves.
`looks_like_cloudflare_interstitial` scans only the leading slice (no full-page lower()).

Perf/logic pass (v0.8.0): the **304 conditional GET** now refreshes only the cache TTL
(a narrow `UPDATE expires_at`) instead of re-writing the whole body + recomputing its
SHA on every revalidation (and fixes a stale-expiry L1 re-seed on that path). HTTP
response bodies are **streamed and capped** (`max_download_bytes`, default 16 MiB; a
generous 64 MiB cap on `throttled_get` for large sitemaps) so a huge/hostile page can't
be fully downloaded+decoded; a single **transient retry** (conn error / 429 / 5xx) on
the HTTP path stops a blip from needlessly escalating to the browser. The L1 cache is
**bounded by total body bytes** (`l1_max_bytes`), not just entry count. Cheap-first
escalation parses the HTML **once** - `extract.page_and_visible_len_from_html` yields
both the extracted page and the JS-shell floor from one lxml parse (was a regex scan +
a parse); custom `needs_browser` predicates keep their `(html, final_url)` contract.
`canonical_url_key` strips tracking params (`utm_*`, `gclid`, `fbclid`, …) and sorts the
rest, so tracking-tagged copies collapse **while pagination/faceted URLs (`page=`, `p=`,
ids, sort/filter) stay distinct and still enter the frontier**. `parse_sitemap` streams
via `iterparse` (per-element clear) for bounded memory on 50 MB sitemaps.
`BrowserFetcher.afetch_many` drains a queue with a fixed pool of `concurrency` workers
(order-preserving) instead of one coroutine-per-URL. `poll_feeds` writes feed status in
one `bulk_update`.
