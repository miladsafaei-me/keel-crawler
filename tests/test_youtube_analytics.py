"""Tests for the OAuth-gated half of keel_crawler.youtube: oauth, analytics,
reporting, and the standalone consent script.

Plain unittest, no Django and no network, following the same FakeSession/FakeResponse
style as test_youtube.py.

Run: python -m unittest tests.test_youtube_analytics
"""
import io
import json
import unittest
from datetime import date, datetime
from unittest import mock

from keel_crawler.youtube import (
    OAuthCredentials,
    OAuthError,
    REACH_BASIC_REPORT,
    YouTubeAnalyticsApi,
    YouTubeAnalyticsError,
    YouTubeReportingApi,
    YouTubeReportingError,
)
from keel_crawler.youtube import consent as consent_module


class FakeResponse:
    def __init__(self, payload, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = content or self.text.encode("utf-8")

    def json(self):
        return self._payload


class FakeSession:
    """Records every call and answers from a queue of (method, response) results."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            return FakeResponse({}, status_code=500)
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)


class OAuthCredentialsTests(unittest.TestCase):
    def test_missing_arguments_are_refused_up_front(self):
        with self.assertRaises(ValueError):
            OAuthCredentials(client_id="", client_secret="s", refresh_token="r")

    def test_a_fresh_token_is_fetched_once(self):
        session = FakeSession([FakeResponse({"access_token": "tok1", "expires_in": 3600})])
        creds = OAuthCredentials("id", "secret", "refresh", session=session)
        self.assertEqual(creds.access_token(), "tok1")
        self.assertEqual(creds.access_token(), "tok1")
        self.assertEqual(len(session.calls), 1)

    def test_a_stale_token_is_refreshed_again(self):
        session = FakeSession(
            [
                FakeResponse({"access_token": "tok1", "expires_in": 0}),
                FakeResponse({"access_token": "tok2", "expires_in": 3600}),
            ]
        )
        creds = OAuthCredentials("id", "secret", "refresh", session=session)
        self.assertEqual(creds.access_token(), "tok1")
        self.assertEqual(creds.access_token(), "tok2")
        self.assertEqual(len(session.calls), 2)

    def test_invalid_grant_explains_the_testing_mode_trap(self):
        session = FakeSession(
            [FakeResponse({"error": "invalid_grant", "error_description": "Token has been expired"}, status_code=400)]
        )
        creds = OAuthCredentials("id", "secret", "refresh", session=session)
        with self.assertRaises(OAuthError) as ctx:
            creds.access_token()
        message = str(ctx.exception)
        self.assertIn("expired or was revoked", message)
        self.assertIn("Testing", message)
        self.assertIn("In production", message)

    def test_a_different_failure_still_raises_with_its_own_reason(self):
        session = FakeSession([FakeResponse({"error": "invalid_client"}, status_code=401)])
        creds = OAuthCredentials("id", "secret", "refresh", session=session)
        with self.assertRaises(OAuthError):
            creds.access_token()


def _credentials():
    session = FakeSession([FakeResponse({"access_token": "tok", "expires_in": 3600})])
    return OAuthCredentials("id", "secret", "refresh", session=session)


class YouTubeAnalyticsApiTests(unittest.TestCase):
    def test_query_zips_column_headers_onto_rows(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "columnHeaders": [{"name": "day"}, {"name": "views"}],
                        "rows": [["2026-09-01", 100], ["2026-09-02", 150]],
                    }
                )
            ]
        )
        api = YouTubeAnalyticsApi(_credentials(), session=session)
        rows = api.query(date(2026, 9, 1), date(2026, 9, 2), "views", dimensions="day")
        self.assertEqual(rows, [{"day": "2026-09-01", "views": 100}, {"day": "2026-09-02", "views": 150}])

    def test_video_daily_parses_day_into_a_date_and_filters_by_video(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "columnHeaders": [{"name": "day"}, {"name": "views"}],
                        "rows": [["2026-09-01", 42]],
                    }
                )
            ]
        )
        api = YouTubeAnalyticsApi(_credentials(), session=session)
        rows = api.video_daily("vid123", date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(rows[0]["day"], date(2026, 9, 1))
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"]["filters"], "video==vid123")
        self.assertEqual(kwargs["params"]["dimensions"], "day")

    def test_retention_curve_uses_elapsed_ratio_dimension(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "columnHeaders": [
                            {"name": "elapsedVideoTimeRatio"},
                            {"name": "audienceWatchRatio"},
                            {"name": "relativeRetentionPerformance"},
                        ],
                        "rows": [[0.1, 0.95, 1.1]],
                    }
                )
            ]
        )
        api = YouTubeAnalyticsApi(_credentials(), session=session)
        rows = api.retention_curve("vid123", date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(len(rows), 1)
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"]["dimensions"], "elapsedVideoTimeRatio")
        self.assertEqual(kwargs["params"]["filters"], "video==vid123")

    def test_traffic_sources_uses_the_traffic_dimension(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "columnHeaders": [
                            {"name": "insightTrafficSourceType"},
                            {"name": "views"},
                            {"name": "estimatedMinutesWatched"},
                        ],
                        "rows": [["SUGGESTED", 10, 5]],
                    }
                )
            ]
        )
        api = YouTubeAnalyticsApi(_credentials(), session=session)
        rows = api.traffic_sources("vid123", date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(rows[0]["insightTrafficSourceType"], "SUGGESTED")

    def test_a_failed_call_raises_with_googles_reason(self):
        session = FakeSession([FakeResponse({"error": {"message": "boom"}}, status_code=403)])
        api = YouTubeAnalyticsApi(_credentials(), session=session)
        with self.assertRaises(YouTubeAnalyticsError):
            api.query(date(2026, 9, 1), date(2026, 9, 1), "views")


class YouTubeReportingApiTests(unittest.TestCase):
    def test_ensure_job_reuses_an_existing_job(self):
        session = FakeSession(
            [FakeResponse({"jobs": [{"id": "job1", "reportTypeId": REACH_BASIC_REPORT}]})]
        )
        api = YouTubeReportingApi(_credentials(), session=session)
        self.assertEqual(api.ensure_job(REACH_BASIC_REPORT, "reach"), "job1")
        self.assertEqual(len(session.calls), 1)

    def test_ensure_job_creates_one_when_absent(self):
        session = FakeSession(
            [
                FakeResponse({"jobs": []}),
                FakeResponse({"id": "job2", "reportTypeId": REACH_BASIC_REPORT}),
            ]
        )
        api = YouTubeReportingApi(_credentials(), session=session)
        self.assertEqual(api.ensure_job(REACH_BASIC_REPORT, "reach"), "job2")
        self.assertEqual(len(session.calls), 2)

    def test_list_reports_follows_pagination(self):
        session = FakeSession(
            [
                FakeResponse({"reports": [{"id": "r1"}], "nextPageToken": "p2"}),
                FakeResponse({"reports": [{"id": "r2"}]}),
            ]
        )
        api = YouTubeReportingApi(_credentials(), session=session)
        reports = api.list_reports("job1")
        self.assertEqual([r["id"] for r in reports], ["r1", "r2"])
        self.assertEqual(len(session.calls), 2)

    def test_list_reports_passes_created_after(self):
        session = FakeSession([FakeResponse({"reports": []})])
        api = YouTubeReportingApi(_credentials(), session=session)
        api.list_reports("job1", created_after=datetime(2026, 9, 1, 0, 0, 0))
        _, _, kwargs = session.calls[0]
        self.assertIn("createdAfter", kwargs["params"])

    def test_download_rows_parses_plain_csv(self):
        csv_body = b"date,views\n2026-09-01,100\n2026-09-02,150\n"
        session = FakeSession([FakeResponse(None, content=csv_body)])
        api = YouTubeReportingApi(_credentials(), session=session)
        rows = api.download_rows({"id": "r1", "downloadUrl": "https://example/dl"})
        self.assertEqual(rows, [{"date": "2026-09-01", "views": "100"}, {"date": "2026-09-02", "views": "150"}])

    def test_download_rows_handles_gzip(self):
        import gzip

        raw = b"date,views\n2026-09-01,7\n"
        session = FakeSession([FakeResponse(None, content=gzip.compress(raw))])
        api = YouTubeReportingApi(_credentials(), session=session)
        rows = api.download_rows({"id": "r1", "downloadUrl": "https://example/dl"})
        self.assertEqual(rows, [{"date": "2026-09-01", "views": "7"}])

    def test_download_rows_refuses_a_report_with_no_url_yet(self):
        api = YouTubeReportingApi(_credentials(), session=FakeSession([]))
        with self.assertRaises(YouTubeReportingError):
            api.download_rows({"id": "r1"})

    def test_a_failed_call_raises(self):
        session = FakeSession([FakeResponse({"error": {"message": "nope"}}, status_code=500)])
        api = YouTubeReportingApi(_credentials(), session=session)
        with self.assertRaises(YouTubeReportingError):
            api.ensure_job(REACH_BASIC_REPORT, "reach")


class ConsentScriptTests(unittest.TestCase):
    def test_write_env_replaces_only_its_own_four_keys(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("SOME_OTHER_KEY=keep-me\nYOUTUBE_OAUTH_CLIENT_ID=stale\n")
            path = handle.name
        try:
            consent_module._write_env(
                path,
                {
                    "YOUTUBE_OAUTH_CLIENT_ID": "id",
                    "YOUTUBE_OAUTH_CLIENT_SECRET": "secret",
                    "YOUTUBE_OAUTH_REFRESH_TOKEN": "refresh",
                    "YOUTUBE_CHANNEL_ID": "UC123",
                },
            )
            with open(path) as handle:
                written = handle.read()
            self.assertIn("SOME_OTHER_KEY=keep-me", written)
            self.assertIn("YOUTUBE_OAUTH_CLIENT_ID=id", written)
            self.assertNotIn("stale", written)
            self.assertIn("YOUTUBE_CHANNEL_ID=UC123", written)
        finally:
            os.unlink(path)

    def test_run_consent_reports_missing_refresh_token(self):
        with mock.patch.object(consent_module, "_wait_for_code", return_value="code123"), mock.patch.object(
            consent_module, "_post_form", return_value={"access_token": "tok"}
        ), mock.patch.object(consent_module.webbrowser, "open", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                consent_module.run_consent("id", "secret", 8765)
            self.assertIn("refresh_token", str(ctx.exception))

    def test_run_consent_returns_the_four_env_values_and_channel_title(self):
        with mock.patch.object(consent_module, "_wait_for_code", return_value="code123"), mock.patch.object(
            consent_module,
            "_post_form",
            return_value={"access_token": "tok", "refresh_token": "refresh123"},
        ), mock.patch.object(
            consent_module,
            "_get_json",
            return_value={"items": [{"id": "UC123", "snippet": {"title": "My Channel"}}]},
        ), mock.patch.object(
            consent_module.webbrowser, "open", return_value=True
        ):
            result = consent_module.run_consent("id123", "secret123", 8765)
        self.assertEqual(result["YOUTUBE_OAUTH_CLIENT_ID"], "id123")
        self.assertEqual(result["YOUTUBE_OAUTH_REFRESH_TOKEN"], "refresh123")
        self.assertEqual(result["YOUTUBE_CHANNEL_ID"], "UC123")
        self.assertEqual(result["_channel_title"], "My Channel")


if __name__ == "__main__":
    unittest.main()
