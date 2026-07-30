"""Poll the RSS watchlist, run the deterministic pre-filter, then the host triage hook.

    python manage.py crawler_rss_poll [--no-filter] [--no-triage] [--max N]
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Poll active RSS feeds, stage new items, pre-filter, and hand off to triage."

    def add_arguments(self, parser):
        parser.add_argument("--max", type=int, default=None, help="Max items per feed.")
        parser.add_argument("--no-filter", action="store_true", help="Skip deterministic pre-filter.")
        parser.add_argument("--no-triage", action="store_true", help="Skip the host triage hook.")

    def handle(self, *args, **opts):
        from keel_crawler.rss import apply_deterministic_filter, poll_feeds, run_triage

        poll_stats = poll_feeds(max_items_per_feed=opts["max"])
        self.stdout.write(f"poll:   {json.dumps(poll_stats)}")

        if not opts["no_filter"]:
            filter_stats = apply_deterministic_filter()
            self.stdout.write(f"filter: {json.dumps(filter_stats)}")

        if not opts["no_triage"]:
            triage_stats = run_triage()
            self.stdout.write(f"triage: {json.dumps(triage_stats)}")

        self.stdout.write(self.style.SUCCESS("crawler_rss_poll done"))
