# TODO

This file is the single source of truth for pending, follow-up, and deferred work on this project. See CLAUDE.md for the tracking rule.

Guidelines:
- Add a task here as soon as it's identified — with priority, prerequisites/dependencies, and enough context to pick it up cold.
- Group by priority: P0 (urgent / blocking / production risk), P1 (next up), P2 (backlog / nice-to-have).
- Note real dependencies explicitly ("Blocked by: ...", "Requires: ...").
- Delete a task from this file the moment it's done. This file only ever holds what's left.

## P1 — Next up
- [ ] Consolidate the Trustpilot crawlers duplicated across revenika, prop-firm-review, and binaryoptiontrading into one reusable keel-crawler module. Scope:
  1. Refresh/update Trustpilot scores per brand — reuse the existing Playwright/patchright `businessUnit`-JSON approach that already beats Trustpilot's AWS WAF (see the revenika-trustpilot-crawler and propfirmreview-trustpilot-refresh memory notes for the current working method in each project).
  2. Scrape a capped number of reviews per brand (configurable cap).
  3. Analyze the scraped reviews with a Claude Sonnet subagent (per the standing "Claude subagents ARE the LLM" rule — no external LLM API or API key).
  4. On each brand's page in the consuming project, render a categorized list of positive and negative reviews, plus a written summary of the positive/negative themes.
  Needs a design pass first: read the three existing per-project crawler implementations (revenika, prop-firm-review, binaryoptiontrading) to find the common shape before building the shared module — don't start coding blind.
  Context: revenika's `core/services/trustpilot_browser.py` + `platform_trustpilot_fetch.py` and prop-firm-review's `core/crawl/products/trustpilot_browser.py` + `core/crawl/products/trustpilot.py` both already solve the WAF via a persistent headless browser session reading Next.js `__NEXT_DATA__ → props.pageProps.businessUnit` (JSON-LD `aggregateRating` as fallback only). binaryoptiontrading requirements.txt has no keel-crawler dependency at all yet, so its Trustpilot logic (if any) is fully separate — check its repo directly during the design pass. revenika and prop-firm-review both already consume keel-crawler v0.8.0, so the browser/anti-bot ladder (`BrowserFetcher`, Layer 1) is available to build on rather than re-solving WAF evasion from scratch.

## P2 — Backlog
- [ ] Wire the RSS `triage_hook` on the keel-content side — keel-crawler's RSS layer (`rss/{monitor,filters,triage}.py`, Layer 4) ships poll → dedup → stage → deterministic pre-filter, but the LLM "is this newsworthy?" triage step is a host hook that keel-content is meant to implement (generalizing its `twitter/` monitor→triage→route pipeline) and hasn't yet. Work happens mostly in `~/www/keel-content`, tracked here because it's the last open item on keel-crawler's own Layer 4 roadmap.
- [ ] Add observability/metrics to the crawl pipeline (fetch/cache hit-rates, browser-escalation counts, per-host throttle stats, etc. — no concrete design yet, listed as an open idea in CLAUDE.md's roadmap notes).
