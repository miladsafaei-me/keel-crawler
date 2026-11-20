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
  time, text).
- :func:`read_idea`: what an idea says, by rules: its direction (long, short or
  unknown), its timeframe, the price levels it names near a reference price, and
  whether it is an advert for a paid group.

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


@dataclass
class Reading:
    """What an idea says, read by rules."""

    direction: str = ""          # "long", "short" or "" when the text does not say
    timeframe: str = ""          # "15m", "4h", "1D", "1W" or ""
    timeframe_minutes: int = 0
    levels: list = field(default_factory=list)
    promo: bool = False


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


def read_idea(title: str, text: str, ref_price: float | None = None, band: float = 0.03) -> Reading:
    """Direction, timeframe, price levels within ``band`` of ``ref_price``, and the advert flag."""
    tf, minutes = _timeframe(title, text)
    both = title + " " + text
    return Reading(
        direction=_direction(title, text),
        timeframe=tf,
        timeframe_minutes=minutes,
        levels=_levels(both, ref_price, band),
        promo=bool(_PROMO.search(both)),
    )
