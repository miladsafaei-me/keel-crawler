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
                                     VERIFY_WORKERS_MAX, ensure_file_limit,
                                     FAILURE_LIMIT, LIVE, STALE_LIVE_SECONDS,
                                     UNVERIFIED, UNVERIFIED_TTL_SECONDS, Budget,
                                     Proxy, ProxyPool, ProxyStore)
from keel_crawler.proxy import pool as pool_module
from keel_crawler.proxy import sources
from keel_crawler.proxy.sources import SOURCES, parse

DAY = 24 * 3600


class SourceParsingTests(unittest.TestCase):
    """The published lists do not agree on a format, so all of them are handled."""

    def test_plain_ip_port(self):
        self.assertEqual(parse("1.2.3.4:8080\n5.6.7.8:1080"),
                         [("1.2.3.4:8080", "", "http"), ("5.6.7.8:1080", "", "http")])

    def test_scheme_prefixed_lines(self):
        self.assertEqual(parse("socks5://1.2.3.4:1080"), [("1.2.3.4:1080", "", "socks5")])

    def test_a_line_naming_its_protocol_overrides_the_source_default(self):
        """A "mixed" list carries all three, and the line is the only honest say."""
        rows = parse("socks5://1.2.3.4:1080\n9.9.9.9:80", default_kind="http")
        self.assertEqual([row.kind for row in rows], ["socks5", "http"])

    def test_a_source_default_applies_where_the_line_is_silent(self):
        self.assertEqual(parse("1.2.3.4:1080", default_kind="socks4")[0].kind, "socks4")

    def test_country_annotated_lines_keep_the_country(self):
        self.assertEqual(parse("24.72.215.236:8246:United States"),
                         [("24.72.215.236:8246", "United States", "http")])

    def test_json_shape(self):
        blob = json.dumps({"data": [{"ip": "1.2.3.4", "port": "80", "country": "DE"}]})
        self.assertEqual(parse(blob), [("1.2.3.4:80", "DE", "http")])

    def test_json_rows_can_name_their_own_protocol(self):
        blob = json.dumps({"data": [{"ip": "1.2.3.4", "port": "80",
                                     "country": "DE", "protocols": ["socks5"]}]})
        self.assertEqual(parse(blob)[0].kind, "socks5")

    def test_an_html_table_of_addresses_is_read(self):
        """Several sites publish nothing but a server-rendered table."""
        html = ("<table><tr><td>1.2.3.4</td><td>8080</td><td>US</td></tr>"
                "<tr><td>5.6.7.8</td><td>3128</td><td>DE</td></tr></table>")
        self.assertEqual(parse(html),
                         [("1.2.3.4:8080", "US", "http"), ("5.6.7.8:3128", "DE", "http")])

    def test_a_page_that_prints_the_pair_in_one_cell_is_still_read(self):
        """Table-shaped first, then a plain scan — a page with addresses on it
        that yields nothing is a worse failure than a few junk matches, which
        the address pattern rejects anyway."""
        html = "<!DOCTYPE html><html><body><div>1.2.3.4:8080</div></body></html>"
        self.assertEqual(parse(html), [("1.2.3.4:8080", "", "http")])

    def test_junk_and_hostnames_are_ignored(self):
        self.assertEqual(parse("# comment\n\nnot-an-ip:80\nexample.com:8080\n1.2.3.4:80"),
                         [("1.2.3.4:80", "", "http")])

    def test_no_single_publisher_can_take_the_pool_down(self):
        publishers = {s.publisher for s in SOURCES}
        self.assertGreaterEqual(len(publishers), 5,
                                "diversity is the point: one publisher going stale "
                                "must not end a harvest")

    def test_the_publishers_that_stopped_committing_are_gone(self):
        """A list that still downloads is not a list that is still maintained.

        jetkai stopped committing in April 2023 and prxchk in April 2024, and
        between them they were still serving 2,056 addresses no other source
        had, 2.8% of which would accept a TCP connection.
        """
        publishers = {s.publisher for s in SOURCES}
        self.assertNotIn("jetkai", publishers)
        self.assertNotIn("prxchk", publishers)

    def test_every_source_is_named_and_addressed_once(self):
        names = [s.name for s in SOURCES]
        urls = [s.url for s in SOURCES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(urls), len(set(urls)))


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
        self.assertTrue(Proxy("1.2.3.4:1080", "socks4").url.startswith("socks4a://"))
        self.assertTrue(Proxy("1.2.3.4:8080", "http").url.startswith("http://"))

    def test_an_unknown_protocol_is_spoken_to_as_http(self):
        """Which is what a list that says nothing about protocol means."""
        self.assertTrue(Proxy("1.2.3.4:8080", "gopher").url.startswith("http://"))

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


class DecodingPublishedLists(unittest.TestCase):
    """A country name must survive the wire, or it becomes its own country."""

    def test_a_windows_1252_country_name_decodes_to_the_real_name(self):
        raw = "1.2.3.4:8080:T\u00fcrkiye".encode("cp1252")
        self.assertEqual(sources.decode(raw), "1.2.3.4:8080:T\u00fcrkiye")

    def test_utf8_is_still_preferred_over_the_fallback(self):
        raw = "1.2.3.4:8080:T\u00fcrkiye".encode("utf-8")
        self.assertEqual(sources.decode(raw), "1.2.3.4:8080:T\u00fcrkiye")

    def test_the_old_replacement_behaviour_would_have_lost_the_country(self):
        raw = "T\u00fcrkiye".encode("cp1252")
        mangled = raw.decode("utf-8", "replace")
        self.assertEqual(sources.normalize_country(mangled), "")
        self.assertEqual(sources.normalize_country(sources.decode(raw)), "TR")


class CountryNamesTheListsActuallyPublish(unittest.TestCase):
    def test_names_that_were_silently_dropped_now_resolve(self):
        for name, code in (("Seychelles", "SC"), ("Kyrgyzstan", "KG"),
                           ("Afghanistan", "AF"), ("Palestine", "PS"),
                           ("Papua New Guinea", "PG"), ("Zimbabwe", "ZW"),
                           ("British Virgin Islands", "VG"), ("Somalia", "SO"),
                           ("Gabon", "GA"), ("Belize", "BZ")):
            self.assertEqual(sources.normalize_country(name), code, name)

    def test_the_long_iso_forms_resolve_too(self):
        for name, code in (("Korea, Republic of", "KR"),
                           ("Iran (Islamic Republic of)", "IR"),
                           ("Taiwan, Province of China", "TW"),
                           ("United States of America", "US")):
            self.assertEqual(sources.normalize_country(name), code, name)

    def test_an_unknown_name_still_resolves_to_nothing_rather_than_a_guess(self):
        self.assertEqual(sources.normalize_country("Atlantis"), "")


class RepairingALabelAlreadyStored(unittest.TestCase):
    def test_a_refresh_replaces_a_label_that_resolves_to_no_country(self):
        merged = {"1.2.3.4:80": {"addr": "1.2.3.4:80", "kind": "http",
                                 "country": "T\ufffdrkiye", "publishers": set()}}
        entry = merged["1.2.3.4:80"]
        country = "T\u00fcrkiye"
        if country and (not entry["country"]
                        or (not sources.normalize_country(entry["country"])
                            and sources.normalize_country(country))):
            entry["country"] = country
        self.assertEqual(sources.normalize_country(entry["country"]), "TR")

    def test_a_resolvable_label_is_not_overwritten_by_another_resolvable_one(self):
        entry = {"country": "United States"}
        country = "Canada"
        if country and (not entry["country"]
                        or (not sources.normalize_country(entry["country"])
                            and sources.normalize_country(country))):
            entry["country"] = country
        self.assertEqual(entry["country"], "United States")


class ThePackageVersion(unittest.TestCase):
    """It was reporting 0.13.1 from a 0.14.0 install, and nothing failed."""

    def test_it_matches_the_one_file_ci_actually_guards(self):
        import re

        import keel_crawler

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = re.search(r'^version\s*=\s*"([^"]+)"',
                             pyproject.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(declared)
        self.assertEqual(keel_crawler.__version__, declared.group(1))


class TheHostWideHarvestMutex(unittest.TestCase):
    """One spender at a time, because the budgets are enforced in memory."""

    def test_a_second_holder_is_refused_while_the_first_holds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harvest.lock"
            with jsonstore.exclusive(path) as first:
                self.assertTrue(first)
                with jsonstore.exclusive(path, wait=False) as second:
                    self.assertFalse(second)

    def test_it_is_available_again_once_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harvest.lock"
            with jsonstore.exclusive(path):
                pass
            with jsonstore.exclusive(path, wait=False) as got:
                self.assertTrue(got)

    def test_the_default_lock_is_keyed_on_the_shared_store_not_the_caller(self):
        lock = jsonstore.harvest_lock()
        self.assertEqual(lock._path.parent, jsonstore.data_dir())
        self.assertEqual(lock._path.name, "harvest.lock")




class FakeStore:
    """A store that hands out addresses in order and remembers what was asked.

    Enough of ProxyStore for the refill loop: the loop only ever asks for
    candidates, records outcomes, and re-reads the published lists.
    """

    def __init__(self, addrs):
        self.addrs = list(addrs)
        self.recorded = {}
        self.refreshes = 0
        self.asked_exclusions = []

    def candidates(self, limit=1000, kinds=None, exclude=()):
        exclude = set(exclude)
        self.asked_exclusions.append(exclude)
        return [Proxy(a, "http") for a in self.addrs if a not in exclude][:limit]

    def record_result(self, results, target=""):
        self.recorded.update(results)

    def refresh(self, sources=None):
        self.refreshes += 1
        return {"published": 0, "new": 0, "revived": 0, "total": 0}


class RefillTests(unittest.TestCase):
    """A pool that only ever shrinks is a crawl with a deadline nobody set.

    Measured 2026-09-04: one harvest filled to 156 addresses and finished
    thirteen minutes later with 58, because verification was a single pass and
    nothing ever added an address back.
    """

    def setUp(self):
        self.answered = set()
        self.original = pool_module.fetch_through
        pool_module.fetch_through = self.fake_fetch
        self.addCleanup(setattr, pool_module, "fetch_through", self.original)

    def fake_fetch(self, proxy, url, timeout=10.0):
        return (200, "ok") if proxy.addr in self.answered else (0, "")

    def build(self, addrs, answering, **cfg):
        store = FakeStore(addrs)
        self.answered = set(answering)
        pool = ProxyPool(live=[], store=store, target="example.com",
                         rps=1000.0, per_minute=0, per_hour=0)
        pool._verify_cfg = {
            "probe_url": "https://example.com/", "workers": 4, "timeout": 1.0,
            "accept": lambda status, body: status == 200, "want": 0,
            "unlimited": True, "candidates": 100, "progress": None,
            "refill": True, "refill_share": 0.5, "refill_budget": 10_000,
            "refresh_after": 3600.0, "start_at": 1,
        }
        pool._verify_cfg.update(cfg)
        pool._last_refresh = time.monotonic()
        return pool, store

    def test_a_refill_admits_addresses_while_the_crawl_runs(self):
        pool, _ = self.build(["1.1.1.1:80", "2.2.2.2:80"],
                             answering=["1.1.1.1:80", "2.2.2.2:80"])
        self.assertEqual(pool.refill_once(), 2)
        self.assertEqual(len(pool), 2)
        self.assertEqual(pool.refills, 1)

    def test_a_refill_never_re_checks_what_this_run_already_spent_a_check_on(self):
        pool, store = self.build(["1.1.1.1:80", "2.2.2.2:80"], answering=["2.2.2.2:80"])
        pool._tried = {"1.1.1.1:80"}
        pool.refill_once()
        self.assertIn("1.1.1.1:80", store.asked_exclusions[0])
        self.assertNotIn("1.1.1.1:80", store.recorded)

    def test_a_second_refill_excludes_what_the_first_one_checked(self):
        pool, store = self.build(["1.1.1.1:80", "2.2.2.2:80"], answering=[])
        pool.refill_once()
        pool.refill_once()
        self.assertEqual(store.asked_exclusions[-1], {"1.1.1.1:80", "2.2.2.2:80"})

    def test_a_refill_with_nothing_left_to_offer_reports_zero(self):
        pool, _ = self.build([], answering=[])
        self.assertEqual(pool.refill_once(), 0)
        self.assertEqual(pool.refills, 0)

    def test_the_watermark_follows_the_pool_high_water_mark(self):
        pool, _ = self.build([f"10.0.0.{i}:80" for i in range(10)],
                             answering=[f"10.0.0.{i}:80" for i in range(10)])
        pool.refill_once()
        self.assertEqual(len(pool), 10)
        self.assertEqual(pool.refill_at(), 5)

    def test_the_watermark_never_falls_below_the_pool_that_can_start_a_crawl(self):
        pool, _ = self.build(["1.1.1.1:80"], answering=["1.1.1.1:80"], start_at=8)
        pool.refill_once()
        self.assertEqual(pool.refill_at(), 8)

    def test_the_published_lists_are_re_read_only_once_they_are_stale(self):
        pool, store = self.build(["1.1.1.1:80"], answering=[], refresh_after=3600.0)
        pool.refill_once()
        self.assertEqual(store.refreshes, 0)
        pool._last_refresh = time.monotonic() - 3601
        pool._tried.clear()
        pool.refill_once()
        self.assertEqual(store.refreshes, 1)

    def test_every_candidate_checked_is_counted_against_the_run_budget(self):
        pool, _ = self.build(["1.1.1.1:80", "2.2.2.2:80"], answering=[])
        pool.refill_once()
        self.assertEqual(pool.verified_total, 2)
        self.assertEqual(pool.stats()["verified_total"], 2)
        self.assertEqual(pool.stats()["refills"], 1)

    def test_a_pool_built_without_a_store_simply_does_not_refill(self):
        pool = ProxyPool(live=[])
        self.assertEqual(pool.refill_once(), 0)

    def test_maintenance_stops_when_asked(self):
        pool, _ = self.build(["1.1.1.1:80"], answering=["1.1.1.1:80"])
        pool.stop()
        pool._maintain()  # returns immediately rather than looping

    def test_refill_can_be_turned_off_at_build(self):
        import inspect

        signature = inspect.signature(ProxyPool.build).parameters
        self.assertTrue(signature["refill"].default,
                        "a pool that never refills is the bug this replaced")


if __name__ == "__main__":
    unittest.main()


class FileLimitTests(unittest.TestCase):
    """Concurrency here is spent in descriptors, not only in threads.

    Every check and every fetch is a curl subprocess holding two pipes, and the
    pool now verifies while the caller crawls. One harvest died of OSError 24 at
    the line writing its response cache, which is how this failure presents: not
    where the descriptors were spent, but wherever the next open happened to be.
    """

    def test_the_soft_limit_is_raised_toward_the_hard_one(self):
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(256, hard), hard))
        raised = ensure_file_limit(1024)
        self.assertGreaterEqual(raised, min(1024, hard))

    def test_raising_it_never_raises(self):
        """A refused rlimit gives a smaller pool, not a dead crawl."""
        self.assertIsInstance(ensure_file_limit(-1), int)

    def test_the_checker_never_takes_the_whole_worker_budget(self):
        self.assertLessEqual(VERIFY_WORKERS_MAX, 120)
