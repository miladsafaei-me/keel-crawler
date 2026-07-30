"""DOM link harvesting for the ``link_harvest`` profile.

Two complementary paths, ported from Revenika's menu-discovery crawler and made
business-blind:

* **Live JS harvest** — while Chromium holds the post-JS DOM, ``DOM_HARVEST_JS``
  collects every absolute http(s) href (anchors, ``area``, ``data-href``/``data-url``).
  ``NAV_EXPAND_JS_*`` first clicks common mega-menu triggers so hidden nav links
  render. The engine installs the hook factories here during a ``link_harvest`` crawl.
* **Static HTML harvest** — ``extract_links_from_html`` re-parses the returned HTML
  with lxml (a JS-free fallback / merge). The relevance filter is **injected**: a host
  passes ``match(anchor_text, href, path) -> bool`` (Revenika's forex partner/IB
  predicate stays in the consumer); the default accepts every link.

lxml is imported lazily so this module loads without the ``[browser]`` extra.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

_HREF_RE = re.compile(r"""(?:href|data-href|data-url)\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)

# Query parameters that never change *which page* is served — only analytics/ad
# attribution. Dropping them from the identity key collapses tracking-tagged copies of
# one page, WITHOUT touching parameters that select real content (``page``, ``p``,
# ``id``, ``q``, ``category``, sort/filter facets, …), so pagination and faceted URLs
# stay distinct and still get crawled.
_TRACKING_PARAMS = frozenset(
    {
        "gclid", "gclsrc", "dclid", "wbraid", "gbraid", "fbclid", "msclkid",
        "mc_eid", "mc_cid", "_hsenc", "_hsmi", "hsctatracking", "igshid", "yclid",
        "ttclid", "twclid", "vero_id", "vero_conv", "oly_enc_id", "oly_anon_id",
        "wickedid", "s_kwcid", "_ga", "_gl", "spm", "scm", "trk", "mkt_tok",
    }
)
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking_param(name: str) -> bool:
    n = (name or "").lower()
    return n in _TRACKING_PARAMS or n.startswith(_TRACKING_PREFIXES)


def _canonical_query(query: str) -> str:
    """Drop tracking params and sort the rest, so ``?a=1&utm_x=y`` and ``?utm_z=w&a=1``
    share one key while ``?page=2`` stays distinct from ``?page=3``."""
    if not query:
        return ""
    pairs = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    if not pairs:
        return ""
    pairs.sort()
    return urlencode(pairs)

# Runs in Playwright after crawl4ai builds the HTML string; page still holds the post-JS DOM.
DOM_HARVEST_JS = r"""
() => {
  const seen = new Set();
  const out = [];
  const base = document.baseURI || window.location.href;
  const add = (href) => {
    if (!href) return;
    const h = String(href).trim();
    if (!h || h.startsWith("#")) return;
    const low = h.toLowerCase();
    if (low.startsWith("javascript:") || low.startsWith("mailto:") || low.startsWith("tel:")) return;
    try {
      const abs = new URL(h, base).href;
      if (seen.has(abs)) return;
      seen.add(abs);
      out.push(abs);
    } catch (e) {}
  };
  document.querySelectorAll("a[href]").forEach((a) => add(a.getAttribute("href")));
  document.querySelectorAll("area[href]").forEach((a) => add(a.getAttribute("href")));
  document.querySelectorAll("[data-href]").forEach((el) => add(el.getAttribute("data-href")));
  document.querySelectorAll("[data-url]").forEach((el) => add(el.getAttribute("data-url")));
  return out;
}
"""

# Expand MUI/React mega-menu rows + <details> so their links render before harvest.
NAV_EXPAND_JS_BEFORE = """
const _click = (el) => { try { el.dispatchEvent(new MouseEvent("click",
  {bubbles:true, cancelable:true, view:window})); } catch (e) {} };
document.querySelectorAll('[data-testid="nav:button"]').forEach(_click);
document.querySelectorAll('button.nav-item[aria-expanded="false"]').forEach(_click);
document.querySelectorAll('nav button.MuiButtonBase-root.nav-item').forEach((el) => {
  if (el.getAttribute("aria-expanded") === "false") { _click(el); }
});
document.querySelectorAll("details:not([open])").forEach((d) => { try { d.open = true; } catch (e) {} });
"""

NAV_EXPAND_JS_AFTER = """
const _click2 = (el) => { try { el.dispatchEvent(new MouseEvent("click",
  {bubbles:true, cancelable:true, view:window})); } catch (e) {} };
document.querySelectorAll('[data-testid="nav:button"]').forEach(_click2);
document.querySelectorAll('button.nav-item[aria-expanded="false"]').forEach(_click2);
"""


def canonical_url_key(url: str) -> str:
    """Scheme/www/trailing-slash-insensitive identity key for one URL.

    Two URLs that differ only by ``www.``, a trailing slash, ``http`` vs ``https``, the
    order of query parameters, or a tracking/analytics parameter (``utm_*``, ``gclid``,
    ``fbclid``, …) collapse to the same key. Content-selecting parameters — pagination
    (``page``/``p``), ids, search terms, sort/filter facets — are preserved, so those
    pages remain distinct and still enter the crawl frontier.

    Shared by :func:`dedupe_urls_preserve_order` and the deep-crawl visited-set so both
    agree on what "the same page" means. Note this is a *discovery* identity and differs
    intentionally from :func:`keel_crawler.normalize.normalize_request_url` (the HTTP
    cache key, which keeps ``www.`` and the raw query) — the two answer different
    questions and must not be conflated.
    """
    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        query = _canonical_query(p.query)
        return f"https://{host}{path}" + (f"?{query}" if query else "")
    except Exception:
        return u.lower()


def dedupe_urls_preserve_order(urls: list[str]) -> list[str]:
    """Collapse equivalent URLs (www, trailing slash, http/https) keeping first spelling."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        u = (raw or "").strip()
        if not u:
            continue
        key = canonical_url_key(u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def extract_links_from_html(
    html: str,
    base_url: str,
    *,
    match: Optional[Callable[[str, str, str], bool]] = None,
    deny: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Collect absolute http(s) links from ``html`` (lxml). Cross-domain links allowed.

    ``match(anchor_text, href, path)`` keeps a link when True (default: keep all).
    ``deny(abs_url)`` drops a link when True (default: deny none).
    """
    if not (html or "").strip():
        return []
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)
    except Exception:
        return []

    collected: list[str] = []
    for el in tree.xpath("//a[@href or @data-href or @data-url] | //area[@href]"):
        raws = []
        for attr in ("href", "data-href", "data-url"):
            v = (el.get(attr) or "").strip()
            if v and not v.startswith("#") and not v.lower().startswith(("javascript:", "mailto:", "tel:")):
                raws.append(v)
        text = el.text_content() or ""
        for raw in raws:
            abs_u = urljoin(base_url, raw)
            parsed = urlparse(abs_u)
            if parsed.scheme not in ("http", "https"):
                continue
            if deny is not None and deny(abs_u):
                continue
            if match is not None and not match(text, raw, parsed.path or ""):
                continue
            collected.append(abs_u)
    return dedupe_urls_preserve_order(collected)


def extract_hrefs_regex(
    html: str,
    base_url: str,
    *,
    match: Optional[Callable[[str, str, str], bool]] = None,
    deny: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Stdlib-only href extraction (no lxml) — the fallback used by HTTP deep crawl.

    Coarser than :func:`extract_links_from_html` (no anchor text), but dependency-free
    so link discovery works without the ``[browser]`` extra.
    """
    out: list[str] = []
    for m in _HREF_RE.finditer(html or ""):
        raw = (m.group(1) or "").strip()
        if not raw or raw.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        abs_u = urljoin(base_url, raw)
        parsed = urlparse(abs_u)
        if parsed.scheme not in ("http", "https"):
            continue
        if deny is not None and deny(abs_u):
            continue
        if match is not None and not match("", raw, parsed.path or ""):
            continue
        out.append(abs_u)
    return dedupe_urls_preserve_order(out)


async def _append_dom_hrefs(page: Any, accum: list[str], seen: set[str]) -> None:
    if page is None:
        return
    try:
        raw = await page.evaluate(DOM_HARVEST_JS)
        if not isinstance(raw, list):
            return
        for x in raw:
            s = str(x).strip()
            if s and s not in seen:
                seen.add(s)
                accum.append(s)
    except Exception:
        pass


def install_discovery_hooks(strategy: Any, accum: list[str], seen: set[str]) -> dict[str, Any]:
    """Install DOM-harvest hooks on a crawl4ai strategy; returns the prior hooks to restore.

    Chains three lifecycle points (before retrieve, after js_code, before return) so
    static links, post-expand mega-menu links, and post-settle links are all captured.
    """
    if strategy is None or not hasattr(strategy, "hooks"):
        return {}
    hook_names = ("before_retrieve", "on_execution_ended", "before_return_html")
    prev = {name: strategy.hooks.get(name) for name in hook_names}

    def make(prev_hook):
        async def _hook(page=None, context=None, config=None, result=None, html=None, **kwargs: Any):
            if prev_hook is not None:
                res = prev_hook(page=page, context=context, config=config, result=result,
                                html=html, **kwargs)
                if asyncio.iscoroutine(res):
                    await res
            await _append_dom_hrefs(page, accum, seen)

        return _hook

    for name in hook_names:
        strategy.hooks[name] = make(prev[name])
    return prev


def restore_discovery_hooks(strategy: Any, prev: dict[str, Any]) -> None:
    if strategy is None or not hasattr(strategy, "hooks") or not prev:
        return
    for name, hook in prev.items():
        strategy.hooks[name] = hook
