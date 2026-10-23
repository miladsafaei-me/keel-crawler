"""The earliest Wayback Machine capture of a site that already says something (2026-10-23).

A domain's first capture says when the name was first archived, not when the business on it
began: ftmo.com was archived in 2002, twelve years before the firm that sells challenges on
it. So the question asked here is narrower and checkable: the first capture of a page on the
site whose own text matches every pattern given (a brand and the words of its trade). It is
a fact about that domain: a business that moved from another domain (topsteptrader.com to
topstep.com) is first found on the new one when it moved.

The search reads the Wayback CDX index (no key) for one capture a year, fetches those pages
oldest first until one matches, then reads the year before it month by month, because a
site that launched in May was first captured matching in May and the yearly sample only
saw January. At most ``max_fetches`` pages are read. Business-blind: the caller says what
the page must contain.

    from keel_crawler.archive import earliest_capture
    found = earliest_capture(["ftmo.com", "www.ftmo.com"], [r"ftmo", r"challenge|funded"])
    # {"timestamp": "20150314...", "date": date(2015, 3, 14), "original": "...", "url": "https://web.archive.org/web/..."}
"""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, Iterable

import requests

CDX = "https://web.archive.org/cdx/search/cdx"
RAW = "https://web.archive.org/web/{timestamp}id_/{original}"
SHOWN = "https://web.archive.org/web/{timestamp}/{original}"
USER_AGENT = "keel-crawler archive check (+https://github.com/miladsafaei-me/keel-crawler)"
TIMEOUT = 40
PAUSE_SECONDS = 4.0


class ArchiveError(RuntimeError):
    """The archive did not answer."""


def _session(session: requests.Session | None) -> requests.Session:
    if session is not None:
        return session
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


# The home page and the language path a home page most often redirects to: a redirect is
# archived as the redirect, and the site itself lives under the path. A prefix query over
# the whole domain is what the index refuses (503) on a large site.
ENTRY_PATHS = ("/", "/en/")
RETRIES = 3


def captures(host: str, *, collapse: str, start: str = "", end: str = "",
             session: requests.Session | None = None, timeout: float = TIMEOUT) -> list[tuple[str, str]]:
    """``(timestamp, original)`` of the HTML captures of ``host``'s entry pages
    (``ENTRY_PATHS``), one per ``collapse`` period ("timestamp:4" a year, "timestamp:6" a
    month) and path, oldest first."""
    out: list[tuple[str, str]] = []
    for path in ENTRY_PATHS:
        params = {"url": f"{host}{path}", "output": "json", "fl": "timestamp,original",
                  "filter": ["statuscode:200", "mimetype:text/html"], "collapse": collapse}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        out += _index(params, session=session, timeout=timeout)
    return sorted(set(out))


def _index(params: dict[str, Any], *, session: requests.Session | None, timeout: float) -> list[tuple[str, str]]:
    last = ""
    for attempt in range(RETRIES):
        try:
            answer = _session(session).get(CDX, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last = f"the Wayback index did not answer: {exc}"
        else:
            if answer.status_code == 200:
                try:
                    rows = answer.json() if answer.text.strip() else []
                except ValueError as exc:
                    raise ArchiveError("the Wayback index answered something that is not JSON") from exc
                return [(r[0], r[1]) for r in rows[1:] if len(r) >= 2]
            last = f"the Wayback index answered {answer.status_code}"
            if answer.status_code < 500:
                break
        time.sleep(PAUSE_SECONDS * (attempt + 2))
    raise ArchiveError(last)


def page_text(timestamp: str, original: str, *, session: requests.Session | None = None,
              timeout: float = TIMEOUT) -> str:
    """The archived page's raw HTML, as it was captured (no Wayback toolbar). Raises
    ``ArchiveError`` when the archive does not hand it over: an empty answer is the archive
    throttling, and reading it as "this page does not match" would date a site too late."""
    last = ""
    for attempt in range(RETRIES):
        try:
            answer = _session(session).get(RAW.format(timestamp=timestamp, original=original), timeout=timeout)
        except requests.RequestException as exc:
            last = f"the archive did not answer: {exc}"
        else:
            if answer.status_code == 200 and answer.text.strip():
                return answer.text
            last = f"the archive answered {answer.status_code} with {len(answer.text)} characters"
        time.sleep(PAUSE_SECONDS * (attempt + 3))
    raise ArchiveError(last)


def _visible(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))


def _matches(html: str, patterns: list[re.Pattern[str]]) -> bool:
    text = _visible(html)
    return bool(text.strip()) and all(p.search(text) for p in patterns)


def _found(timestamp: str, original: str) -> dict[str, Any]:
    return {"timestamp": timestamp, "original": original,
            "date": date(int(timestamp[:4]), int(timestamp[4:6]), int(timestamp[6:8])),
            "url": SHOWN.format(timestamp=timestamp, original=original)}


def earliest_capture(hosts: Iterable[str], patterns: Iterable[str], *, session: requests.Session | None = None,
                     max_fetches: int = 30, pause: float = PAUSE_SECONDS,
                     timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """The earliest capture of any of ``hosts`` whose page text matches every pattern, or
    ``None`` when no capture read does. Raises ``ArchiveError`` when the index or a page does
    not answer, so a caller never keeps a date the archive did not let us check."""
    s = _session(session)
    rx = [re.compile(p, re.I) for p in patterns]
    yearly: dict[str, tuple[str, str]] = {}
    hosts = list(dict.fromkeys(h.lower().removeprefix("www.") for h in hosts))
    for host in hosts:
        for ts, original in captures(host, collapse="timestamp:4", session=s, timeout=timeout):
            if ts[:4] not in yearly or ts < yearly[ts[:4]][0]:
                yearly[ts[:4]] = (ts, original)
    fetches = 0
    hit: tuple[str, str] | None = None
    years = sorted(yearly)
    for year in years:
        if fetches >= max_fetches:
            return None
        ts, original = yearly[year]
        fetches += 1
        if _matches(page_text(ts, original, session=s, timeout=timeout), rx):
            hit = (ts, original)
            break
        time.sleep(pause)
    if hit is None:
        return None
    # The year before the first matching sample, month by month: the site may have launched
    # after that year's single sample was taken.
    before = str(int(hit[0][:4]) - 1)
    if before in yearly:
        monthly: list[tuple[str, str]] = []
        for host in hosts:
            monthly += captures(host, collapse="timestamp:6", start=before, end=before, session=s, timeout=timeout)
        for ts, original in sorted(set(monthly)):
            if ts <= yearly[before][0] or ts >= hit[0]:
                continue
            if fetches >= max_fetches:
                break
            fetches += 1
            time.sleep(pause)
            if _matches(page_text(ts, original, session=s, timeout=timeout), rx):
                hit = (ts, original)
                break
    return _found(*hit)
