"""Inspect or reset the proxy score store.

    python manage.py crawler_proxy_scores --show
    python manage.py crawler_proxy_scores --reset
    python manage.py crawler_proxy_scores --reset-outbound "JP-01"
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from keel_crawler.proxy.scores import default_score_store


class Command(BaseCommand):
    help = "Show, reset, or clear one outbound in the proxy score store."

    def add_arguments(self, parser):
        parser.add_argument("--show", action="store_true", help="Print ranked scores + delays.")
        parser.add_argument("--reset", action="store_true", help="Clear ALL scores and delays.")
        parser.add_argument("--reset-outbound", metavar="NAME", default="", help="Clear one outbound.")

    def handle(self, *args, **opts):
        store = default_score_store()
        self.stdout.write(f"scoring enabled: {store.enabled}")
        self.stdout.write(f"scores file:     {store.scores_path}")

        if opts["reset"]:
            store.reset()
            self.stdout.write(self.style.SUCCESS("reset: all scores + delays cleared"))
            return
        if opts["reset_outbound"]:
            store.reset_outbound(opts["reset_outbound"])
            self.stdout.write(self.style.SUCCESS(f"reset outbound: {opts['reset_outbound']}"))
            return

        scores = store.load_scores()
        delays = store.load_delays()
        if not scores and not delays:
            self.stdout.write("(no scores recorded yet)")
            return
        ranked = store.sort_by_rank(list(scores.keys()), scores=scores, delays=delays)
        self.stdout.write("rank  score  delay_ms  outbound")
        for name in ranked:
            self.stdout.write(f"      {scores.get(name, 0):>5}  {delays.get(name, -1):>8}  {name}")
