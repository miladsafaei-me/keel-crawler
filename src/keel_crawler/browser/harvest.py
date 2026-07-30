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
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

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


def dedupe_urls_preserve_order(urls: list[str]) -> list[str]:
    """Collapse equivalent URLs (www, trailing slash, http/https) keeping first spelling."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        u = (raw or "").strip()
        if not u:
            continue
        try:
            p = urlparse(u)
            host = (p.netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            path = p.path or "/"
            if path != "/" and path.endswith("/"):
                path = path[:-1]
            key = f"https://{host}{path}" + (f"?{p.query}" if p.query else "")
        except Exception:
            key = u.lower()
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
