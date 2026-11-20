"""TradingView ideas: the public RSS feed of one symbol, parsed and read with no model.

TradingView publishes the newest ideas of each symbol as RSS 2.0 at
``https://www.tradingview.com/feed/?sort=recent&symbol=<EXCHANGE:TICKER>`` (about
30 items). Each item carries a title, a link, the publication time, the plain text
and, inside ``content:encoded``, the author's profile link. The feed has no
direction, timeframe, likes or follower counts, so those are read from the text.

What this module does, all stdlib:

- :func:`feed_url` and :func:`looks_like_feed`: the address of a symbol's feed and
  the acceptance test a proxy pool verifies against (:mod:`keel_crawler.proxy.batch`).
- :func:`parse_feed`: RSS text to :class:`Idea` records (id, title, link, author,
  time, text, chart snapshot).
- :func:`read_idea`: what an idea says, by rules: its direction (long, short or
  unknown), its timeframe, the price levels it names near a reference price and
  which of them are targets, stops and entries, the methods it names, and whether
  it is an advert for a paid group.

The text is for reading, never for republishing. TradingView's House Rules ask
for permission and credit before anyone reuses an author's content, and its Terms
limit automated access; a consumer publishes its own aggregates and links to the
idea, it does not copy the idea. Fetch through the proxy pool, never from a
production host's own address.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

FEED = "https://www.tradingview.com/feed/?sort=recent&symbol={symbol}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

LONG, SHORT = "long", "short"

_ID = re.compile(r"/chart/[^/]+/([A-Za-z0-9]+)-")
_AUTHOR = re.compile(r'href="(https://www\.tradingview\.com/u/([^/"]+)/)"')
_CHART = re.compile(r'src="(https://s3\.tradingview\.com/[a-z0-9]/([A-Za-z0-9]+)_mid\.png)"')
_SNAPSHOT = re.compile(r"https?://(?:www\.)?tradingview\.com/x/[A-Za-z0-9]+/?")
_SPACE = re.compile(r"\s+")

_LONG_WORDS = re.compile(
    r"\b(long|longs|buy|buying|buys|bullish|bulls?|upside|rally|uptrend|calls?)\b", re.I)
_SHORT_WORDS = re.compile(
    r"\b(short|shorts|shorting|sell|selling|sells|bearish|bears?|downside|downtrend|sell-off|selloff|drop)\b",
    re.I)

# Promotion of a paid group or a broker: the idea is an advert, not an analysis.
_PROMO = re.compile(
    r"\b(vip|telegram|whats\s?app|join\s+(?:us|our|my)|link\s+in\s+(?:my\s+)?(?:bio|profile|signature)"
    r"|dm\s+me|subscribe|sponsored|promo\s?code|affiliate|signal\s+group|free\s+signals?\s+(?:group|channel))\b",
    re.I)

_TF_PATTERNS = (
    (re.compile(r"\b(?:m(1|3|5|15|30|45)|(1|3|5|15|30|45)\s?(?:m|min|mins|minutes?))\b", re.I), 1),
    (re.compile(r"\b(?:h(1|2|4|6|8|12)|(1|2|4|6|8|12)\s?(?:h|hr|hrs|hours?))\b", re.I), 60),
    (re.compile(r"\b(?:(1)\s?d|d(1)|(daily)|(1)\s?day)\b", re.I), 1440),
    (re.compile(r"\b(?:(1)\s?w|w(1)|(weekly))\b", re.I), 10080),
)

# A decimal price: not part of a longer number (a sentence's full stop or comma
# after it is fine), and never a percentage.
_NUMBER = re.compile(r"(?<![\d.,])(\d{1,5}\.\d{1,5})(?!\d|[.,]\d|\s?%)")


@dataclass
class Idea:
    """One item of a symbol's feed."""

    id: str
    title: str
    url: str
    author: str
    author_url: str
    published: datetime
    text: str
    image: str = ""              # the idea's chart snapshot (932 x 550 PNG on TradingView's CDN)


@dataclass
class Reading:
    """What an idea says, read by rules."""

    direction: str = ""          # "long", "short" or "" when the text does not say
    timeframe: str = ""          # "15m", "4h", "1D", "1W" or ""
    timeframe_minutes: int = 0
    levels: list = field(default_factory=list)
    promo: bool = False
    # Roles of the levels, by the words around each number (see read_roles).
    targets: list = field(default_factory=list)
    stops: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    # Methods the text names, in a fixed vocabulary (see METHODS).
    methods: list = field(default_factory=list)


def feed_url(symbol: str) -> str:
    """The RSS address of one symbol, e.g. ``feed_url("FX:EURUSD")``."""
    return FEED.format(symbol=quote(symbol, safe=":"))


def looks_like_feed(status: int, body: str) -> bool:
    """Acceptance test for a pool: a 200 that is really the RSS document."""
    return status == 200 and "<rss" in (body or "")[:2000]


def clean_text(raw: str) -> str:
    """Plain text of an item: entities decoded, chart snapshot links and spacing removed."""
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = _SNAPSHOT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def chart_image(content_html: str, idea_id: str) -> str:
    """The snapshot of this idea's own chart in ``content:encoded``, or "".

    Only TradingView's CDN address whose name is the idea's id counts, so an image
    the author pasted into the text is never taken for the idea's chart. Showing it
    elsewhere needs the author and TradingView credited next to it (TradingView
    Terms, section 4, and House Rules); hotlink it, do not copy it.
    """
    for found in _CHART.finditer(content_html or ""):
        if found.group(2) == idea_id:
            return found.group(1)
    return ""


def parse_feed(xml_text: str) -> list:
    """Every item of a feed as an :class:`Idea`; items without a link or time are skipped."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ideas = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        found = _ID.search(link)
        when = item.findtext("pubDate")
        if not found or not when:
            continue
        try:
            published = parsedate_to_datetime(when).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        content = item.findtext(CONTENT_NS) or ""
        author = _AUTHOR.search(content)
        ideas.append(Idea(
            id=found.group(1),
            title=clean_text(item.findtext("title") or ""),
            url=link,
            author=author.group(2) if author else "",
            author_url=author.group(1) if author else "",
            published=published,
            text=clean_text(item.findtext("description") or ""),
            image=chart_image(content, found.group(1)),
        ))
    return ideas


def _direction(title: str, text: str) -> str:
    # The title states the call far more often than the body does, so it weighs
    # three times as much. A side wins only with twice the other side's weight.
    long_w = 3 * len(_LONG_WORDS.findall(title)) + len(_LONG_WORDS.findall(text))
    short_w = 3 * len(_SHORT_WORDS.findall(title)) + len(_SHORT_WORDS.findall(text))
    if long_w >= 2 and long_w >= 2 * short_w:
        return LONG
    if short_w >= 2 and short_w >= 2 * long_w:
        return SHORT
    return ""


def _timeframe(title: str, text: str) -> tuple:
    for source in (title, text[:400]):
        for pattern, unit in _TF_PATTERNS:
            found = pattern.search(source)
            if not found:
                continue
            value = next((g for g in found.groups() if g), "1")
            n = 1 if not value.isdigit() else int(value)
            minutes = n * unit
            if unit == 1:
                return "%dm" % n, minutes
            if unit == 60:
                return "%dh" % n, minutes
            return ("1D", minutes) if unit == 1440 else ("1W", minutes)
    return "", 0


def _levels(text: str, ref_price: float | None, band: float) -> list:
    if not ref_price:
        return []
    low, high = ref_price * (1 - band), ref_price * (1 + band)
    seen = set()
    for raw in _NUMBER.findall(text):
        value = float(raw)
        if low <= value <= high:
            seen.add(value)
    return sorted(seen)


# The analysis methods an idea can name, as (label, pattern). Order is the order
# they are reported in.
METHODS = (
    ("Elliott wave count", r"elliott|\bwave\s*[\"']?[1-5abc]\b|impulse count"),
    ("double top", r"double\s+top"),
    ("double bottom", r"double\s+bottom"),
    ("head and shoulders", r"head\s*(?:and|&)\s*shoulders"),
    ("price channel", r"channel"),
    ("trendline", r"trend\s?line|supply\s+line|demand\s+line"),
    ("wedge or triangle", r"wedge|triangle"),
    ("flag", r"\bflag\b|pennant"),
    ("support and resistance", r"support|resistance|pivot"),
    ("Fibonacci", r"fibonacci|\bfib\b|retracement"),
    ("moving averages", r"moving\s+average|\bma\s?\d{2,3}\b|\bema\b|\bsma\b"),
    ("RSI", r"\brsi\b"),
    ("MACD", r"\bmacd\b"),
    ("Ichimoku", r"ichimoku"),
    ("divergence", r"divergence"),
    ("volume profile", r"volume\s+profile|point\s+of\s+control|\bpoc\b"),
    ("smart money concepts", r"order\s+block|fair\s+value\s+gap|\bfvg\b|liquidity\s+(?:grab|sweep)|\bbos\b|\bchoch\b"),
    ("market structure", r"lower\s+highs|higher\s+lows|market\s+structure"),
    ("fundamentals", r"\bfed\b|\becb\b|\bboj\b|\bboe\b|\bsnb\b|\brba\b|inflation|\bcpi\b|rate\s+(?:hike|cut)|yields?\b|payrolls|\bgdp\b"),
)
_METHODS = tuple((label, re.compile(pattern, re.I)) for label, pattern in METHODS)

_STOP_WORDS = re.compile(r"stop[\s-]*loss|\bstop\b|\bsl\b|invalidat|cancel", re.I)
_TARGET_WORDS = re.compile(r"target|\btp\s*\d?\b|take[\s-]*profit|objective", re.I)
_ENTRY_WORDS = re.compile(r"\bentry|\benter\b|(?:buy|sell)\s+(?:at|from|zone|limit)", re.I)
_AFTER_STOP = re.compile(r"^[^.]{0,60}?(?:would|will|to)\s+invalidat", re.I)
_RANGE_JOIN = re.compile(r"^\s*(?:-|to|and)\s*$", re.I)


def read_methods(text: str) -> list:
    """The labels of METHODS the text names, in METHODS order."""
    return [label for label, pattern in _METHODS if pattern.search(text or "")]


def read_roles(text: str, ref_price: float | None, band: float = 0.03) -> dict:
    """Which named levels are targets, stops and entries, by the words before each number.

    The label is the last role word between the previous number (or 60 characters
    back, never past the start of the sentence) and this one, so "Stop Loss: 1.1015
    - Take Profit 1: 1.0950" gives each number its own role. The second number of a
    range ("1.0980 - 1.0990") takes the first one's role. A number followed within
    its sentence by "would invalidate" is a stop. Unlabelled numbers get no role.
    """
    roles = {"targets": [], "stops": [], "entries": []}
    if not ref_price:
        return roles
    low, high = ref_price * (1 - band), ref_price * (1 + band)
    last_end, last_role = 0, ""
    for found in _NUMBER.finditer(text or ""):
        value = float(found.group(1))
        before = text[max(last_end, found.start() - 60):found.start()]
        joined = last_end and _RANGE_JOIN.match(text[last_end:found.start()])
        last_end = found.end()
        before = before.rsplit(". ", 1)[-1]
        marks = [(m.end(), role) for role, words in (("stops", _STOP_WORDS), ("targets", _TARGET_WORDS),
                                                   ("entries", _ENTRY_WORDS)) for m in words.finditer(before)]
        if marks:
            role = max(marks)[1]
        elif joined:
            role = last_role
        else:
            role = "stops" if _AFTER_STOP.search(text[found.end():found.end() + 80]) else ""
        last_role = role
        if role and low <= value <= high and value not in roles[role]:
            roles[role].append(value)
    return roles


def read_idea(title: str, text: str, ref_price: float | None = None, band: float = 0.03) -> Reading:
    """Direction, timeframe, levels near ``ref_price`` and their roles, methods, and the advert flag."""
    tf, minutes = _timeframe(title, text)
    both = title + " " + text
    roles = read_roles(text, ref_price, band)
    return Reading(
        direction=_direction(title, text),
        timeframe=tf,
        timeframe_minutes=minutes,
        levels=_levels(both, ref_price, band),
        promo=bool(_PROMO.search(both)),
        targets=roles["targets"],
        stops=roles["stops"],
        entries=roles["entries"],
        methods=read_methods(both),
    )
