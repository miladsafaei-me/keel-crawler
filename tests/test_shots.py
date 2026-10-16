"""Layer 7: screenshots kept by version. The store's rule, the page key, finding a quote."""
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keel_crawler import shots


def _webp(colour=(255, 255, 255)):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1280, 600), colour).save(out, "WEBP")
    return out.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = shots.ShotStore(Path(self.tmp.name))
        self.t0 = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
        self.url = "https://www.example.com/pricing/"

    def tearDown(self):
        self.tmp.cleanup()

    def _blocks(self, text):
        return [{"t": text, "x": 10, "y": 100, "w": 500, "h": 30}]

    def test_an_unchanged_page_replaces_its_previous_shot(self):
        self.store.record("firm", self.url, when=self.t0, webp=_webp(), blocks=self._blocks("Fee 100"))
        self.store.record("firm", self.url, when=self.t0 + timedelta(days=1), webp=_webp(), blocks=self._blocks("Fee 100"))
        versions = self.store.versions("firm", self.url)
        self.assertEqual([v.stamp for v in versions], ["20261002T010000Z"])
        self.assertEqual(len(list((Path(self.tmp.name) / "firm").rglob("*.webp"))), 1)

    def test_a_change_that_matters_keeps_both_versions_and_never_loses_them(self):
        self.store.record("firm", self.url, when=self.t0, webp=_webp(), blocks=self._blocks("Fee 100"))
        self.store.record("firm", self.url, when=self.t0 + timedelta(days=1), webp=_webp(), blocks=self._blocks("Fee 120"), keep=True)
        self.store.record("firm", self.url, when=self.t0 + timedelta(days=2), webp=_webp(), blocks=self._blocks("Fee 120"))
        versions = self.store.versions("firm", self.url)
        self.assertEqual([(v.stamp[:8], v.kept) for v in versions],
                         [("20261001", True), ("20261002", True), ("20261003", False)])
        before = self.store.before("firm", self.url, self.t0 + timedelta(hours=12))
        after = self.store.after("firm", self.url, self.t0 + timedelta(hours=12))
        self.assertEqual((before.stamp[:8], after.stamp[:8]), ("20261001", "20261002"))
        self.assertTrue(before.image.is_file() and after.image.is_file())

    def test_the_page_key_is_stable_across_www_and_a_trailing_slash(self):
        self.assertEqual(shots.page_key("https://www.example.com/pricing/"), "example.com/pricing")
        self.assertEqual(shots.page_key("https://example.com/pricing"), "example.com/pricing")
        self.assertNotEqual(shots.page_key("https://example.com/p?tab=1"), shots.page_key("https://example.com/p?tab=2"))

    def test_a_quote_is_found_whole_or_by_its_longest_run_of_words(self):
        blocks = [{"t": "Profit target 8% in phase one.", "x": 0, "y": 0, "w": 1, "h": 1},
                  {"t": "The Consistency rule requires that your Best Day does not represent more than 50% of your total profit", "x": 0, "y": 50, "w": 1, "h": 1}]
        self.assertEqual(shots.find_block(blocks, "best day does NOT represent more than 50%")["y"], 50)
        self.assertEqual(shots.find_block(blocks, "The Consistency rule requires that your Best Day stays under half")["y"], 50)
        self.assertIsNone(shots.find_block(blocks, "nothing like this at all here"))

    def test_a_bot_checkpoint_is_recognised(self):
        self.assertTrue(shots.is_checkpoint([{"t": "blueberryfunded.com"}, {"t": "Performing security verification"}, {"t": "Ray ID:"}]))
        self.assertFalse(shots.is_checkpoint([{"t": "Pricing"}, {"t": "$100,000 account 499 USD"}]))

    def test_a_page_taller_than_webp_allows_is_cut_at_the_limit(self):
        from PIL import Image

        png = io.BytesIO()
        Image.new("RGB", (1280, 20000), (255, 255, 255)).save(png, "PNG")
        out = Image.open(io.BytesIO(shots.to_webp(png.getvalue())))
        self.assertEqual(out.height, shots.MAX_HEIGHT)

    def test_a_frame_is_a_crop_around_the_block(self):
        out = shots.frame(_webp(), {"x": 100, "y": 300, "w": 400, "h": 40})
        from PIL import Image

        image = Image.open(io.BytesIO(out))
        self.assertEqual(image.width, 1200)
        self.assertLess(image.height, 600)


if __name__ == "__main__":
    unittest.main()


class RenderViaTests(unittest.TestCase):
    """The proxy ladder for one page, with a fake renderer (no browser)."""

    @staticmethod
    def _factory(outcomes):
        class Fake:
            def __init__(self, proxy_server="", **options):
                self.proxy_server = proxy_server

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def render(self, url):
                out = outcomes[self.proxy_server]
                if isinstance(out, Exception):
                    raise out
                return shots.Rendered(png=b"", blocks=out, width=1280, height=900, url=url)

        return Fake

    PAGE = [{"t": f"line {i}"} for i in range(12)]
    WALL = [{"t": "Performing security verification"}]

    def test_the_first_readable_address_wins_and_is_reported(self):
        seen = []
        factory = self._factory({"a": self.WALL, "b": RuntimeError("refused"), "c": self.PAGE, "d": self.PAGE})
        got = shots.render_via("https://x.example/", ["a", "b", "c", "d"], renderer_factory=factory, on_success=seen.append)
        self.assertEqual(got[1], "c")
        self.assertEqual(seen, ["c"])

    def test_nothing_readable_returns_none_and_respects_max_tries(self):
        factory = self._factory({"a": self.WALL, "b": self.WALL, "c": self.PAGE})
        self.assertIsNone(shots.render_via("https://x.example/", ["a", "b", "c"], max_tries=2, renderer_factory=factory))

    def test_the_route_is_kept_on_the_version_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = shots.ShotStore(Path(tmp))
            when = datetime(2026, 10, 1, tzinfo=timezone.utc)
            store.record("firm", "https://x.example/p", when=when, webp=_webp(), blocks=[{"t": "a"}], route="pool-browser")
            self.assertEqual(store.versions("firm", "https://x.example/p")[0].route, "pool-browser")


class WatchdogTests(unittest.TestCase):
    def test_a_browser_process_under_this_one_is_killed_by_its_command_line(self):
        import subprocess
        import time

        child = subprocess.Popen(["bash", "-c", "exec -a chromium-under-test sleep 30"])
        try:
            time.sleep(0.3)
            killed = shots.kill_browser_processes(match=("chromium-under-test",))
            self.assertEqual(killed, 1)
            self.assertIsNotNone(child.wait(timeout=5))
        finally:
            if child.poll() is None:
                child.kill()

    def test_the_reading_stamp_is_kept_beside_the_render_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = shots.ShotStore(Path(tmp))
            rendered_at = datetime(2026, 10, 17, 6, 20, tzinfo=timezone.utc)
            reading_at = datetime(2026, 10, 17, 3, 10, tzinfo=timezone.utc)
            version = store.record("alpha", "https://alpha.example/p", when=rendered_at, webp=_webp(),
                                   blocks=[{"t": "x", "x": 0, "y": 0, "w": 1, "h": 1}], reading_at=reading_at)
            self.assertEqual(version.when, rendered_at)
            self.assertEqual(store.versions("alpha", "https://alpha.example/p")[0].reading, "20261017T031000Z")


class InboxTests(unittest.TestCase):
    def test_a_shot_put_during_a_crawl_is_drained_once_with_its_render_time(self):
        import io as _io

        from PIL import Image

        png = _io.BytesIO()
        Image.new("RGB", (1280, 600), (255, 255, 255)).save(png, "PNG")
        rendered = shots.Rendered(png=png.getvalue(), blocks=[{"t": "Fee 500 USD", "x": 1, "y": 2, "w": 3, "h": 4}],
                                  width=1280, height=600, url="https://alpha.example/pricing")
        with tempfile.TemporaryDirectory() as tmp:
            inbox = shots.ShotInbox(Path(tmp) / "inbox")
            inbox.put("https://alpha.example/pricing", rendered, route="direct")
            items = list(inbox.drain())
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["url"], "https://alpha.example/pricing")
            self.assertEqual(items[0]["rendered_at"], rendered.rendered_at)
            self.assertEqual(items[0]["blocks"][0]["t"], "Fee 500 USD")
            self.assertEqual(list(inbox.drain()), [])
