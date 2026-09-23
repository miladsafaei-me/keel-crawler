"""A quota-aware client for the YouTube Data API v3.

Business-blind: it knows about channels, playlists, videos and comment threads, and
nothing about why a host is reading them. It carries three opinions, each of which
exists because the obvious implementation costs a hundred times more than it needs
to.

**Uploads come from the uploads playlist, never from a search.** Every channel has a
hidden playlist holding its uploads in reverse order, readable for 1 unit a page,
while ``search.list`` filtered to that channel costs 100 for the same rows.

**``search.list`` is refused unless the caller asks for it by name.** It is the only
expensive endpoint here, and it is also the most convenient one, so it is guarded by
``allow_search`` rather than by remembering.

**Ids are batched fifty at a time.** ``videos.list`` charges per call, so fifty ids
in one call cost 1 unit and fifty calls cost 50.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator, Sequence

import requests

from keel_crawler.youtube.quota import QuotaLedger

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"
MAX_IDS_PER_CALL = 50


class YouTubeApiError(RuntimeError):
    """A non-2xx answer from the API, with whatever reason it gave."""


class SearchNotAllowed(RuntimeError):
    """``search.list`` was called without the caller opting into its 100-unit cost."""


def _chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


class YouTubeDataApi:
    """One API key, one budget, one requests session.

    ``proxy_url`` is accepted for symmetry with the rest of the toolkit but is rarely
    needed: this API rations by key, not by address, so a shared egress buys nothing
    and only adds a failure mode.
    """

    def __init__(
        self,
        api_key: str,
        *,
        ledger: QuotaLedger | None = None,
        session: requests.Session | None = None,
        timeout: int = 20,
        proxy_url: str = "",
    ) -> None:
        if not api_key:
            raise ValueError("a YouTube Data API key is required")
        self.api_key = api_key
        self.ledger = ledger or QuotaLedger()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """One charged call. The ledger is charged first, so a refusal costs nothing."""
        self.ledger.charge(endpoint)
        resource = endpoint.split(".", 1)[0]
        query = {**params, "key": self.api_key}
        response = self.session.get(
            f"{API_ROOT}/{resource}",
            params=query,
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if response.status_code != 200:
            reason = ""
            try:
                reason = response.json().get("error", {}).get("message", "")
            except ValueError:
                reason = response.text[:200]
            raise YouTubeApiError(f"{endpoint} -> HTTP {response.status_code}: {reason}")
        return response.json()

    def channels(
        self,
        *,
        ids: Iterable[str] = (),
        handles: Iterable[str] = (),
        parts: Sequence[str] = ("snippet", "statistics", "contentDetails"),
    ) -> list[dict[str, Any]]:
        """Channel rows by id (batched) or by @handle (one call each, as the API demands)."""
        found: list[dict[str, Any]] = []
        id_list = [value for value in ids if value]
        for batch in _chunked(id_list, MAX_IDS_PER_CALL):
            payload = self._get(
                "channels.list", {"part": ",".join(parts), "id": ",".join(batch), "maxResults": 50}
            )
            found.extend(payload.get("items", []))
        for handle in handles:
            if not handle:
                continue
            payload = self._get(
                "channels.list",
                {"part": ",".join(parts), "forHandle": handle.lstrip("@"), "maxResults": 1},
            )
            found.extend(payload.get("items", []))
        return found

    @staticmethod
    def uploads_playlist_id(channel: dict[str, Any]) -> str:
        """The uploads playlist of a channel row fetched with ``contentDetails``."""
        return (
            channel.get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads", "")
        )

    def playlist_items(
        self, playlist_id: str, *, max_pages: int = 1, page_size: int = 50
    ) -> list[dict[str, Any]]:
        """Newest-first playlist rows. One page is one unit and holds fifty videos."""
        items: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": min(50, page_size),
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get("playlistItems.list", params)
            items.extend(payload.get("items", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                break
        return items

    def videos(
        self,
        video_ids: Sequence[str],
        *,
        parts: Sequence[str] = ("snippet", "statistics", "contentDetails"),
    ) -> list[dict[str, Any]]:
        """Full rows for up to any number of ids, fifty per charged call."""
        found: list[dict[str, Any]] = []
        for batch in _chunked([v for v in video_ids if v], MAX_IDS_PER_CALL):
            payload = self._get(
                "videos.list",
                {"part": ",".join(parts), "id": ",".join(batch), "maxResults": 50},
            )
            found.extend(payload.get("items", []))
        return found

    def comment_threads(
        self,
        video_id: str,
        *,
        max_pages: int = 1,
        order: str = "relevance",
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Top-level comments on one video, newest or most relevant first.

        Comments are disabled on some videos and the API answers 403 for those; that
        is a property of the video and not a failure of the run, so the caller gets
        an empty list rather than an exception.
        """
        threads: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {
                "part": "snippet",
                "videoId": video_id,
                "order": order,
                "maxResults": min(100, page_size),
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = self._get("commentThreads.list", params)
            except YouTubeApiError as exc:
                if "disabled comments" in str(exc) or "403" in str(exc):
                    logger.info("comments unavailable on %s: %s", video_id, exc)
                    return threads
                raise
            threads.extend(payload.get("items", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                break
        return threads

    def search(
        self,
        query: str,
        *,
        allow_search: bool = False,
        kind: str = "channel",
        max_results: int = 25,
        region_code: str = "",
        relevance_language: str = "",
    ) -> list[dict[str, Any]]:
        """Discovery only, and only when the caller has agreed to pay 100 units."""
        if not allow_search:
            raise SearchNotAllowed(
                "search.list costs 100 units (a hundred uploads pages); "
                "pass allow_search=True to spend it deliberately"
            )
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": kind,
            "maxResults": min(50, max_results),
        }
        if region_code:
            params["regionCode"] = region_code
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        payload = self._get("search.list", params)
        return payload.get("items", [])
