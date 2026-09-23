"""YouTube's own autocomplete, read as a demand signal.

The suggestions under a search box are ordered by how often people actually type
them, which makes them the only free reading of YouTube demand that comes from
YouTube. No volume is attached, so a single snapshot says only "these exist";
the signal is in the **difference between two snapshots**, because a phrase that
appears where it was not before is the platform reporting that demand moved.

Unlike the Data API, this endpoint rations by address rather than by key, so a host
that polls a few hundred prefixes a day needs the toolkit's proxy support here and
nowhere else in this package's YouTube layer.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"


class SuggestUnavailable(RuntimeError):
    """The endpoint refused or answered something that is not a suggestion list."""


def fetch_suggestions(
    prefix: str,
    *,
    language: str = "en",
    region: str = "US",
    session: requests.Session | None = None,
    proxy_url: str = "",
    timeout: int = 10,
) -> list[str]:
    """Suggestions for one prefix, lowercased and de-duplicated in place."""
    http = session or requests
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    response = http.get(
        SUGGEST_URL,
        params={
            "client": "firefox",
            "ds": "yt",
            "hl": language,
            "gl": region.lower(),
            "q": prefix,
        },
        timeout=timeout,
        proxies=proxies,
    )
    if response.status_code != 200:
        raise SuggestUnavailable(f"HTTP {response.status_code} for {prefix!r}")
    try:
        payload = json.loads(response.text)
    except ValueError as exc:
        raise SuggestUnavailable(f"unparseable answer for {prefix!r}") from exc
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise SuggestUnavailable(f"unexpected shape for {prefix!r}")
    seen: list[str] = []
    for item in payload[1]:
        text = str(item).strip().lower()
        if text and text not in seen:
            seen.append(text)
    return seen


def diff_snapshots(previous: Iterable[str], current: Iterable[str]) -> tuple[list[str], list[str]]:
    """What appeared and what vanished between two snapshots of the same prefix.

    Both halves are reported. A phrase leaving the list is as real a movement as one
    arriving, and a host that only watches arrivals reads a falling topic as a stable
    one.
    """
    before = {text.strip().lower() for text in previous if text.strip()}
    after = {text.strip().lower() for text in current if text.strip()}
    return sorted(after - before), sorted(before - after)
