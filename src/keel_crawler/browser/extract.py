"""Turn a crawl4ai ``CrawlResult`` into ``(title, text)`` — best of markdown vs DOM main.

Extracted verbatim (minus forex specifics) from Revenika's platform_crawler text
extraction. ``lxml`` is imported lazily so a Layer-0-only host never needs it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

MAX_TEXT_CHARS_PER_URL = 80_000

_MAIN_MIN_CHARS = 500
_ARTICLE_MIN_CHARS = 200


@dataclass
class CrawledPage:
    """One crawl outcome. ``url`` is the final location after HTTP/JS redirects."""

    url: str
    text: str
    title: str = ""
    error: str = ""
    requested_url: str = ""
    egress_proxy: bool | None = None
    egress_ip: str = ""
    discovery_hrefs: list[str] = field(default_factory=list)
    # Number of fetch attempts made for this page inside the last browser session
    # (the transient-retry ladder). 1 = succeeded/failed on the first try.
    attempts: int = 1

    def ok(self) -> bool:
        return not (self.error or "").strip() and bool((self.text or "").strip())


def with_egress(page: CrawledPage, *, via_proxy: bool, egress_ip: str = "") -> CrawledPage:
    return replace(page, egress_proxy=via_proxy, egress_ip=(egress_ip or "")[:64])


def _normalize_blob(s: str) -> str:
    return " ".join((s or "").split())


def _parse_html(html_str: str | None) -> Any:
    """Parse HTML into an lxml tree once, or ``None`` on any failure. lxml is lazy."""
    if not html_str or not html_str.strip():
        return None
    try:
        from lxml import html as lxml_html

        return lxml_html.fromstring(html_str)
    except Exception:
        return None


def _title_from_tree(tree: Any) -> str:
    if tree is None:
        return ""
    try:
        titles = tree.xpath("//title/text()")
        if titles:
            return " ".join(str(titles[0]).split())
    except Exception:
        pass
    return ""


def _title_from_html_fragment(html_fragment: str | None) -> str:
    return _title_from_tree(_parse_html(html_fragment))


def _gather_visible_text(nodes: list[Any]) -> str:
    parts: list[str] = []
    for node in nodes:
        if not hasattr(node, "xpath"):
            continue
        try:
            for t in node.xpath(".//text()"):
                if t is None:
                    continue
                frag = str(t).strip()
                if frag:
                    parts.append(frag)
        except Exception:
            continue
    return _normalize_blob(" ".join(parts))


def _drop_nonvisible(tree: Any) -> None:
    """Drop script/style/noscript subtrees in place (their text is never visible)."""
    for bad in tree.xpath("//script|//style|//noscript"):
        try:
            bad.drop_tree()
        except Exception:
            pass


def _full_visible_len(tree: Any) -> int:
    """Length of ALL visible text in ``tree`` (call after :func:`_drop_nonvisible`).

    This is the JS-shell floor the cheap-first path checks — it counts nav/header/
    footer text too, matching the old regex ``estimate_visible_text_len`` — but is
    derived from the same lxml parse that produces the page, so no separate scan runs.
    """
    if tree is None:
        return 0
    try:
        parts = tree.xpath("//body//text()") or tree.xpath("//text()")
        return len(_normalize_blob(" ".join(str(t).strip() for t in parts if t and str(t).strip())))
    except Exception:
        return 0


def _main_text_from_tree(tree: Any) -> str:
    """Prefer <main>/role=main/<article> after stripping chrome; fall back to <body>.

    Mutates ``tree`` (drops chrome nodes), so extract any title BEFORE calling this.
    """
    if tree is None:
        return ""
    _drop_nonvisible(tree)
    for tag in ("header", "footer", "nav"):
        for el in list(tree.xpath(f"//{tag}")):
            try:
                el.drop_tree()
            except Exception:
                pass

    chunk = _gather_visible_text(tree.xpath("//main | .//*[@role='main']"))
    if len(chunk) >= _MAIN_MIN_CHARS:
        return chunk
    chunk = _gather_visible_text(tree.xpath("//article"))
    if len(chunk) >= _ARTICLE_MIN_CHARS:
        return chunk
    bodies = tree.xpath("//body")
    if bodies:
        chunk = _gather_visible_text(bodies[:1])
        if chunk:
            return chunk
    try:
        parts = tree.xpath("//text()")
        return _normalize_blob(" ".join(str(t).strip() for t in parts if t and str(t).strip()))
    except Exception:
        return ""


def main_content_text_from_html(html_str: str) -> str:
    """Prefer <main>/role=main/<article> after stripping chrome; fall back to <body>."""
    return _main_text_from_tree(_parse_html(html_str))


def title_and_main_text_from_html(html_str: str) -> tuple[str, str]:
    """Parse ``html_str`` **once** and return ``(title, main_text)``.

    Used by the cheap-first path so an HTTP-served page pays a single lxml parse for
    both its title and its main content instead of two.
    """
    tree = _parse_html(html_str)
    if tree is None:
        return "", ""
    title = _title_from_tree(tree)  # before _main_text_from_tree mutates the tree
    return title, _main_text_from_tree(tree)


def _text_from_cleaned_html(html_str: str) -> str:
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html_str)
        for bad in tree.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        parts = tree.xpath("//text()")
        return _normalize_blob(" ".join(p.strip() for p in parts if p and p.strip()))
    except Exception:
        return ""


def markdown_text_from_result(result: Any) -> str:
    """crawl4ai stores markdown in a private result; ``fit_markdown`` is usually the article."""
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    inner = getattr(md, "_markdown_result", None)
    if inner is not None:
        def _g(attr: str) -> str:
            try:
                v = getattr(inner, attr, None)
                return str(v).strip() if v is not None else ""
            except Exception:
                return ""

        fit = _g("fit_markdown")
        raw = _g("raw_markdown")
        cite = _g("markdown_with_citations")
        refs = _g("references_markdown")
        if len(fit) >= 200:
            return fit
        chunks = [x for x in (raw, cite, refs, fit) if x]
        if chunks:
            return max(chunks, key=len)
        return str(md).strip()
    if isinstance(md, str):
        return md.strip()
    return str(md).strip()


def _dom_main_text_from_result(result: Any) -> str:
    fit_html = getattr(result, "fit_html", None)
    if fit_html and str(fit_html).strip():
        t = main_content_text_from_html(str(fit_html))
        if t.strip():
            return t
    html_src = getattr(result, "cleaned_html", None) or getattr(result, "html", None)
    if html_src:
        return main_content_text_from_html(str(html_src))
    return ""


def text_from_crawl_result(result: Any) -> tuple[str, str]:
    """Return ``(title, body_text)`` from a crawl4ai ``CrawlResult`` (best of markdown vs DOM)."""
    title = ""
    if getattr(result, "metadata", None) and isinstance(result.metadata, dict):
        raw_title = result.metadata.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            title = " ".join(raw_title.split())

    body_from_html = _dom_main_text_from_result(result)
    md_body = markdown_text_from_result(result)

    if md_body:
        if not body_from_html:
            body = md_body
        elif len(body_from_html) < 400:
            body = md_body if len(md_body) > len(body_from_html) else body_from_html
        elif (
            len(md_body) > len(body_from_html) * 2
            and len(md_body) > 4000
            and len(body_from_html) < 3000
        ):
            body = md_body
        else:
            body = body_from_html
    else:
        body = body_from_html

    if not body and getattr(result, "extracted_content", None):
        body = str(result.extracted_content).strip()
    if not body and getattr(result, "cleaned_html", None):
        body = _text_from_cleaned_html(result.cleaned_html)
    if not body and getattr(result, "html", None):
        body = _text_from_cleaned_html(result.html)

    if not title:
        title = _title_from_html_fragment(getattr(result, "cleaned_html", None)) or (
            _title_from_html_fragment(getattr(result, "html", None))
        )
    if not title and body:
        first = body.split("\n", 1)[0].strip()
        if len(first) <= 200:
            title = first
    return title, body


def _final_url_after_redirect(requested_url: str, result: Any) -> tuple[str, str]:
    req_raw = (requested_url or "").strip()
    eff = req_raw
    redir = getattr(result, "redirected_url", None)
    if isinstance(redir, str) and redir.strip():
        eff = redir.strip()
    req_field = req_raw if eff != req_raw else ""
    return eff, req_field


def crawl_result_to_page(requested_url: str, result: Any) -> CrawledPage:
    """Convert a crawl4ai result into a :class:`CrawledPage` (with error/empty handling)."""
    eff, req_field = _final_url_after_redirect(requested_url, result)
    if not getattr(result, "success", False):
        err = getattr(result, "error_message", None) or "Crawl failed"
        return CrawledPage(url=eff, text="", title="", error=str(err), requested_url=req_field)

    title, text = text_from_crawl_result(result)
    if not text:
        return CrawledPage(
            url=eff,
            text="",
            title=title,
            error="No extractable text (empty markdown/HTML).",
            requested_url=req_field,
        )
    if len(text) > MAX_TEXT_CHARS_PER_URL:
        text = text[:MAX_TEXT_CHARS_PER_URL]
    return CrawledPage(url=eff, text=text, title=title, error="", requested_url=req_field)


def _page_from_parts(url: str, req_field: str, title: str, text: str) -> CrawledPage:
    if not text:
        return CrawledPage(
            url=url, text="", title=title, error="No extractable text", requested_url=req_field
        )
    if len(text) > MAX_TEXT_CHARS_PER_URL:
        text = text[:MAX_TEXT_CHARS_PER_URL]
    return CrawledPage(url=url, text=text, title=title, error="", requested_url=req_field)


def page_from_html(requested_url: str, html: str, final_url: str = "") -> CrawledPage:
    """Build a :class:`CrawledPage` from raw HTML fetched cheaply (no crawl4ai).

    The cheap-first path uses this so an HTTP-served page goes through the same
    main-content + title DOM heuristics as a browser-served one — downstream code sees
    one consistent shape regardless of transport.
    """
    url = (final_url or requested_url or "").strip()
    req_field = requested_url.strip() if (final_url and final_url != requested_url) else ""
    if not (html or "").strip():
        return CrawledPage(url=url, text="", title="", error="empty HTML", requested_url=req_field)
    title, text = title_and_main_text_from_html(html)
    return _page_from_parts(url, req_field, title, text)


def page_and_visible_len_from_html(
    requested_url: str, html: str, final_url: str = ""
) -> tuple[CrawledPage, int]:
    """Parse cheap HTML **once** → ``(CrawledPage, full_visible_text_len)``.

    The cheap-first hybrid path needs two things from a page: the extracted content
    (to keep) and a total visible-text length (to decide whether the page is a real
    document or a near-empty JS shell worth escalating to the browser). Doing both from
    a single lxml parse — instead of a separate regex scan plus a parse — halves the
    per-page work on the common HTTP-served path.
    """
    url = (final_url or requested_url or "").strip()
    req_field = requested_url.strip() if (final_url and final_url != requested_url) else ""
    if not (html or "").strip():
        return (
            CrawledPage(url=url, text="", title="", error="empty HTML", requested_url=req_field),
            0,
        )
    tree = _parse_html(html)
    if tree is None:
        return (
            CrawledPage(
                url=url, text="", title="", error="No extractable text", requested_url=req_field
            ),
            0,
        )
    title = _title_from_tree(tree)  # before _main_text_from_tree mutates the tree
    _drop_nonvisible(tree)
    visible_len = _full_visible_len(tree)  # counts chrome too — before main-text strips it
    text = _main_text_from_tree(tree)
    return _page_from_parts(url, req_field, title, text), visible_len
