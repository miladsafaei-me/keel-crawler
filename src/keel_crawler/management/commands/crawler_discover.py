"""Discover URLs for a site via sitemap and/or deep crawl (read-only; prints results).

    python manage.py crawler_discover https://example.com --sitemap
    python manage.py crawler_discover https://example.com --deep --depth 2 --max 100
    python manage.py crawler_discover https://example.com --deep --browser   # JS nav

By default runs the sitemap strategy. Deep crawl uses the light HTTP+regex link
fetcher unless ``--browser`` is given (which uses the crawl4ai link_harvest profile).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Discover a site's URLs from its sitemap and/or by deep crawling links."

    def add_arguments(self, parser):
        parser.add_argument("base_url")
        parser.add_argument("--sitemap", action="store_true", help="Use sitemap discovery.")
        parser.add_argument("--deep", action="store_true", help="Use deep-crawl discovery.")
        parser.add_argument("--browser", action="store_true", help="Deep crawl via crawl4ai link_harvest.")
        parser.add_argument("--depth", type=int, default=2, help="Deep-crawl max depth.")
        parser.add_argument("--max", type=int, default=200, help="Max pages/URLs to return.")
        parser.add_argument("--limit-print", type=int, default=50, help="How many URLs to print.")

    def handle(self, *args, **opts):
        base = opts["base_url"]
        do_sitemap = opts["sitemap"] or not opts["deep"]
        do_deep = opts["deep"]
        found: list[str] = []

        if do_sitemap:
            from keel_crawler.discover import discover_sitemap_urls

            urls = discover_sitemap_urls(base, max_urls=opts["max"])
            self.stdout.write(self.style.SUCCESS(f"sitemap: {len(urls)} URL(s)"))
            found.extend(urls)

        if do_deep:
            from keel_crawler.discover import (
                browser_links_many_fetcher,
                deep_crawl,
                http_links_many_fetcher,
            )

            if opts["browser"]:
                from keel_crawler import BrowserFetcher

                fetch_many = browser_links_many_fetcher(
                    BrowserFetcher.from_config(run_profile="link_harvest")
                )
            else:
                from keel_crawler import HttpFetcher

                fetch_many = http_links_many_fetcher(HttpFetcher())
            urls = deep_crawl(
                [base], fetch_links_many=fetch_many, max_pages=opts["max"], max_depth=opts["depth"]
            )
            self.stdout.write(self.style.SUCCESS(f"deep crawl: {len(urls)} URL(s)"))
            found.extend(urls)

        from keel_crawler.browser.harvest import dedupe_urls_preserve_order

        found = dedupe_urls_preserve_order(found)
        for u in found[: opts["limit_print"]]:
            self.stdout.write(f"  {u}")
        if len(found) > opts["limit_print"]:
            self.stdout.write(f"  … and {len(found) - opts['limit_print']} more")
