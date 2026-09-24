"""The YouTube Analytics API: per-video performance, owner-only.

Where ``api.py`` reads what anyone can see on a video's public page (view
count, comments), this module reads what only the channel owner can see --
watch time, retention, where a view came from, subscriber deltas. It is
OAuth-gated (see ``oauth.py``) rather than key-gated, and it has no daily
quota like the Data API's; Google throttles it by rate rather than by budget.

Business-blind: it returns rows shaped by the report's own column headers and
knows nothing about episodes, lanes or checkpoints. A host turns these rows
into whatever it tracks.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Sequence

import requests

from keel_crawler.youtube.oauth import OAuthCredentials

logger = logging.getLogger(__name__)

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"

VIDEO_DAILY_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "subscribersGained,subscribersLost,likes,comments,shares"
)
RETENTION_METRICS = "audienceWatchRatio,relativeRetentionPerformance"
TRAFFIC_SOURCE_METRICS = "views,estimatedMinutesWatched"


class YouTubeAnalyticsError(RuntimeError):
    """A non-2xx answer from the Analytics API, with whatever reason it gave."""


class YouTubeAnalyticsApi:
    """One channel's own analytics (``ids=channel==MINE``), read report by report."""

    def __init__(
        self,
        credentials: OAuthCredentials,
        *,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self.credentials = credentials
        self.session = session or requests.Session()
        self.timeout = timeout

    def query(
        self,
        start: date,
        end: date,
        metrics: str,
        *,
        dimensions: str | None = None,
        filters: str | None = None,
        sort: str | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        """One report, turned into a list of dicts keyed by its own column names.

        The API answers ``columnHeaders`` (a name per column) and ``rows`` (parallel
        arrays); this is the one place that zips them back together so every helper
        below reads a plain dict instead of a positional row.
        """
        params: dict[str, Any] = {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": metrics,
        }
        if dimensions:
            params["dimensions"] = dimensions
        if filters:
            params["filters"] = filters
        if sort:
            params["sort"] = sort
        if max_results:
            params["maxResults"] = max_results
        payload = self._get(params)
        headers = [column["name"] for column in payload.get("columnHeaders", [])]
        rows = payload.get("rows", []) or []
        return [dict(zip(headers, row)) for row in rows]

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        token = self.credentials.access_token()
        response = self.session.get(
            REPORTS_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            reason = ""
            try:
                reason = response.json().get("error", {}).get("message", "")
            except ValueError:
                reason = response.text[:200]
            raise YouTubeAnalyticsError(f"reports -> HTTP {response.status_code}: {reason}")
        return response.json()

    def video_daily(self, video_id: str, start: date, end: date) -> list[dict[str, Any]]:
        """One row per day: views, watch time, retention shape, subs and engagement."""
        rows = self.query(
            start,
            end,
            VIDEO_DAILY_METRICS,
            dimensions="day",
            filters=f"video=={video_id}",
            sort="day",
        )
        for row in rows:
            day_value = row.get("day")
            if isinstance(day_value, str):
                row["day"] = date.fromisoformat(day_value)
        return rows

    def retention_curve(self, video_id: str, start: date, end: date) -> list[dict[str, Any]]:
        """The audience-retention curve, one point per elapsed-time ratio.

        ``video_id`` is a single id on purpose: the retention report refuses a
        filter naming more than one video, unlike the other two helpers here.
        """
        return self.query(
            start,
            end,
            RETENTION_METRICS,
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}",
            sort="elapsedVideoTimeRatio",
        )

    def traffic_sources(self, video_id: str, start: date, end: date) -> list[dict[str, Any]]:
        """Views and watch time broken down by how the viewer arrived."""
        return self.query(
            start,
            end,
            TRAFFIC_SOURCE_METRICS,
            dimensions="insightTrafficSourceType",
            filters=f"video=={video_id}",
        )
