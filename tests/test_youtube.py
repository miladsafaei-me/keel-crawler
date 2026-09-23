"""Tests for keel_crawler.youtube — the quota ledger, the client's call shapes, the
outlier maths and the suggestion diff.

Plain unittest, no Django and no network: the API client is handed a fake session
that records the requests it would have made, so what is asserted is the thing that
costs money -- which endpoint was called, how many times, and with which ids.

Run: python -m unittest tests.test_youtube
"""
import unittest
from datetime import datetime, timedelta, timezone

from keel_crawler.youtube import (
    QuotaExhausted,
    QuotaLedger,
    SearchNotAllowed,
    YouTubeApiError,
    YouTubeDataApi,
    channel_baseline,
    diff_snapshots,
    early_pace_baseline,
    looks_like_short,
    outlier_multiplier,
    parse_duration_seconds,
    parse_published_at,
    read_velocity,
    views_per_hour,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records every call and answers from a queue of payloads."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, timeout=None, proxies=None):
        self.calls.append((url, dict(params or {})))
        if not self.payloads:
            return FakeResponse({"items": []})
        payload = self.payloads.pop(0)
        if isinstance(payload, int):
            return FakeResponse({"error": {"message": "boom"}}, status_code=payload)
        return FakeResponse(payload)


class QuotaLedgerTests(unittest.TestCase):
    def test_search_costs_a_hundred_times_a_playlist_page(self):
        ledger = QuotaLedger()
        ledger.charge("playlistItems.list")
        self.assertEqual(ledger.spent, 1)
        ledger.charge("search.list")
        self.assertEqual(ledger.spent, 101)

    def test_it_refuses_rather_than_overspends(self):
        ledger = QuotaLedger(limit=5)
        ledger.charge("videos.list", 5)
        self.assertFalse(ledger.can_afford("videos.list"))
        with self.assertRaises(QuotaExhausted):
            ledger.charge("videos.list")

    def test_a_refused_call_is_not_recorded_as_spent(self):
        ledger = QuotaLedger(limit=10)
        with self.assertRaises(QuotaExhausted):
            ledger.charge("search.list")
        self.assertEqual(ledger.spent, 0)

    def test_an_unknown_endpoint_is_an_error_not_a_free_call(self):
        with self.assertRaises(KeyError):
            QuotaLedger().charge("playlists.insert")


class ApiCallShapeTests(unittest.TestCase):
    def test_fifty_ids_are_one_call_and_fifty_one_are_two(self):
        session = FakeSession([{"items": [{"id": "a"}]}, {"items": [{"id": "b"}]}])
        api = YouTubeDataApi("key", session=session)
        api.videos([f"v{i}" for i in range(51)])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(api.ledger.spent, 2)

    def test_search_is_refused_unless_the_caller_opts_in(self):
        api = YouTubeDataApi("key", session=FakeSession([]))
        with self.assertRaises(SearchNotAllowed):
            api.search("prop firm")
        self.assertEqual(api.ledger.spent, 0)

    def test_an_opted_in_search_is_charged_a_hundred(self):
        api = YouTubeDataApi("key", session=FakeSession([{"items": [{"id": {}}]}]))
        api.search("prop firm", allow_search=True)
        self.assertEqual(api.ledger.spent, 100)

    def test_playlist_paging_stops_when_the_api_stops(self):
        session = FakeSession(
            [
                {"items": [{"contentDetails": {"videoId": "a"}}], "nextPageToken": "t2"},
                {"items": [{"contentDetails": {"videoId": "b"}}]},
            ]
        )
        api = YouTubeDataApi("key", session=session)
        items = api.playlist_items("UUxxx", max_pages=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(session.calls), 2)

    def test_disabled_comments_return_empty_rather_than_raising(self):
        api = YouTubeDataApi("key", session=FakeSession([403]))
        self.assertEqual(api.comment_threads("vid"), [])

    def test_a_real_error_still_raises(self):
        api = YouTubeDataApi("key", session=FakeSession([500]))
        with self.assertRaises(YouTubeApiError):
            api.videos(["a"])

    def test_the_key_never_reaches_the_caller_supplied_params(self):
        session = FakeSession([{"items": []}])
        YouTubeDataApi("secret-key", session=session).videos(["a"])
        _, params = session.calls[0]
        self.assertEqual(params["key"], "secret-key")
        self.assertEqual(params["id"], "a")


class VelocityTests(unittest.TestCase):
    def test_duration_parsing_and_the_shorts_hint(self):
        self.assertEqual(parse_duration_seconds("PT4M13S"), 253)
        self.assertEqual(parse_duration_seconds("PT45S"), 45)
        self.assertEqual(parse_duration_seconds(""), 0)
        self.assertTrue(looks_like_short(45))
        self.assertFalse(looks_like_short(253))
        self.assertFalse(looks_like_short(0))

    def test_baseline_is_a_median_so_one_runaway_does_not_raise_the_bar(self):
        counts = [1_000_000, 1_000, 1_100, 900, 1_050]
        self.assertEqual(channel_baseline(counts), 1050.0)

    def test_no_baseline_means_no_multiplier_rather_than_a_huge_one(self):
        self.assertEqual(outlier_multiplier(50_000, 0), 0.0)
        self.assertEqual(outlier_multiplier(50_000, 10_000), 5.0)

    def test_views_per_hour_floors_the_age_so_a_new_video_is_not_infinite(self):
        published = NOW - timedelta(minutes=1)
        self.assertLess(views_per_hour(100, published, now=NOW), 500)

    def test_early_pace_ignores_videos_too_young_or_too_old_to_settle(self):
        samples = [
            (10_000, NOW - timedelta(hours=2)),
            (10_000, NOW - timedelta(days=10)),
            (20_000, NOW - timedelta(days=20)),
            (10_000, NOW - timedelta(days=400)),
        ]
        pace = early_pace_baseline(samples, now=NOW)
        ten_day = views_per_hour(10_000, NOW - timedelta(days=10), now=NOW)
        twenty_day = views_per_hour(20_000, NOW - timedelta(days=20), now=NOW)
        self.assertAlmostEqual(pace, (ten_day + twenty_day) / 2, places=1)

    def test_breaking_needs_both_speed_and_youth(self):
        fast_and_young = read_velocity("a", 6_000, NOW - timedelta(hours=10), 60.0, now=NOW)
        self.assertTrue(fast_and_young.is_breaking)
        fast_and_old = read_velocity("b", 60_000, NOW - timedelta(days=30), 60.0, now=NOW)
        self.assertFalse(fast_and_old.is_breaking)

    def test_published_at_parsing_is_always_aware(self):
        parsed = parse_published_at("2026-09-01T08:30:00Z")
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.hour, 8)


class SuggestDiffTests(unittest.TestCase):
    def test_both_arrivals_and_departures_are_reported(self):
        arrived, left = diff_snapshots(["ftmo review", "ftmo payout"], ["ftmo review", "ftmo scam"])
        self.assertEqual(arrived, ["ftmo scam"])
        self.assertEqual(left, ["ftmo payout"])

    def test_case_and_padding_do_not_invent_movement(self):
        arrived, left = diff_snapshots([" FTMO Review "], ["ftmo review"])
        self.assertEqual(arrived, [])
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
