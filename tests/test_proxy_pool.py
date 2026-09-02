"""Tests for keel_crawler.proxy — the public-proxy store, its ageing, and rotation.

Plain unittest, no Django and no network: sources are parsed from fixed strings
and the store is pointed at a temporary file, so the ageing policy is tested by
writing timestamps rather than by waiting.

Run: python -m unittest tests.test_proxy_pool
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from keel_crawler.proxy import jsonstore
from keel_crawler.proxy.pool import (BLOCK_MEMORY_SECONDS, DEAD, DEAD_MEMORY_SECONDS,
                                     FAILURE_LIMIT, LIVE, STALE_LIVE_SECONDS,
                                     UNVERIFIED, UNVERIFIED_TTL_SECONDS, Budget,
                                     Proxy, ProxyPool, ProxyStore)
from keel_crawler.proxy.sources import SOURCES, parse

DAY = 24 * 3600


class SourceParsingTests(unittest.TestCase):
    """The published lists do not agree on a format, so all of them are handled."""

    def test_plain_ip_port(self):
        self.assertEqual(parse("1.2.3.4:8080\n5.6.7.8:1080"),
                         [("1.2.3.4:8080", ""), ("5.6.7.8:1080", "")])

    def test_scheme_prefixed_lines(self):
        self.assertEqual(parse("socks5://1.2.3.4:1080"), [("1.2.3.4:1080", "")])

    def test_country_annotated_lines_keep_the_country(self):
        self.assertEqual(parse("24.72.215.236:8246:United States"),
                         [("24.72.215.236:8246", "United States")])

    def test_json_shape(self):
        blob = json.dumps({"data": [{"ip": "1.2.3.4", "port": "80", "country": "DE"}]})
        self.assertEqual(parse(blob), [("1.2.3.4:80", "DE")])

    def test_junk_and_hostnames_are_ignored(self):
        self.assertEqual(parse("# comment\n\nnot-an-ip:80\nexample.com:8080\n1.2.3.4:80"),
                         [("1.2.3.4:80", "")])

    def test_no_single_publisher_can_take_the_pool_down(self):
        publishers = {s.publisher for s in SOURCES}
        self.assertGreaterEqual(len(publishers), 5,
                                "diversity is the point: one publisher going stale "
                                "must not end a harvest")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ProxyStore(Path(self.dir.name) / "pool.json")

    def write(self, records):
        with jsonstore.locked(self.store.path) as handle:
            handle.write({"version": 1, "proxies": records})

    def read(self):
        with jsonstore.locked(self.store.path) as handle:
            return handle.read().get("proxies", {})

    def record(self, **over):
        now = time.time()
        base = {"kind": "http", "country": "", "publishers": ["a"], "status": UNVERIFIED,
                "first_seen": now, "last_seen": now, "last_checked": 0.0, "last_ok": 0.0,
                "ok": 0, "fail": 0, "consecutive_fail": 0, "blocked": {}}
        base.update(over)
        return base

    def test_a_success_promotes_and_a_run_of_failures_condemns(self):
        self.write({"1.1.1.1:80": self.record(), "2.2.2.2:80": self.record()})
        self.store.record_result({"1.1.1.1:80": True})
        self.assertEqual(self.read()["1.1.1.1:80"]["status"], LIVE)
        for _ in range(FAILURE_LIMIT):
            self.store.record_result({"2.2.2.2:80": False})
        self.assertEqual(self.read()["2.2.2.2:80"]["status"], DEAD)

    def test_one_failure_does_not_condemn(self):
        self.write({"1.1.1.1:80": self.record()})
        self.store.record_result({"1.1.1.1:80": False})
        self.assertNotEqual(self.read()["1.1.1.1:80"]["status"], DEAD,
                            "free proxies are erratic; one timeout is not death")

    def test_a_success_clears_an_earlier_run_of_failures(self):
        self.write({"1.1.1.1:80": self.record()})
        for _ in range(FAILURE_LIMIT - 1):
            self.store.record_result({"1.1.1.1:80": False})
        self.store.record_result({"1.1.1.1:80": True})
        self.assertEqual(self.read()["1.1.1.1:80"]["consecutive_fail"], 0)

    def test_a_target_block_is_recorded_against_that_target_only(self):
        self.write({"1.1.1.1:80": self.record(status=LIVE, last_ok=time.time())})
        self.store.record_blocked("1.1.1.1:80", "google.com")
        blocked = self.read()["1.1.1.1:80"]["blocked"]
        self.assertIn("google.com", blocked)
        self.assertNotIn("example.com", blocked,
                         "a refusal by one site says nothing about another")


class AgeingTests(StoreTests):
    """The rules that stop the store becoming a junk drawer."""

    def test_a_dead_address_is_kept_briefly_then_forgotten(self):
        now = time.time()
        self.write({
            "fresh:80": self.record(status=DEAD, last_checked=now - 60),
            "old:80": self.record(status=DEAD, last_checked=now - DEAD_MEMORY_SECONDS - 60),
        })
        self.store.record_result({})
        kept = self.read()
        self.assertIn("fresh:80", kept, "a recent corpse is a negative cache, not junk")
        self.assertNotIn("old:80", kept)

    def test_an_address_that_never_answered_is_dropped_eventually(self):
        now = time.time()
        self.write({
            "new:80": self.record(first_seen=now - 60),
            "stale:80": self.record(first_seen=now - UNVERIFIED_TTL_SECONDS - 60),
        })
        self.store.record_result({})
        kept = self.read()
        self.assertIn("new:80", kept)
        self.assertNotIn("stale:80", kept)

    def test_a_once_good_address_is_retired_when_it_stops_answering(self):
        now = time.time()
        self.write({
            "recent:80": self.record(status=LIVE, last_ok=now - 60),
            "forgotten:80": self.record(status=LIVE, last_ok=now - STALE_LIVE_SECONDS - 60),
        })
        self.store.record_result({})
        kept = self.read()
        self.assertIn("recent:80", kept)
        self.assertNotIn("forgotten:80", kept)

    def test_an_expired_block_is_forgotten_so_the_site_can_be_tried_again(self):
        now = time.time()
        self.write({"1.1.1.1:80": self.record(
            status=LIVE, last_ok=now,
            blocked={"old.com": now - BLOCK_MEMORY_SECONDS - 60, "new.com": now - 60})})
        self.store.record_result({})
        blocked = self.read()["1.1.1.1:80"]["blocked"]
        self.assertIn("new.com", blocked)
        self.assertNotIn("old.com", blocked)

    def test_the_file_cannot_grow_past_its_ceiling(self):
        now = time.time()
        store = ProxyStore(Path(self.dir.name) / "capped.json", max_records=10)
        records = {f"10.0.0.{i}:80": self.record(status=LIVE, last_ok=now, ok=i)
                   for i in range(50)}
        with jsonstore.locked(store.path) as handle:
            handle.write({"version": 1, "proxies": store.prune(records)})
        with jsonstore.locked(store.path) as handle:
            kept = handle.read()["proxies"]
        self.assertEqual(len(kept), 10)
        self.assertIn("10.0.0.49:80", kept, "the most successful must survive the cap")

    def test_pruning_keeps_the_useful_and_drops_the_useless_in_one_pass(self):
        now = time.time()
        self.write({
            "good:80": self.record(status=LIVE, last_ok=now),
            "corpse:80": self.record(status=DEAD, last_checked=now - DEAD_MEMORY_SECONDS - 1),
            "neverwas:80": self.record(first_seen=now - UNVERIFIED_TTL_SECONDS - 1),
        })
        self.store.record_result({})
        self.assertEqual(set(self.read()), {"good:80"})


class CandidateOrderTests(StoreTests):
    def test_known_good_addresses_are_offered_before_unverified_ones(self):
        now = time.time()
        self.write({
            "unverified:80": self.record(publishers=["a", "b", "c"]),
            "known:80": self.record(status=LIVE, last_ok=now, publishers=["a"]),
        })
        order = [p.addr for p in self.store.candidates()]
        self.assertEqual(order[0], "known:80")

    def test_more_publishers_wins_among_unverified_addresses(self):
        self.write({
            "one:80": self.record(publishers=["a"]),
            "three:80": self.record(publishers=["a", "b", "c"]),
        })
        self.assertEqual(self.store.candidates()[0].addr, "three:80")

    def test_a_recent_corpse_is_not_offered_again(self):
        self.write({"dead:80": self.record(status=DEAD, last_checked=time.time())})
        self.assertEqual(self.store.candidates(), [])


class RotationTests(unittest.TestCase):
    """Many addresses at once, each of them gently."""

    def pool(self, n=4, **kw):
        opts = {"rps": 1000.0, "per_minute": 0, "per_hour": 0}
        opts.update(kw)
        return ProxyPool(live=[Proxy(f"10.0.0.{i}:8080", "http") for i in range(n)], **opts)

    def test_socks_addresses_resolve_dns_at_the_proxy(self):
        self.assertTrue(Proxy("1.2.3.4:1080", "socks5").url.startswith("socks5h://"))
        self.assertTrue(Proxy("1.2.3.4:8080", "http").url.startswith("http://"))

    def test_work_spreads_over_the_pool(self):
        pool = self.pool(4)
        seen = []
        for _ in range(4):
            proxy = pool.acquire()
            seen.append(proxy.addr)
            pool.release(proxy)
        self.assertEqual(len(set(seen)), 4)

    def test_an_address_is_never_handed_out_twice_at_once(self):
        pool = self.pool(2)
        first, second = pool.acquire(), pool.acquire()
        self.assertNotEqual(first.addr, second.addr)
        self.assertIsNone(pool.acquire(max_wait=0.2))
        pool.release(first)
        self.assertEqual(pool.acquire(max_wait=1.0).addr, first.addr)

    def test_the_per_second_budget_paces_one_address(self):
        pool = self.pool(1, rps=5.0)
        pool.release(pool.acquire())
        started = time.monotonic()
        pool.release(pool.acquire(max_wait=3.0))
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_the_per_minute_cap_stops_an_address_after_its_quota(self):
        pool = self.pool(1, rps=1000.0, per_minute=3)
        for _ in range(3):
            pool.release(pool.acquire(max_wait=1.0))
        self.assertIsNone(pool.acquire(max_wait=0.3))

    def test_the_per_hour_cap_is_enforced_too(self):
        pool = self.pool(1, rps=1000.0, per_minute=0, per_hour=2)
        for _ in range(2):
            pool.release(pool.acquire(max_wait=1.0))
        self.assertIsNone(pool.acquire(max_wait=0.3))

    def test_all_three_budgets_apply_together(self):
        budget = Budget(rps=1000.0, per_minute=2, per_hour=100)
        now = time.monotonic()
        budget.record(now)
        budget.record(now)
        self.assertGreater(budget.ready_at(now), now, "the per-minute cap must bind")

    def test_a_refusal_evicts_the_address_at_once(self):
        pool = self.pool(3)
        victim = pool.acquire()
        pool.release(victim)
        pool.report_blocked(victim)
        self.assertEqual(len(pool), 2)
        self.assertEqual(pool.stats()["blocked_by_target"], 1)

    def test_a_flaky_address_survives_one_failure_but_not_a_run(self):
        pool = self.pool(2)
        flaky = pool.live[0]
        for _ in range(FAILURE_LIMIT - 1):
            pool.report_failure(flaky)
        self.assertEqual(len(pool), 2)
        pool.report_failure(flaky)
        self.assertEqual(len(pool), 1)

    def test_want_zero_means_keep_every_address_that_answers(self):
        """A cap on the pool is a cap on the crawl, so there is no default cap."""
        import inspect

        self.assertEqual(inspect.signature(ProxyPool.build).parameters["want"].default, 0,
                         "the pool must not discard verified addresses by default")

    def test_a_pool_grows_past_any_previous_default(self):
        pool = ProxyPool(live=[], rps=1000.0, per_minute=0, per_hour=0)
        for i in range(200):
            pool.add(Proxy(f"10.1.{i // 256}.{i % 256}:8080", "http"))
        self.assertEqual(len(pool), 200)

    def test_an_empty_pool_reports_a_dead_end_rather_than_hanging(self):
        self.assertIsNone(ProxyPool(live=[]).acquire(max_wait=5.0))

    def test_an_address_added_mid_crawl_serves_the_next_request(self):
        """Verification runs in the background, so the pool grows while in use."""
        pool = ProxyPool(live=[], rps=1000.0, per_minute=0, per_hour=0)
        self.assertTrue(pool.add(Proxy("10.0.0.7:8080", "http")))
        self.assertEqual(pool.acquire(max_wait=1.0).addr, "10.0.0.7:8080")

    def test_the_same_address_is_never_added_twice(self):
        pool = ProxyPool(live=[], rps=1000.0, per_minute=0, per_hour=0)
        self.assertTrue(pool.add(Proxy("10.0.0.7:8080", "http")))
        self.assertFalse(pool.add(Proxy("10.0.0.7:8080", "http")))
        self.assertEqual(len(pool), 1)

    def test_an_empty_pool_waits_while_it_is_still_filling(self):
        """Empty-but-filling is a gap that closes, not a dead end."""
        import threading as _t

        pool = ProxyPool(live=[], rps=1000.0, per_minute=0, per_hour=0)
        pool.filling = True
        _t.Timer(0.3, lambda: pool.add(Proxy("10.0.0.9:8080", "http"))).start()
        got = pool.acquire(max_wait=4.0)
        self.assertIsNotNone(got, "a crawl must not end on a gap that fills itself")
        self.assertEqual(got.addr, "10.0.0.9:8080")

    def test_an_empty_pool_that_has_finished_filling_is_a_dead_end(self):
        pool = ProxyPool(live=[], rps=1000.0, per_minute=0, per_hour=0)
        pool.filling = False
        self.assertIsNone(pool.acquire(max_wait=1.0))

    def test_a_block_reaches_the_store_when_one_is_attached(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProxyStore(Path(tmp) / "pool.json")
            with jsonstore.locked(store.path) as handle:
                handle.write({"version": 1, "proxies": {"10.0.0.0:8080": {
                    "kind": "http", "status": LIVE, "last_ok": time.time(),
                    "first_seen": time.time(), "publishers": ["a"], "blocked": {}}}})
            pool = ProxyPool(live=[Proxy("10.0.0.0:8080", "http")], store=store,
                             target="example.com")
            pool.report_blocked(pool.live[0])
            with jsonstore.locked(store.path) as handle:
                blocked = handle.read()["proxies"]["10.0.0.0:8080"]["blocked"]
            self.assertIn("example.com", blocked)


class JsonStoreTests(unittest.TestCase):
    def test_a_locked_file_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            with jsonstore.locked(path) as handle:
                handle.write({"a": 1})
            with jsonstore.locked(path) as handle:
                self.assertEqual(handle.read(), {"a": 1})

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with jsonstore.locked(path) as handle:
                self.assertEqual(handle.read(), {})

    def test_a_shorter_write_does_not_leave_the_old_tail_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with jsonstore.locked(path) as handle:
                handle.write({"key": "x" * 500})
            with jsonstore.locked(path) as handle:
                handle.write({"key": "y"})
            with jsonstore.locked(path) as handle:
                self.assertEqual(handle.read(), {"key": "y"})


if __name__ == "__main__":
    unittest.main()
