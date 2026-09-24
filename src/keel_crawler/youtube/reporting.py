"""The YouTube Reporting API: bulk CSV reports, for metrics Analytics can't give.

Analytics answers a query in real time over any date range; Reporting instead
runs a standing **job** that Google fills with one CSV file per day, on its
own schedule, for whatever window has already elapsed. The trade is latency
for coverage -- some dimensions (thumbnail impressions among them) only ever
appear in a bulk report, never in an Analytics query.

Business-blind, like the rest of this layer: it names report types and column
shapes, never what a host does with the numbers.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import datetime
from typing import Any

import requests

from keel_crawler.youtube.oauth import OAuthCredentials

logger = logging.getLogger(__name__)

REPORTING_ROOT = "https://youtubereporting.googleapis.com/v1"

# Daily channel reach: thumbnail impressions and their click-through rate, by day
# and video -- the one number the Analytics API has no query dimension for.
# Dimensions: date, channel_id, video_id.
# Metrics: video_thumbnail_impressions, video_thumbnail_impressions_ctr.
REACH_BASIC_REPORT = "channel_reach_basic_a1"


class YouTubeReportingError(RuntimeError):
    """A non-2xx answer from the Reporting API, with whatever reason it gave."""


class YouTubeReportingApi:
    """A standing report job, and the files it has produced so far.

    A freshly created job produces no report immediately -- Google backfills it
    starting roughly two days after creation, and from then on each report
    covers exactly one day. A host that just called ``ensure_job`` should not
    expect ``list_reports`` to return anything until the run after next.
    """

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

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.access_token()}"}

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method, url, headers=self._headers(), timeout=self.timeout, **kwargs
        )
        if response.status_code // 100 != 2:
            reason = ""
            try:
                reason = response.json().get("error", {}).get("message", "")
            except ValueError:
                reason = response.text[:200]
            raise YouTubeReportingError(f"{method} {url} -> HTTP {response.status_code}: {reason}")
        return response

    def ensure_job(self, report_type_id: str, name: str) -> str:
        """The id of the standing job for ``report_type_id``, creating it once."""
        listing = self._request("GET", f"{REPORTING_ROOT}/jobs").json()
        for job in listing.get("jobs", []) or []:
            if job.get("reportTypeId") == report_type_id:
                return job["id"]
        created = self._request(
            "POST",
            f"{REPORTING_ROOT}/jobs",
            json={"reportTypeId": report_type_id, "name": name},
        ).json()
        return created["id"]

    def list_reports(self, job_id: str, created_after: datetime | None = None) -> list[dict[str, Any]]:
        """Every report file the job has produced, newest metadata included.

        Each entry carries ``id``, ``startTime``, ``endTime``, ``createTime`` and
        ``downloadUrl`` -- everything ``download_rows`` and a host's own
        already-applied bookkeeping (``ChannelReportFile`` on the Django side)
        need, with no separate lookup.
        """
        reports: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        if created_after is not None:
            params["createdAfter"] = created_after.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        page_token = ""
        while True:
            if page_token:
                params["pageToken"] = page_token
            payload = self._request(
                "GET", f"{REPORTING_ROOT}/jobs/{job_id}/reports", params=params
            ).json()
            reports.extend(payload.get("reports", []) or [])
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                break
        return reports

    def download_rows(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """One report file, decoded from its download URL into plain dict rows."""
        download_url = report.get("downloadUrl", "")
        if not download_url:
            raise YouTubeReportingError(f"report {report.get('id')} has no downloadUrl yet")
        response = self._request("GET", download_url)
        body = response.content
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        text = body.decode("utf-8")
        return list(csv.DictReader(io.StringIO(text)))
