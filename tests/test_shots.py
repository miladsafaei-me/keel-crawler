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

    def test_a_frame_is_a_crop_around_the_block(self):
        out = shots.frame(_webp(), {"x": 100, "y": 300, "w": 400, "h": 40})
        from PIL import Image

        image = Image.open(io.BytesIO(out))
        self.assertEqual(image.width, 1200)
        self.assertLess(image.height, 600)


if __name__ == "__main__":
    unittest.main()
