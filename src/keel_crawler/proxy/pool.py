"""A self-maintaining pool of public proxies: harvest, verify, rotate, forget.

Free proxy lists are mostly dead addresses. Measured on 2026-09-02, of 600
sampled entries 37 answered the target endpoint — but among those actually
alive, two thirds got through, so the lists are not poisoned, merely stale. Any
tool built on them therefore lives or dies on one thing: whether it forgets
faster than it collects.

That is what this module is for. Three pieces, deliberately separate:

:class:`ProxyStore` is the durable half — a lock-guarded JSON file that
remembers what has been seen, what answered, what failed and when. It is the
part that must never grow without bound, so every write prunes (see
:meth:`ProxyStore.prune`), and the ageing rules are stated as constants rather
than buried in conditionals.

:class:`ProxyPool` is the runtime half — rotation over addresses known to work,
with a per-address budget on three timescales so a pool stays usable instead of
burning itself down in one run.

:mod:`keel_crawler.proxy.sources` is where addresses come from: fifty-five lists
across twenty-seven publishers, so no single one going stale stops the work.

**Liveness is per target, not global.** A proxy refused by one site may serve
another perfectly well, so a refusal is recorded against the target that issued
it. Treating a target's block as global would throw away most of a good pool the
first time one site got strict — and because the store is shared between
projects, it would throw away *other* projects' working proxies too.

**Verification asks the real target.** A proxy that cheerfully fetches
``httpbin.org`` may still be refused by the endpoint that matters, and only
asking that endpoint reveals it.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from keel_crawler.proxy.jsonstore import data_dir, locked
from keel_crawler.proxy.sources import (SOURCES, USER_AGENT, fetch_all,
                                        geolocate, normalize_country)

# The ageing policy, stated in one place so it can be read without tracing
# conditionals. Consecutive failed checks before an address is considered dead:
# free proxies are erratic rather than cleanly up or down, so one timeout is not
# evidence.
FAILURE_LIMIT = 3

# How long a dead address is remembered after it dies. This is a negative cache,
# not sentiment: without it every refresh re-imports the same corpses from the
# same lists and re-tests them, which is most of the cost of a refresh.
DEAD_MEMORY_SECONDS = 24 * 3600

# An address that has never answered, and has sat unverified this long, is
# dropped. The lists publish far more than anyone will ever check.
UNVERIFIED_TTL_SECONDS = 3 * 24 * 3600

# A once-good address that has not answered in this long is retired even without
# a run of consecutive failures - it simply stopped being usable.
STALE_LIVE_SECONDS = 7 * 24 * 3600

# How long a target's refusal is honoured before the address is offered to that
# same target again. Blocks observed on the endpoint this was built for lasted
# many hours.
BLOCK_MEMORY_SECONDS = 24 * 3600

# Hard ceiling on stored addresses. Without a cap the file grows every refresh
# forever, which is the failure this module exists to avoid.
MAX_RECORDS = 20_000

# What one address is allowed to do, on three timescales at once. A single global
# rate does not achieve this: as addresses are evicted the survivors absorb the
# whole rate, so the last few inherit exactly the traffic that got the first ones
# blocked.
#
# These numbers are measured, and the first version of them was not. They began
# as 0.2/s and 200/hour, reasoned as "an order of magnitude under the one block
# ever observed" - a guess wearing a number's clothes. A deliberate burn test
# (2026-09-02) put 30 addresses through five rate cohorts, from 0.2/s to
# unthrottled, and pushed 15,553 requests: **not one address was blocked**, at any
# rate, up to 1,500 requests each. The old ceiling was roughly 18x too low.
#
# Two things that test settled. Free proxies self-limit long before Google does:
# the unthrottled cohort achieved 0.18-1.45 req/s, identical to the 2/s cohort,
# because proxy latency - not our throttle and not the endpoint - sets the pace.
# A per-address rate above ~1.5/s is therefore meaningless. And the hourly figure
# is set to exactly the largest volume actually tested per address rather than
# extrapolated, because the test found a floor, not a ceiling: nothing blocked,
# so the true limit is somewhere above this and remains unknown.
# How the pool keeps itself alive once a crawl is under way. Verification used to
# be a single pass: check a batch, admit what answers, and never look again. That
# makes a pool strictly one-way — `report_failure` and `report_blocked` remove
# addresses and nothing adds any — so a long harvest decays for hours. Measured
# 2026-09-04: one run filled to 156 addresses and ended thirteen minutes later
# with 58; a second filled to 600 and spent the following hours shrinking, having
# stopped checking with about 3,000 already-ranked candidates untouched because
# it had reached its `want`.
#
# So the pool watches itself and refills. The trigger is the live count, not
# throughput: throughput is confounded by the caller's own response cache, which
# replays a whole level of work with zero network calls and would read as a
# healthy pool at the exact moment the pool was emptying.
# The checker never takes more than this many threads, however many the
# caller asked for, because it is now running while the crawl is.
VERIFY_WORKERS_MAX = 120
REFILL_CHECK_SECONDS = 15.0
# Refill when the pool falls to this share of its own high-water mark. Relative,
# because `want` is often 0 ("keep everything that answers") and a fixed floor
# would be either meaningless for a pool of 600 or unreachable for a pool of 20.
REFILL_SHARE = 0.5
# A refill pass that finds nothing waits longer before the next one, up to this.
REFILL_BACKOFF_MAX = 900.0
# The published lists commit five to seven times a day, so re-reading them more
# often than this buys nothing; re-reading them never is what leaves a
# twelve-hour run working from the addresses that existed when it started.
REFRESH_AFTER_SECONDS = 7200.0

PER_PROXY_RPS = 1.5
PER_PROXY_PER_MINUTE = 90
PER_PROXY_PER_HOUR = 1500

LIVE = "live"
UNVERIFIED = "unverified"
DEAD = "dead"


# Every check and every fetch through the pool is a curl subprocess holding two
# pipes, so concurrency here is spent in file descriptors as much as in threads:
# 500 workers is already past a 1024 descriptor limit, and the pool now verifies
# while the caller crawls, which stacks the two. The failure is not graceful — it
# surfaces as OSError 24 from whatever unrelated line happens to open a file
# next, which cost one harvest its output at the point it was writing its cache.
# The soft limit is ours to raise (the hard limit on a Linux host is typically
# hundreds of thousands), so raise it rather than rationing workers.
FILE_LIMIT_TARGET = 65536


def ensure_file_limit(target: int = FILE_LIMIT_TARGET) -> int:
    """Raise this process's open-file soft limit toward ``target``.

    Best effort and never raises: the module is POSIX-only, the call can be
    refused, and a pool that cannot raise the limit is still a working pool —
    just a smaller one than the caller asked for. Returns the limit in force.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no rlimits
        return 0
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = min(target, hard) if hard > 0 else target
        if wanted > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
            return wanted
        return soft
    except Exception:  # noqa: BLE001 - a refused rlimit must not end a crawl
        return 0


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class Proxy:
    """One address, and how to speak to it."""

    addr: str
    kind: str = "http"
    country: str = ""

    # socks5h and socks4a, not socks5 and socks4: both spellings resolve DNS at
    # the proxy, so the lookup is neither leaked to nor answered by the local
    # network. Anything unrecognised is spoken to as HTTP, which is what the
    # published lists mean when they say nothing.
    SCHEMES = {"socks5": "socks5h", "socks4": "socks4a"}

    @property
    def url(self) -> str:
        return f"{self.SCHEMES.get(self.kind, 'http')}://{self.addr}"


def fetch_through(proxy: Proxy, url: str, timeout: float = 10.0) -> tuple[int, str]:
    """GET `url` through `proxy`, returning (status, body). Status 0 means no reply.

    Uses curl rather than urllib for two reasons that are not stylistic. It
    speaks SOCKS5 without a Python dependency and without PySocks' process-wide
    default-proxy state, which is unusable from a thread pool. And it takes
    ``--noproxy ""``, which is **required**: with ``NO_PROXY=*`` in the
    environment, urllib and curl both silently ignore an explicit proxy and
    connect directly. That failure is invisible — the caller appears to rotate
    while every request leaves from the one address the pool exists to avoid —
    and it produced three wrong measurements before it was noticed.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), "--noproxy", "",
             "--proxy", proxy.url, "-H", f"User-Agent: {USER_AGENT}",
             "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 6,
        )
    except Exception:  # noqa: BLE001
        return 0, ""
    parts = result.stdout.rsplit("\n", 1)
    if len(parts) != 2:
        return 0, ""
    body, code = parts
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, body


def looks_usable(status: int, body: str) -> bool:
    """Default acceptance test: a 200 carrying something."""
    return status == 200 and bool(body.strip())


class ProxyStore:
    """Durable memory of every address seen, with an ageing policy that runs itself.

    One JSON file, shared by every project on the machine, so an address verified
    for one crawler is already known to the next. Concurrency-safe: each mutation
    is a locked read-modify-write, because the damaging race is two processes
    both reading the old set and each writing their own version of it.
    """

    def __init__(self, path=None, *, max_records: int = MAX_RECORDS):
        self.path = path or (data_dir() / "proxy-pool.json")
        self.max_records = max_records

    def _load(self, handle) -> dict:
        raw = handle.read()
        return raw.get("proxies", {}) if isinstance(raw, dict) else {}

    def _save(self, handle, records: dict) -> None:
        records = self.prune(records)
        handle.write({"version": 1, "updated_at": _now(), "proxies": records})

    def refresh(self, sources=SOURCES) -> dict:
        """Pull every list and merge new addresses in, without losing what is known.

        An address already on file keeps its history — verification state,
        counters, per-target blocks — because re-appearing in a public list is not
        evidence about whether it works, only that it is still published.
        """
        found = fetch_all(sources)
        now = _now()
        added = revived = 0
        with locked(self.path) as handle:
            records = self._load(handle)
            known_before = len(records)
            for addr, entry in found.items():
                record = records.get(addr)
                if record is None:
                    records[addr] = {
                        "kind": entry["kind"], "country": entry.get("country", ""),
                        "publishers": entry["publishers"], "status": UNVERIFIED,
                        "first_seen": now, "last_seen": now, "last_checked": 0.0,
                        "last_ok": 0.0, "ok": 0, "fail": 0, "consecutive_fail": 0,
                        "blocked": {},
                    }
                    added += 1
                else:
                    record["last_seen"] = now
                    record["publishers"] = sorted(
                        set(record.get("publishers", [])) | set(entry["publishers"])
                    )
                    if entry.get("country") and not record.get("country"):
                        record["country"] = entry["country"]
                    # A dead address still being published gets one more chance
                    # once its negative-cache window has passed, rather than
                    # being written off permanently.
                    if (record.get("status") == DEAD
                            and now - record.get("last_checked", 0) > DEAD_MEMORY_SECONDS):
                        record["status"] = UNVERIFIED
                        record["consecutive_fail"] = 0
                        revived += 1
            self._save(handle, records)
        return {"published": len(found), "new": added, "revived": revived,
                "known_before": known_before}

    def candidates(self, limit: int = 1000, kinds=("http", "socks5", "socks4"),
                   exclude=()) -> list:
        """What to check next, best-evidence first.

        Ordering is deliberate. Addresses already known to work come first so a
        crawl can start immediately; then unverified ones, most-published first,
        because several independent lists agreeing is the only quality signal
        available before a check is spent.

        ``exclude`` skips addresses a caller has already spent a check on. A
        refilling pool asks for its next batch repeatedly, and without this it
        would be handed the same best-ranked corpses every time.
        """
        now = _now()
        with locked(self.path) as handle:
            records = self._load(handle)

        def rank(item):
            _, record = item
            status_rank = {LIVE: 0, UNVERIFIED: 1, DEAD: 2}.get(record.get("status"), 3)
            return (status_rank, -len(record.get("publishers", [])), -record.get("last_ok", 0.0))

        exclude = set(exclude)
        out = []
        for addr, record in sorted(records.items(), key=rank):
            if record.get("kind") not in kinds or addr in exclude:
                continue
            if (record.get("status") == DEAD
                    and now - record.get("last_checked", 0) < DEAD_MEMORY_SECONDS):
                continue
            out.append(Proxy(addr, record.get("kind", "http"), record.get("country", "")))
            if len(out) >= limit:
                break
        return out

    def record_result(self, results: dict, target: str = "") -> None:
        """Write back a batch of check outcomes in one locked pass.

        Batched on purpose: a verification run produces hundreds of outcomes, and
        taking the file lock per address would serialise the run on disk.
        """
        now = _now()
        with locked(self.path) as handle:
            records = self._load(handle)
            for addr, ok in results.items():
                record = records.get(addr)
                if record is None:
                    continue
                record["last_checked"] = now
                if ok:
                    record["ok"] = record.get("ok", 0) + 1
                    record["last_ok"] = now
                    record["consecutive_fail"] = 0
                    record["status"] = LIVE
                else:
                    record["fail"] = record.get("fail", 0) + 1
                    record["consecutive_fail"] = record.get("consecutive_fail", 0) + 1
                    if record["consecutive_fail"] >= FAILURE_LIMIT:
                        record["status"] = DEAD
            self._save(handle, records)

    def resolve_countries(self, addrs, progress=None) -> dict:
        """Fill in the country of any address that does not have one yet.

        Only one of the published lists labels country, so most addresses arrive
        without one - and a result that cannot say where it came from is a result
        nobody can interpret, on an endpoint that answers differently per country.
        An address does not move, so this is paid once per address, ever, and read
        from the store thereafter.
        """
        with locked(self.path) as handle:
            records = self._load(handle)
        # Only an ISO code counts as known. A stored full name ("United States")
        # is re-resolved rather than trusted, which is how the older mixed labels
        # converge on one form instead of persisting forever.
        known = {}
        for addr in addrs:
            code = normalize_country((records.get(addr) or {}).get("country", ""))
            if code:
                known[addr] = code
        missing = [a for a in addrs if a not in known]
        if not missing:
            return known

        found = geolocate([a.split(":")[0] for a in missing])
        if progress:
            progress(f"proxy store: resolved {len(found):,} of {len(missing):,} "
                     "previously unknown proxy countries")
        if found:
            with locked(self.path) as handle:
                records = self._load(handle)
                for addr in missing:
                    code = found.get(addr.split(":")[0])
                    if code and addr in records:
                        records[addr]["country"] = code
                self._save(handle, records)
            for addr in missing:
                code = found.get(addr.split(":")[0])
                if code:
                    known[addr] = code
        return known

    def record_blocked(self, addr: str, target: str) -> None:
        """Note that one target refused this address, without condemning it globally."""
        now = _now()
        with locked(self.path) as handle:
            records = self._load(handle)
            record = records.get(addr)
            if record is not None:
                record.setdefault("blocked", {})[target] = now
            self._save(handle, records)

    def prune(self, records: dict) -> dict:
        """Apply the ageing policy. Called on every write, so it never piles up.

        Four rules, in order of how confident each is:

        1. A dead address is kept for a day as a negative cache, then dropped —
           remembering it briefly is what stops the next refresh re-importing and
           re-testing the same corpses.
        2. An address that has never answered and has sat unverified for three
           days is dropped; the lists publish far more than will ever be checked.
        3. An address that once worked but has not answered for a week is
           retired, even without a run of consecutive failures.
        4. Expired per-target blocks are forgotten, so a site that was strict
           yesterday can be tried again.

        If the file is still over its ceiling after all that, the least useful go
        first: dead before unverified before live, then fewest successes.
        """
        now = _now()
        kept: dict = {}
        for addr, record in records.items():
            status = record.get("status", UNVERIFIED)
            last_checked = record.get("last_checked", 0.0)
            last_ok = record.get("last_ok", 0.0)
            first_seen = record.get("first_seen", now)

            if status == DEAD and now - last_checked > DEAD_MEMORY_SECONDS:
                continue
            if status == UNVERIFIED and not last_ok and now - first_seen > UNVERIFIED_TTL_SECONDS:
                continue
            if last_ok and now - last_ok > STALE_LIVE_SECONDS:
                continue

            record["blocked"] = {t: at for t, at in (record.get("blocked") or {}).items()
                                 if now - at < BLOCK_MEMORY_SECONDS}
            kept[addr] = record

        if len(kept) > self.max_records:
            def worth(item):
                _, record = item
                status_rank = {LIVE: 0, UNVERIFIED: 1, DEAD: 2}.get(record.get("status"), 3)
                return (status_rank, -record.get("ok", 0), -record.get("last_ok", 0.0))

            kept = dict(sorted(kept.items(), key=worth)[: self.max_records])
        return kept

    def stats(self) -> dict:
        with locked(self.path) as handle:
            records = self._load(handle)
        counts: dict = {}
        for record in records.values():
            status = record.get("status", UNVERIFIED)
            counts[status] = counts.get(status, 0) + 1
        return {"total": len(records), "by_status": counts, "path": str(self.path)}


@dataclass
class Budget:
    """Per-address usage limits on three timescales, enforced together.

    Keeps the timestamps of recent requests and answers one question: at the
    earliest, when may this address be used again? Entries fall out of the window
    as they age, so memory stays proportional to the hourly cap.
    """

    rps: float = PER_PROXY_RPS
    per_minute: int = PER_PROXY_PER_MINUTE
    per_hour: int = PER_PROXY_PER_HOUR
    _times: deque = field(default_factory=deque)

    def _prune(self, now: float) -> None:
        while self._times and now - self._times[0] >= 3600.0:
            self._times.popleft()

    def ready_at(self, now: float) -> float:
        self._prune(now)
        earliest = now
        if self._times:
            gap = 1.0 / self.rps if self.rps > 0 else 0.0
            earliest = max(earliest, self._times[-1] + gap)
        if self.per_minute and len(self._times) >= self.per_minute:
            recent = [t for t in self._times if now - t < 60.0]
            if len(recent) >= self.per_minute:
                earliest = max(earliest, recent[-self.per_minute] + 60.0)
        if self.per_hour and len(self._times) >= self.per_hour:
            earliest = max(earliest, self._times[-self.per_hour] + 3600.0)
        return earliest

    def record(self, now: float) -> None:
        self._times.append(now)


@dataclass
class ProxyPool:
    """Rotation over verified addresses: many at once, each of them gently."""

    live: list = field(default_factory=list)
    target: str = ""
    store: ProxyStore | None = None
    validated_from: int = 0
    blocked: int = 0
    retired: int = 0
    served: int = 0
    rps: float = PER_PROXY_RPS
    per_minute: int = PER_PROXY_PER_MINUTE
    per_hour: int = PER_PROXY_PER_HOUR
    # True while the background verifier is still adding addresses. It changes
    # what an empty pool means: not "no egress left", but "none ready yet".
    filling: bool = False
    # Refills performed, and every candidate checked across all of them. The
    # first is how a log shows the pool healing itself; the second is what the
    # runaway guard counts.
    refills: int = 0
    verified_total: int = 0
    _failures: dict = field(default_factory=dict)
    _budgets: dict = field(default_factory=dict)
    _busy: set = field(default_factory=set)
    _lock: threading.Condition = field(default_factory=threading.Condition)

    # Set by build(); a pool constructed directly simply never refills.
    _verify_cfg: dict = field(default_factory=dict)
    _tried: set = field(default_factory=set)
    _high_water: int = 0
    _last_refresh: float = 0.0
    _closed: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        for proxy in self.live:
            self._budgets.setdefault(
                proxy.addr,
                Budget(rps=self.rps, per_minute=self.per_minute, per_hour=self.per_hour),
            )

    def __len__(self) -> int:
        return len(self.live)

    def add(self, proxy) -> bool:
        """Put a newly verified address into rotation immediately.

        Safe to call while a crawl is running: a waiting acquirer is woken, so a
        proxy verified a second ago can serve the very next request.
        """
        with self._lock:
            if any(p.addr == proxy.addr for p in self.live):
                return False
            self._budgets.setdefault(
                proxy.addr,
                Budget(rps=self.rps, per_minute=self.per_minute, per_hour=self.per_hour),
            )
            self.live.append(proxy)
            self._lock.notify_all()
            return True

    @classmethod
    def build(cls, probe_url: str, *, want: int = 0, start_at: int | None = None,
              candidates: int = 900, workers: int = 120, timeout: float = 10.0,
              store: ProxyStore | None = None, accept=looks_usable, refresh: bool = True,
              target: str = "", rps: float = PER_PROXY_RPS,
              per_minute: int = PER_PROXY_PER_MINUTE, per_hour: int = PER_PROXY_PER_HOUR,
              refill: bool = True, refill_share: float = REFILL_SHARE,
              refill_budget: int = 0, refresh_after: float = REFRESH_AFTER_SECONDS,
              progress=None) -> "ProxyPool":
        """Start as soon as a few addresses answer, and keep filling in the background.

        Verification used to be a blocking phase: check every candidate, *then*
        begin. That is the wrong shape twice over. It leaves the caller idle for
        minutes while a slow, mostly-dead candidate list is worked through — and
        the work does not need a full pool to start, because a pool of ten already
        supports ten concurrent requests. Worse, the wait grows with `want`
        precisely because the store hands out its best-evidence addresses first,
        so each extra address asked for is drawn from a thinner part of the queue.

        So the build returns once `start_at` addresses answer, and a background
        thread keeps verifying up to `want`, adding each one to rotation the moment
        it passes. Throughput ramps instead of being paid for up front.

        Every outcome is still written back to the store, so this remains the
        mechanism that keeps the file healthy — promoting what worked, condemning
        what did not, pruning what aged out — with no separate maintenance job.

        **And it keeps filling for as long as the pool is used.** Once the first
        pass ends, a maintenance thread watches the live count and starts another
        pass whenever it falls to ``refill_share`` of its high-water mark, taking
        the candidates the last pass did not reach and re-reading the published
        lists when they are older than ``refresh_after``. ``refill_budget`` caps
        the candidates one run may check in total (0 derives one from
        ``candidates``); ``refill=False`` restores the old single-pass behaviour.
        """
        from concurrent.futures import ThreadPoolExecutor  # noqa: F401 - used by _verify

        ensure_file_limit()
        store = store or ProxyStore()
        if not target:
            target = probe_url.split("/")[2] if "//" in probe_url else probe_url
        # want=0 means "keep everything that answers". There is no reason to
        # discard a verified address: throughput is live addresses x the
        # per-address budget, so a cap on the pool is a cap on the crawl, and the
        # real spend is `candidates` - how many are *tested* - not how many pass.
        unlimited = want <= 0
        if start_at is None:
            start_at = 10 if unlimited else max(1, min(want, 10))
        if not unlimited:
            start_at = min(start_at, want)

        if refresh:
            summary = store.refresh()
            if progress:
                progress(f"proxy store: {summary['published']:,} published, "
                         f"{summary['new']:,} new, {summary['revived']:,} revived")

        batch = store.candidates(limit=candidates)

        # Resolve country BEFORE verifying, not after. The pool fills in a
        # background thread and the crawl starts the moment a handful of
        # addresses answer, so labelling at the end would leave every early
        # result unable to say where it came from. Doing it first costs one pass
        # over addresses the store has not seen before, and never again.
        labels = store.resolve_countries([p.addr for p in batch], progress=progress)
        if labels:
            batch = [Proxy(p.addr, p.kind, labels.get(p.addr) or p.country)
                     for p in batch]

        target_text = "keeping every one that answers" if unlimited else f"filling to {want}"
        if progress:
            progress(f"proxy store: verifying up to {len(batch):,} candidates; "
                     f"starting as soon as {start_at} answer, {target_text}")

        pool = cls(live=[], target=target, store=store, rps=rps,
                   per_minute=per_minute, per_hour=per_hour)
        pool._verify_cfg = {
            "probe_url": probe_url, "workers": workers, "timeout": timeout,
            "accept": accept, "want": want, "unlimited": unlimited,
            "candidates": candidates, "progress": progress,
            "refill": refill, "refill_share": refill_share,
            "refill_budget": refill_budget or max(candidates * 5, 5000),
            "refresh_after": refresh_after, "start_at": start_at,
        }
        pool._tried = {proxy.addr for proxy in batch}
        pool._last_refresh = time.monotonic()
        finished = threading.Event()

        def verify_all():
            try:
                pool._verify(batch)
            finally:
                pool.validated_from = pool.verified_total
                pool.filling = False
                finished.set()
                if progress:
                    checked = pool.verified_total
                    progress(f"proxy pool: filled to {len(pool)} from {checked} checked "
                             f"({100 * len(pool) / max(1, checked):.1f}% usable) — "
                             f"ceiling ≈ {len(pool) * rps:.0f} req/s, "
                             f"{len(pool) * per_hour:,}/hour")
                if refill:
                    pool._start_maintenance()

        pool.filling = True
        threading.Thread(target=verify_all, name="proxy-pool-fill", daemon=True).start()

        # Wait only for the first few, not for the whole list.
        while len(pool) < start_at and not finished.is_set():
            finished.wait(0.25)
        if progress and len(pool):
            progress(f"proxy pool: {len(pool)} address(es) ready — starting now, "
                     f"{target_text} in the background")
        return pool

    def _verify(self, batch) -> int:
        """Check a batch of candidates and admit every one that answers.

        Shared by the first pass and by every refill, so an address admitted an
        hour into a crawl went through exactly the same test as one admitted at
        the start. Returns how many joined the pool.
        """
        from concurrent.futures import ThreadPoolExecutor

        cfg = self._verify_cfg
        results: dict = {}
        before = len(self)
        # Deliberately not the caller's own worker count. Verification runs
        # alongside the crawl for as long as the pool keeps refilling, and both
        # sides spend descriptors, so the checker takes the smaller share.
        checkers = min(cfg["workers"], VERIFY_WORKERS_MAX)

        def check(proxy):
            status, body = fetch_through(proxy, cfg["probe_url"], cfg["timeout"])
            return proxy, status, body

        try:
            with ThreadPoolExecutor(max_workers=checkers) as executor:
                for proxy, status, body in executor.map(check, batch):
                    ok = cfg["accept"](status, body)
                    results[proxy.addr] = ok
                    if ok:
                        self.add(proxy)
                        if not cfg["unlimited"] and len(self) >= cfg["want"]:
                            break
        finally:
            # Record even on an early exit: outcomes already gathered are the
            # only thing that makes the next build faster.
            try:
                if self.store is not None:
                    self.store.record_result(dict(results), target=self.target)
            except Exception:  # noqa: BLE001 - never kill a crawl over bookkeeping
                pass
            self.verified_total += len(results)
            with self._lock:
                self._high_water = max(self._high_water, len(self.live))
        return len(self) - before

    def refill_at(self) -> int:
        """The live count at which another verification pass is worth starting.

        Relative to the pool's own high-water mark rather than to ``want``:
        ``want`` is commonly 0, meaning "keep everything that answers", and a
        fixed floor would be either trivial for a pool of six hundred or
        unreachable for a pool of twenty. Never below ``start_at``, because a
        pool that small is one bad minute from being a dead end.
        """
        cfg = self._verify_cfg
        share = cfg.get("refill_share", REFILL_SHARE)
        return max(int(cfg.get("start_at", 1)), int(self._high_water * share))

    def refill_once(self) -> int:
        """One refill pass: re-read the lists if they are stale, then verify.

        Public because it is the whole mechanism, and a caller that would rather
        drive it itself — a test, or a scheduler — should not have to reach for
        a private name.
        """
        cfg = self._verify_cfg
        store = self.store
        if store is None or not cfg:
            return 0
        progress = cfg.get("progress")
        now = time.monotonic()
        if now - self._last_refresh >= cfg["refresh_after"]:
            # The lists have moved on since this run started. Cheap, and the only
            # way a long crawl ever sees an address published after it began.
            try:
                summary = store.refresh()
                if progress:
                    progress(f"proxy store: refreshed mid-run — {summary['new']:,} new, "
                             f"{summary['revived']:,} revived")
            except Exception:  # noqa: BLE001 - a failed refresh must not end a crawl
                pass
            self._last_refresh = now
        batch = store.candidates(limit=cfg["candidates"], exclude=self._tried)
        if not batch:
            return 0
        self._tried.update(proxy.addr for proxy in batch)
        self.filling = True
        try:
            added = self._verify(batch)
        finally:
            self.filling = False
            with self._lock:
                self._lock.notify_all()
        self.refills += 1
        if progress:
            progress(f"proxy pool: refill {self.refills} added {added} — "
                     f"{len(self)} live, {self.verified_total:,} checked in total")
        return added

    def _start_maintenance(self) -> None:
        with self._lock:
            self._high_water = max(self._high_water, len(self.live))
        threading.Thread(target=self._maintain, name="proxy-pool-maintain",
                         daemon=True).start()

    def _maintain(self) -> None:
        """Watch the live count and refill when it falls, until the run ends.

        A daemon thread, so it never keeps a process alive; and it stops on its
        own once the store has nothing left to offer or the run has spent its
        candidate budget, rather than spinning against an exhausted store.
        """
        cfg = self._verify_cfg
        interval = REFILL_CHECK_SECONDS
        while not self._closed.wait(interval):
            if self.filling or len(self) > self.refill_at():
                interval = REFILL_CHECK_SECONDS
                continue
            if self.verified_total >= cfg["refill_budget"]:
                progress = cfg.get("progress")
                if progress:
                    progress(f"proxy pool: refill budget spent "
                             f"({self.verified_total:,} candidates checked)")
                return
            added = self.refill_once()
            # A pass that found nothing means the store is thin right now, not
            # that it will be thin in ten minutes: back off rather than stop.
            interval = (REFILL_CHECK_SECONDS if added
                        else min(interval * 2, REFILL_BACKOFF_MAX))

    def stop(self) -> None:
        """End maintenance. Safe to call more than once, and safe never to call."""
        self._closed.set()

    def acquire(self, max_wait: float = 120.0):
        """Hand out the most-rested idle address, waiting if every one is spending.

        Three rules together produce "many at once, each of them gently":

        *One request at a time per address.* An address already mid-request is not
        offered again, so worker threads necessarily spread across different
        addresses instead of colliding on one.

        *Least-recently-used first*, rather than round-robin. Round-robin looks
        fair but is not, once addresses start being evicted: the survivors inherit
        the whole load in the same order.

        *Nobody goes early.* An address is offered only once its own per-second,
        per-minute and per-hour budgets all allow it.

        Returns None only when the pool is empty, which is a real dead end.
        """
        deadline = time.monotonic() + max_wait
        with self._lock:
            while True:
                if not self.live:
                    # An empty pool is only a dead end once nothing more is
                    # coming. While the background verifier is still working,
                    # every address may simply have been evicted faster than
                    # replacements arrive - so wait for one rather than ending
                    # the crawl on a gap that closes by itself.
                    if not self.filling or time.monotonic() >= deadline:
                        return None
                    self._lock.wait(0.25)
                    continue
                now = time.monotonic()
                idle = [p for p in self.live if p.addr not in self._busy]
                if idle:
                    ready = sorted(
                        ((self._budgets[p.addr].ready_at(now), p) for p in idle),
                        key=lambda item: (item[0], item[1].addr),
                    )
                    when, proxy = ready[0]
                    if when <= now:
                        self._busy.add(proxy.addr)
                        self._budgets[proxy.addr].record(now)
                        self.served += 1
                        return proxy
                    delay = min(when - now, deadline - now)
                else:
                    delay = min(0.25, max(0.0, deadline - now))
                if delay <= 0 and time.monotonic() >= deadline:
                    return None
                self._lock.wait(max(delay, 0.01))

    def release(self, proxy) -> None:
        """Mark an address idle again. Always call this, including after a failure."""
        with self._lock:
            self._busy.discard(proxy.addr)
            self._lock.notify_all()

    def report_ok(self, proxy) -> None:
        with self._lock:
            self._failures.pop(proxy.addr, None)

    def report_failure(self, proxy) -> None:
        """Count a flaky call; drop the address once it is consistently unusable."""
        with self._lock:
            count = self._failures.get(proxy.addr, 0) + 1
            self._failures[proxy.addr] = count
            if count >= FAILURE_LIMIT:
                self._drop(proxy)
                self.retired += 1

    def report_blocked(self, proxy) -> None:
        """The target refused this address: drop it here, and remember it there.

        Recorded against this target only. The same address may serve a different
        site perfectly well, and the store is shared between projects.
        """
        with self._lock:
            self._drop(proxy)
            self.blocked += 1
        if self.store is not None and self.target:
            self.store.record_blocked(proxy.addr, self.target)

    def _drop(self, proxy) -> None:
        # Caller holds the lock.
        self.live = [p for p in self.live if p.addr != proxy.addr]
        self._failures.pop(proxy.addr, None)
        self._busy.discard(proxy.addr)
        self._lock.notify_all()

    def stats(self) -> dict:
        with self._lock:
            return {
                "live": len(self.live),
                "validated_from": self.validated_from,
                "blocked_by_target": self.blocked,
                "retired_unreliable": self.retired,
                "requests_served": self.served,
                "still_filling": self.filling,
                "refills": self.refills,
                "verified_total": self.verified_total,
                "target": self.target,
                "per_proxy_limits": {"per_second": self.rps, "per_minute": self.per_minute,
                                     "per_hour": self.per_hour},
            }
