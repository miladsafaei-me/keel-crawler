"""Maintenance for the shared proxy store.

    python -m keel_crawler.proxy refresh              # pull every list, merge new addresses
    python -m keel_crawler.proxy check <url>          # verify against a real target
    python -m keel_crawler.proxy prune                # apply the ageing policy now
    python -m keel_crawler.proxy stats                # what is on file
    python -m keel_crawler.proxy sources              # which lists are reachable today

Routine use needs none of this: :meth:`ProxyPool.build` refreshes, verifies and
prunes on every run, so the store maintains itself. These commands exist for
inspecting it, for pre-warming before a big crawl, and for checking whether the
published lists are still alive.
"""
from __future__ import annotations

import argparse
import json
import sys

from keel_crawler.proxy.pool import ProxyPool, ProxyStore, looks_usable
from keel_crawler.proxy.sources import SOURCES, fetch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m keel_crawler.proxy",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="pull every published list into the store")
    check = sub.add_parser("check", help="verify stored addresses against a real URL")
    check.add_argument("url", help="the target to verify against — use the real one")
    check.add_argument("--want", type=int, default=60)
    check.add_argument("--candidates", type=int, default=900)
    check.add_argument("--workers", type=int, default=120)
    sub.add_parser("prune", help="apply the ageing policy immediately")
    sub.add_parser("stats", help="counts by status, and where the file lives")
    sub.add_parser("sources", help="fetch each list and report what it returned")
    args = parser.parse_args(argv)

    store = ProxyStore()

    def say(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    if args.command == "refresh":
        summary = store.refresh()
        print(json.dumps({**summary, **store.stats()}, indent=1))
        return 0

    if args.command == "check":
        pool = ProxyPool.build(args.url, want=args.want, candidates=args.candidates,
                               workers=args.workers, store=store, accept=looks_usable,
                               progress=say)
        print(json.dumps({"pool": pool.stats(), "store": store.stats()}, indent=1))
        return 0 if len(pool) else 1

    if args.command == "prune":
        # Pruning happens inside every write, so an empty refresh is the cheapest
        # way to force one without inventing a second code path for it.
        before = store.stats()["total"]
        store.record_result({})
        after = store.stats()
        print(json.dumps({"removed": before - after["total"], **after}, indent=1))
        return 0

    if args.command == "stats":
        print(json.dumps(store.stats(), indent=1))
        return 0

    if args.command == "sources":
        rows = []
        for source in SOURCES:
            entries = fetch(source)
            rows.append({"name": source.name, "publisher": source.publisher,
                         "kind": source.kind, "entries": len(entries)})
        alive = [r for r in rows if r["entries"]]
        print(json.dumps({"reachable": len(alive), "total": len(rows),
                          "entries": sum(r["entries"] for r in alive),
                          "publishers_alive": len({r["publisher"] for r in alive}),
                          "sources": sorted(rows, key=lambda r: -r["entries"])}, indent=1))
        return 0 if alive else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
