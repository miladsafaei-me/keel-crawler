"""Page screenshots kept by version (layer 7, 2026-10-16).

A crawler that keeps a page's text keeps what the page said; a reader believes what the
page looked like. This layer renders a page in a browser at a fixed width, saves it as one
WebP, and keeps beside it a map of the page's text blocks with their rectangles, so a
sentence quoted later can be found and framed on the image without the page being opened
again.

The store keeps versions by the rule the owner of prop-firm.review put on 2026-10-16: a
page is shot on every reading; when nothing that matters changed, the new shot **replaces**
the previous one, so the stored image is always the latest confirmation of the page; when
the reading found a change that matters, **both** versions are kept, the older one as the
"before" of that change and the newer one as its "after". What "matters" is the consumer's
call (``keep=True``); the store only honours it, and a version once kept is never replaced.

Layout under the store's root::

    <slug>/<page key>/<stamp>.webp          the page at 1280 px, full height
    <slug>/<page key>/<stamp>.blocks.json   [{"t": text, "x", "y", "w", "h"}] in page pixels
    <slug>/<page key>/index.json            {"url", "versions": [{"stamp", "text_hash", "kept"}]}

A stamp is the reading's time in UTC, ``YYYYMMDDTHHMMSSZ``, so versions sort by name.
Rendering needs ``patchright`` or ``playwright`` (the ``browser`` extra); the store itself
needs neither, and a consumer may store a shot it rendered another way.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

WIDTH = 1280
VIEWPORT_HEIGHT = 900
PAGE_TIMEOUT_MS = 45_000
SETTLE_MS = 1_500
WEBP_QUALITY = 80
MAX_BLOCKS = 3_000
MAX_BLOCK_TEXT = 400
STAMP = "%Y%m%dT%H%M%SZ"

_CONSENT_JS = r"""
() => {
  const words = ['accept all', 'allow all', 'accept cookies', 'i accept', 'accept', 'agree', 'i agree', 'got it', 'ok', 'allow'];
  let clicked = 0;
  for (const el of document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')) {
    const t = ((el.innerText || el.value || '') + '').trim().toLowerCase();
    if (!t || t.length > 40) continue;
    if (words.includes(t) || words.some(w => t === w || t.startsWith(w + ' '))) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) { try { el.click(); clicked++; } catch (e) {} }
    }
  }
  return clicked;
}
"""

# Every element that holds text of its own, with its rectangle in page pixels: the map a
# quote is found on later. Fixed and sticky layers are dropped first, since they would sit
# on the full-page image at their first position only.
_BLOCKS_JS = r"""
(limits) => {
  for (const el of Array.from(document.querySelectorAll('*'))) {
    const pos = getComputedStyle(el).position;
    if (pos === 'fixed' || pos === 'sticky') el.remove();
  }
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  for (let el = walker.nextNode(); el && out.length < limits.max; el = walker.nextNode()) {
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE' || tag === 'SVG') continue;
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue + ' ';
    own = own.replace(/\s+/g, ' ').trim();
    if (!own) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.3) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    out.push({t: own.slice(0, limits.text), x: Math.round(r.left + window.scrollX), y: Math.round(r.top + window.scrollY),
              w: Math.round(r.width), h: Math.round(r.height)});
  }
  return out;
}
"""


@dataclass
class Rendered:
    png: bytes
    blocks: list[dict[str, Any]]
    width: int
    height: int
    url: str
    rendered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def text_hash(self) -> str:
        return text_hash(self.blocks)


#: What a bot checkpoint says instead of the page. A shot of one proves nothing and is never
#: stored (Blueberry Funded's ten pages on 2026-10-16 all read "Performing security
#: verification" to a headless browser).
CHECKPOINT_PHRASES = (
    "performing security verification", "just a moment", "checking your browser", "verify you are human",
    "verifying you are human", "enable javascript and cookies to continue", "access denied", "attention required",
    "ddos protection by", "please wait while we verify", "challenge-platform", "security service to protect",
)


def is_checkpoint(blocks: list[dict[str, Any]]) -> bool:
    """Whether the rendered page is a bot checkpoint rather than the page: few text blocks
    and one of the usual phrases among them."""
    texts = [str(b.get("t") or "").lower() for b in blocks]
    if len(texts) > 60:
        return False
    joined = " ".join(texts)
    return any(phrase in joined for phrase in CHECKPOINT_PHRASES)


def text_hash(blocks: list[dict[str, Any]]) -> str:
    """The page's visible text, hashed: what "unchanged" means to the store."""
    joined = "\n".join(str(b.get("t") or "") for b in blocks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _playwright():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - which one is installed is the consumer's choice
        from playwright.sync_api import sync_playwright
    return sync_playwright


class Renderer:
    """One browser for many pages: ``with Renderer() as r: r.render(url)``. Launching
    Chromium costs more than most pages do (a night of 170 pages took 18 s a page with a
    browser per page on 2026-10-16, against 2 to 7 s for the page itself)."""

    def __init__(self, *, width: int = WIDTH, headless: bool = True, consent: bool = True,
                 timeout_ms: int = PAGE_TIMEOUT_MS, settle_ms: int = SETTLE_MS, user_agent: str = ""):
        self.width, self.headless, self.consent = width, headless, consent
        self.timeout_ms, self.settle_ms = timeout_ms, settle_ms
        self.user_agent = user_agent or ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/129.0.0.0 Safari/537.36")
        self._manager = None
        self._pw = None
        self._browser = None

    def __enter__(self) -> "Renderer":
        # The context manager is kept, not only what it returns: leaving it unexited keeps
        # playwright's loop on the thread, and a Django command then cannot close its
        # database connections ("cannot call this from an async context", 2026-10-16).
        self._manager = _playwright()()
        self._pw = self._manager.__enter__()
        self._browser = self._pw.chromium.launch(headless=self.headless, args=["--disable-blink-features=AutomationControlled"])
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._manager is not None:
                self._manager.__exit__(*exc)
                self._manager = None
                self._pw = None

    def render(self, url: str) -> Rendered:
        """The page as one full-height PNG at the renderer's width plus its text-block map,
        in a fresh context (no cookies or storage carried from the page before)."""
        from PIL import Image

        if self._browser is None:
            raise RuntimeError("use Renderer as a context manager")
        context = self._browser.new_context(
            viewport={"width": self.width, "height": VIEWPORT_HEIGHT}, device_scale_factor=1, locale="en-US",
            user_agent=self.user_agent,
        )
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001 - a page that never idles is still a page
                pass
            page.wait_for_timeout(self.settle_ms)
            if self.consent and page.evaluate(_CONSENT_JS):
                page.wait_for_timeout(600)
            blocks = page.evaluate(_BLOCKS_JS, {"max": MAX_BLOCKS, "text": MAX_BLOCK_TEXT})
            png = page.screenshot(type="png", full_page=True)
        finally:
            context.close()
        image = Image.open(io.BytesIO(png))
        return Rendered(png=png, blocks=blocks, width=image.width, height=image.height, url=url)


def render_page(url: str, **options: Any) -> Rendered:
    """One page in a browser of its own; for many pages use :class:`Renderer`."""
    with Renderer(**options) as renderer:
        return renderer.render(url)


def to_webp(png: bytes, quality: int = WEBP_QUALITY) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, "WEBP", quality=quality, method=6)
    return out.getvalue()


# The store.

_SEGMENT = re.compile(r"[^a-z0-9._-]+")


def page_key(url: str) -> str:
    """A stable directory name for one URL: ``<host>/<path>``, ``www.`` and a trailing
    slash dropped, a query string kept as a short hash."""
    parts = urlsplit((url or "").strip())
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    segments = [_SEGMENT.sub("-", s.lower())[:80] for s in (parts.path or "").split("/") if s and s not in (".", "..")]
    name = "/".join(segments) or "index"
    if parts.query:
        name += "~" + hashlib.sha1(parts.query.encode("utf-8")).hexdigest()[:8]
    return f"{host}/{name}"


def stamp_of(when: datetime) -> str:
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(STAMP)


def when_of(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)


@dataclass
class Version:
    stamp: str
    text_hash: str
    kept: bool
    image: Path
    blocks_file: Path

    @property
    def when(self) -> datetime:
        return when_of(self.stamp)

    def blocks(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.blocks_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []


class ShotStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, slug: str, url: str) -> Path:
        return self.root / slug / page_key(url)

    def _read_index(self, folder: Path) -> dict[str, Any]:
        try:
            return json.loads((folder / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"url": "", "versions": []}

    def _write_index(self, folder: Path, index: dict[str, Any]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")

    def versions(self, slug: str, url: str) -> list[Version]:
        folder = self._dir(slug, url)
        index = self._read_index(folder)
        out = []
        for row in index.get("versions") or []:
            stamp = str(row.get("stamp") or "")
            out.append(Version(stamp=stamp, text_hash=str(row.get("text_hash") or ""), kept=bool(row.get("kept")),
                               image=folder / f"{stamp}.webp", blocks_file=folder / f"{stamp}.blocks.json"))
        return sorted(out, key=lambda v: v.stamp)

    def record(self, slug: str, url: str, *, when: datetime, webp: bytes, blocks: list[dict[str, Any]],
               text_hash_value: str = "", keep: bool = False) -> Version:
        """Store one shot. ``keep=False``: it replaces the latest version unless that one
        is kept. ``keep=True``: the reading found a change that matters, so the latest
        version becomes the kept "before" and this shot the kept "after"."""
        folder = self._dir(slug, url)
        index = self._read_index(folder)
        index["url"] = url
        versions = list(index.get("versions") or [])
        stamp = stamp_of(when)
        digest = text_hash_value or text_hash(blocks)
        if versions:
            latest = versions[-1]
            if keep:
                latest["kept"] = True
            elif not latest.get("kept"):
                for suffix in (".webp", ".blocks.json"):
                    try:
                        (folder / f"{latest['stamp']}{suffix}").unlink()
                    except FileNotFoundError:
                        pass
                versions.pop()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{stamp}.webp").write_bytes(webp)
        (folder / f"{stamp}.blocks.json").write_text(json.dumps(blocks, separators=(",", ":")), encoding="utf-8")
        versions = [v for v in versions if v.get("stamp") != stamp]
        versions.append({"stamp": stamp, "text_hash": digest, "kept": bool(keep)})
        index["versions"] = sorted(versions, key=lambda v: v["stamp"])
        self._write_index(folder, index)
        return Version(stamp=stamp, text_hash=digest, kept=bool(keep), image=folder / f"{stamp}.webp",
                       blocks_file=folder / f"{stamp}.blocks.json")

    def before(self, slug: str, url: str, when: datetime) -> Version | None:
        """The latest version taken at or before ``when``."""
        candidates = [v for v in self.versions(slug, url) if v.when <= when and v.image.is_file()]
        return candidates[-1] if candidates else None

    def after(self, slug: str, url: str, when: datetime) -> Version | None:
        """The earliest version taken at or after ``when``."""
        candidates = [v for v in self.versions(slug, url) if v.when >= when and v.image.is_file()]
        return candidates[0] if candidates else None

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


# Finding a sentence on a stored shot.

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


def find_block(blocks: list[dict[str, Any]], quote: str) -> dict[str, Any] | None:
    """The block that holds ``quote`` (whitespace and case aside), else the block sharing
    the longest run of its words, if that run is at least six words; else ``None``."""
    needle = _norm(quote)
    if not needle:
        return None
    for block in blocks:
        if needle in _norm(block.get("t", "")):
            return block
    words = needle.split()
    best, best_len = None, 0
    for block in blocks:
        text = _norm(block.get("t", ""))
        for size in range(min(len(words), 24), 5, -1):
            found = False
            for start in range(0, len(words) - size + 1):
                if " ".join(words[start:start + size]) in text:
                    found = True
                    break
            if found:
                if size > best_len:
                    best, best_len = block, size
                break
    return best


def frame(webp_bytes: bytes, block: dict[str, Any], *, above: int = 170, below: int = 170, pad: int = 8,
          colour: tuple[int, int, int] = (19, 18, 58), out_width: int = 1200, quality: int = 84) -> bytes:
    """A crop of the stored shot around ``block``, the block framed in ``colour``."""
    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(webp_bytes)).convert("RGB")
    top = min(max(0, int(block["y"]) - above), image.height - 1)
    bottom = max(top + 1, min(image.height, int(block["y"]) + int(block["h"]) + below))
    crop = image.crop((0, top, image.width, bottom))
    draw = ImageDraw.Draw(crop, "RGBA")
    x0 = max(0, int(block["x"]) - pad)
    x1 = min(crop.width - 1, int(block["x"]) + int(block["w"]) + pad)
    y0 = max(0, int(block["y"]) - top - pad)
    y1 = min(crop.height - 1, int(block["y"]) + int(block["h"]) - top + pad)
    draw.rounded_rectangle((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)), radius=10, fill=colour + (28,),
                           outline=colour + (255,), width=3)
    if crop.width > out_width:
        crop = crop.resize((out_width, max(1, round(crop.height * out_width / crop.width))), Image.LANCZOS)
    out = io.BytesIO()
    crop.save(out, "WEBP", quality=quality, method=6)
    return out.getvalue()
