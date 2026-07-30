"""BrowserFetcher — the crawl4ai engine + 3-layer anti-bot retry ladder.

Ported from Revenika's platform_crawler, with the forex/env coupling inverted into
constructor arguments and injectable strategy hooks:

1. **Transient retry** inside one Chromium session (longer page_timeout each try,
   harder backoff for 429).
2. **Egress fallback** — try direct vs proxy per host, remembering the last success
   in Django cache; on an anti-bot block, retry via the other egress.
3. **Proxy rotation** — when the proxy egress is still blocked, advance the Mihomo
   AUTO group and re-open Chromium through the (unchanged) local proxy URL.

If the ladder is exhausted and the page still looks like a challenge, the optional
host captcha solver (:mod:`keel_crawler.captcha`) is given a last shot.

crawl4ai is imported lazily via ``crawler_factory`` so this module imports without
the ``[browser]`` extra, and tests can inject a fake crawler.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from keel_crawler import captcha
from keel_crawler.antibot.classifiers import (
    is_antibot_block_error,
    is_proxy_connection_error,
    is_rate_limit_error,
    looks_like_cloudflare_interstitial,
    retry_sleep_seconds,
    should_retry_error,
)
from keel_crawler.browser.config import build_browser_config, build_run_config
from keel_crawler.browser.extract import CrawledPage, crawl_result_to_page, with_egress
from keel_crawler.browser.harvest import (
    dedupe_urls_preserve_order,
    extract_links_from_html,
    install_discovery_hooks,
    restore_discovery_hooks,
)

logger = logging.getLogger(__name__)

_EXTRA_RETRY_ATTEMPTS = 2
_RETRY_TIMEOUT_BUMP_SEC = 35
_RETRY_MAX_TIMEOUT_SEC = 180
_EXTRA_429_ATTEMPTS = 3


def _hostname(url: str) -> str:
    try:
        host = (urlparse((url or "").strip()).netloc or "").lower()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


class EgressPreferenceStore:
    """Remember the last egress ("direct"/"proxy") that worked per host, in Django cache."""

    def __init__(self, *, ttl_seconds: int = 86_400) -> None:
        self._ttl = max(60, int(ttl_seconds))

    @staticmethod
    def _key(host: str) -> str:
        return f"keel_crawler:egress:v1:{host or 'unknown'}"

    def get_preferred(self, host: str) -> Optional[str]:
        if not host:
            return None
        try:
            from django.core.cache import cache

            v = cache.get(self._key(host))
            return str(v) if v in ("direct", "proxy") else None
        except Exception:
            return None

    def remember(self, host: str, mode: str) -> None:
        if not host or mode not in ("direct", "proxy"):
            return
        try:
            from django.core.cache import cache

            cache.set(self._key(host), mode, timeout=self._ttl)
        except Exception:
            pass

    def pick_first(self, host: str, *, has_proxy: bool, prefer_proxy: bool) -> str:
        pref = self.get_preferred(host)
        if pref:
            return pref
        if has_proxy and prefer_proxy:
            return "proxy"
        return "direct"


async def _probe_egress_ip(page: Any, context: Any) -> str:
    """Public egress IP via the SAME Chromium network path (in-page fetch, then a 2nd tab)."""
    try:
        raw = await page.evaluate(
            """async () => {
                try {
                    const r = await fetch('https://api.ipify.org/?format=json',
                        {method:'GET', credentials:'omit', cache:'no-store'});
                    if (!r.ok) return '';
                    const j = await r.json();
                    return (j && typeof j.ip === 'string') ? j.ip.trim() : '';
                } catch (e) { return ''; }
            }"""
        )
        if isinstance(raw, str) and raw.strip() and len(raw.strip()) <= 64:
            return raw.strip()
    except Exception:
        pass
    p2 = None
    try:
        p2 = await context.new_page()
        await p2.goto("https://api.ipify.org/", wait_until="domcontentloaded", timeout=10_000)
        txt = await p2.inner_text("body")
        line = (txt or "").strip().split("\n", 1)[0].strip()
        if line and len(line) <= 64:
            return line
    except Exception:
        pass
    finally:
        if p2 is not None:
            try:
                await p2.close()
            except Exception:
                pass
    return ""


def _install_egress_hook(strategy: Any, holder: list[str]) -> Any:
    """Chain any existing ``before_return_html`` hook; store last observed IP in holder[0]."""
    if strategy is None or not hasattr(strategy, "hooks"):
        return None
    prev = strategy.hooks.get("before_return_html")

    async def _hook(*, page=None, html=None, context=None, config=None, **kwargs: Any) -> None:
        if prev is not None:
            res = prev(page=page, html=html, context=context, config=config, **kwargs)
            if asyncio.iscoroutine(res):
                await res
        if page is not None and context is not None:
            try:
                ip = await _probe_egress_ip(page, context)
                if ip:
                    holder[0] = ip
            except Exception:
                pass

    strategy.hooks["before_return_html"] = _hook
    return prev


def _restore_egress_hook(strategy: Any, prev: Any) -> None:
    if strategy is not None and hasattr(strategy, "hooks"):
        strategy.hooks["before_return_html"] = prev


class BrowserFetcher:
    """crawl4ai-backed fetcher with the anti-bot escalation ladder.

    Build with :meth:`from_config` to resolve proxy/Mihomo/browser settings from
    ``KEEL_CRAWLER`` (with ``.env`` fallback), or construct directly for full control.
    """

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        mihomo: Any = None,
        auto_egress: bool = True,
        run_profile: str = "content",
        page_timeout_sec: int = 45,
        headless: bool = True,
        channel: str = "",
        rotate_user_agent: bool = True,
        max_proxy_rotations: int = 5,
        same_host_stagger_sec: float = 4.0,
        egress_store: EgressPreferenceStore | None = None,
        on_result: Optional[Callable[[bool, bool], None]] = None,
        crawler_factory: Optional[Callable[[Any], Any]] = None,
        link_match: Optional[Callable[[str, str, str], bool]] = None,
        link_deny: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._proxy_url = (proxy_url or "").strip() or None
        self._mihomo = mihomo
        self._auto_egress = bool(auto_egress) and bool(self._proxy_url)
        self._profile = run_profile
        self._page_timeout = int(page_timeout_sec)
        self._headless = bool(headless)
        self._channel = channel or ""
        self._rotate_ua = bool(rotate_user_agent)
        self._max_rotations = max(0, int(max_proxy_rotations))
        self._stagger = max(0.0, float(same_host_stagger_sec))
        self._egress = egress_store or EgressPreferenceStore()
        self._on_result = on_result
        self._crawler_factory = crawler_factory or self._default_crawler_factory
        # link_harvest: relevance filter for the static-HTML pass (default: keep all).
        self._link_match = link_match
        self._link_deny = link_deny

    @classmethod
    def from_config(cls, **overrides: Any) -> "BrowserFetcher":
        """Resolve proxy/Mihomo/browser settings from ``KEEL_CRAWLER`` (+ env)."""
        import os

        from keel_crawler.config import crawler_setting
        from keel_crawler.proxy.mihomo import mihomo_from_config

        proxy_url = (crawler_setting("proxy_url") or os.environ.get("LOCAL_PROXY_URL") or "").strip()
        mihomo = mihomo_from_config()
        kwargs: dict[str, Any] = {
            "proxy_url": proxy_url or None,
            "mihomo": mihomo if mihomo.is_configured() else None,
            "headless": bool(crawler_setting("browser_headless")),
            "channel": crawler_setting("browser_channel") or "",
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @staticmethod
    def _default_crawler_factory(browser_cfg: Any) -> Any:
        from crawl4ai import AsyncWebCrawler

        return AsyncWebCrawler(config=browser_cfg)

    def _browser_config(self, *, force_proxy: bool) -> Any:
        return build_browser_config(
            proxy_url=self._proxy_url if force_proxy else None,
            rotate_user_agent=self._rotate_ua,
            headless=self._headless,
            channel=self._channel,
        )

    def _run_config(self, timeout_sec: int) -> Any:
        return build_run_config(timeout_sec, profile=self._profile)

    def _notify_result(self, *, via_proxy: bool, success: bool) -> None:
        if self._on_result is not None:
            try:
                self._on_result(via_proxy, success)
            except Exception:
                logger.debug("on_result hook raised", exc_info=True)
            return
        if via_proxy and self._mihomo is not None:
            try:
                (self._mihomo.record_success if success else self._mihomo.record_failure)()
            except Exception:
                logger.debug("mihomo score record raised", exc_info=True)

    def _rotation_warranted(self, page: CrawledPage) -> bool:
        if page.egress_proxy is not True:
            return False
        err = (page.error or "").strip()
        if is_proxy_connection_error(err):
            return False
        if not err and (page.text or "").strip():
            return False
        return True

    def _collect_discovery(self, accum: list[str], result: Any, base_url: str) -> list[str]:
        """Merge JS-harvested hrefs with a static-HTML pass over the returned document."""
        static = extract_links_from_html(
            str(getattr(result, "html", "") or ""),
            base_url,
            match=self._link_match,
            deny=self._link_deny,
        )
        return dedupe_urls_preserve_order(list(accum) + static)

    async def _crawl_with_retries(
        self, crawler: Any, url: str, base_timeout: int
    ) -> tuple[CrawledPage, str]:
        strategy = getattr(crawler, "crawler_strategy", None)
        holder: list[str] = [""]
        prev_hook = _install_egress_hook(strategy, holder)
        harvesting = self._profile == "link_harvest"
        disc_accum: list[str] = []
        disc_prev = install_discovery_hooks(strategy, disc_accum, set()) if harvesting else {}
        attempt_timeout = base_timeout
        last_page = CrawledPage(url=url, text="", error="")
        last_ip = ""
        base_cap = _EXTRA_RETRY_ATTEMPTS + 1
        hard_cap = base_cap + _EXTRA_429_ATTEMPTS
        try:
            for attempt in range(hard_cap):
                holder[0] = ""
                result = None
                try:
                    result = await crawler.arun(url=url, config=self._run_config(attempt_timeout))
                    last_ip = (holder[0] or "").strip()[:64]
                    last_page = crawl_result_to_page(url, result)
                    err = last_page.error or ""
                    if not err:
                        if harvesting:
                            last_page.discovery_hrefs = self._collect_discovery(
                                disc_accum, result, last_page.url
                            )
                        return last_page, last_ip
                except Exception as exc:
                    err = str(exc)
                    last_ip = (holder[0] or "").strip()[:64]
                    last_page = CrawledPage(url=url, text="", error=err)
                if attempt >= hard_cap - 1 or not should_retry_error(err):
                    if harvesting:
                        last_page.discovery_hrefs = self._collect_discovery(
                            disc_accum, result, last_page.url
                        )
                    return last_page, last_ip
                if attempt + 1 >= base_cap and not is_rate_limit_error(err):
                    if harvesting:
                        last_page.discovery_hrefs = self._collect_discovery(
                            disc_accum, result, last_page.url
                        )
                    return last_page, last_ip
                attempt_timeout = min(
                    base_timeout + _RETRY_TIMEOUT_BUMP_SEC * (attempt + 1), _RETRY_MAX_TIMEOUT_SEC
                )
                await asyncio.sleep(retry_sleep_seconds(err, attempt))
            return last_page, last_ip
        finally:
            _restore_egress_hook(strategy, prev_hook)
            if harvesting:
                restore_discovery_hooks(strategy, disc_prev)

    async def _crawl_new_session(self, url: str, timeout: int, *, force_proxy: bool) -> CrawledPage:
        browser_cfg = self._browser_config(force_proxy=force_proxy)
        via_proxy = bool(force_proxy and self._proxy_url)
        try:
            async with self._crawler_factory(browser_cfg) as crawler:
                page, obs_ip = await self._crawl_with_retries(crawler, url, timeout)
        except Exception as exc:
            page, obs_ip = CrawledPage(url=url, text="", error=str(exc)), ""
        page = with_egress(page, via_proxy=via_proxy, egress_ip=obs_ip)
        self._notify_result(via_proxy=via_proxy, success=page.ok())
        return page

    async def _rotate_proxy_retry(
        self, url: str, timeout: int, *, host: str, page: CrawledPage
    ) -> CrawledPage:
        if self._mihomo is None or not self._mihomo.is_configured():
            return page
        if not self._rotation_warranted(page):
            return page
        for _ in range(self._max_rotations):
            ok, detail = await asyncio.to_thread(self._mihomo.cycle_next)
            if not ok:
                logger.info("keel-crawler: Mihomo rotation stopped: %s", detail)
                break
            logger.info("keel-crawler: %s — retry via proxy", detail)
            page = await self._crawl_new_session(url, timeout, force_proxy=True)
            if not self._rotation_warranted(page):
                if page.ok() and host:
                    self._egress.remember(host, "proxy")
                return page
        return page

    async def _crawl_auto_egress(self, url: str, timeout: int) -> CrawledPage:
        host = _hostname(url)
        has_proxy = bool(self._proxy_url)
        if not has_proxy:
            return await self._crawl_new_session(url, timeout, force_proxy=False)

        first = self._egress.pick_first(host, has_proxy=True, prefer_proxy=False)

        async def once(force_proxy: bool) -> CrawledPage:
            return await self._crawl_new_session(url, timeout, force_proxy=force_proxy)

        page = await once(first == "proxy")
        if page.ok():
            self._egress.remember(host, first)
            return page

        if first == "proxy" and is_proxy_connection_error(page.error):
            logger.warning("keel-crawler: proxy connection failed; trying direct for %s", url[:120])
            page2 = await once(False)
            if page2.ok():
                self._egress.remember(host, "direct")
                return page2
            return await self._rotate_proxy_retry(url, timeout, host=host, page=page2)

        other = "proxy" if first == "direct" else "direct"
        if is_antibot_block_error(page.error):
            logger.info("keel-crawler: anti-bot via %s, retrying %s (%s)", first, other, url[:120])
            page2 = await once(other == "proxy")
            if page2.ok():
                self._egress.remember(host, other)
                return page2
            if other == "proxy" and is_proxy_connection_error(page2.error):
                page3 = await once(False)
                if page3.ok():
                    self._egress.remember(host, "direct")
                    return page3
                return await self._rotate_proxy_retry(url, timeout, host=host, page=page3)
            return await self._rotate_proxy_retry(url, timeout, host=host, page=page2)

        return await self._rotate_proxy_retry(url, timeout, host=host, page=page)

    async def _maybe_solve_captcha(self, url: str, page: CrawledPage) -> CrawledPage:
        if page.ok():
            return page
        looks_challenge = is_antibot_block_error(page.error) or looks_like_cloudflare_interstitial(
            page.text
        )
        if not looks_challenge:
            return page
        solved = await asyncio.to_thread(captcha.try_solve, url, page)
        return solved if solved is not None else page

    async def afetch_one(self, url: str) -> CrawledPage:
        """Async: crawl one URL through the full ladder + captcha last resort."""
        if self._auto_egress:
            page = await self._crawl_auto_egress(url, self._page_timeout)
        else:
            page = await self._crawl_new_session(
                url, self._page_timeout, force_proxy=bool(self._proxy_url)
            )
            if page.egress_proxy and is_proxy_connection_error(page.error):
                page = await self._crawl_new_session(url, self._page_timeout, force_proxy=False)
        return await self._maybe_solve_captcha(url, page)

    async def afetch_many(self, urls: list[str]) -> list[CrawledPage]:
        out: list[CrawledPage] = []
        prev_host = ""
        for u in urls:
            host = _hostname(u)
            if self._stagger > 0 and prev_host and prev_host == host:
                await asyncio.sleep(self._stagger)
            try:
                out.append(await self.afetch_one(u))
            except Exception as exc:
                out.append(CrawledPage(url=u, text="", error=str(exc)))
            prev_host = host
        return out

    def fetch_one(self, url: str) -> CrawledPage:
        """Sync wrapper around :meth:`afetch_one` (opens its own event loop)."""
        return asyncio.run(self.afetch_one(url))

    def fetch_many(self, urls: list[str]) -> list[CrawledPage]:
        """Sync wrapper around :meth:`afetch_many`."""
        if not urls:
            return []
        return asyncio.run(self.afetch_many(urls))
