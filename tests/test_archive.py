"""Tests for keel_crawler.archive: the earliest capture whose page says what it must. No network.

Run: python -m unittest tests.test_archive
"""
import unittest
from datetime import date
from unittest import mock

from keel_crawler import archive

PARKED = "<html><body>Domain Name For Sale - contact: info@example.com</body></html>"
FIRM = "<html><body><h1>ACME Funding</h1><p>Pass the ACME challenge and get funded.</p></body></html>"


class _Answer:
    def __init__(self, status_code, text="", payload=None):
        self.status_code, self.text, self._payload = status_code, text, payload

    def json(self):
        return self._payload


def _session(index: dict, pages: dict):
    """A session whose index answers from ``index`` ({(path, collapse, year or ""): rows})
    and whose raw pages answer from ``pages`` ({timestamp: html})."""
    def get(url, params=None, timeout=None):
        if url == archive.CDX:
            path = params["url"].split("/", 1)[1] if "/" in params["url"] else ""
            key = ("/" + path, params["collapse"], params.get("from", ""))
            rows = index.get(key, [])
            return _Answer(200, "x" if rows else "", [["timestamp", "original"], *rows] if rows else [])
        ts = url.split("/web/", 1)[1].split("id_/", 1)[0]
        html = pages.get(ts)
        return _Answer(200, html) if html is not None else _Answer(404, "")
    s = mock.Mock()
    s.get.side_effect = get
    return s


class EarliestCaptureTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(archive.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_parked_domain_is_not_the_business(self):
        index = {("/", "timestamp:4", ""): [["20100101000000", "http://acme.com/"], ["20160101000000", "http://acme.com/"]]}
        pages = {"20100101000000": PARKED, "20160101000000": FIRM}
        found = archive.earliest_capture(["www.acme.com"], [r"acme", r"funded|challenge"],
                                         session=_session(index, pages))
        self.assertEqual(found["date"], date(2016, 1, 1))
        self.assertEqual(found["url"], "https://web.archive.org/web/20160101000000/http://acme.com/")

    def test_the_year_before_is_read_month_by_month(self):
        index = {("/", "timestamp:4", ""): [["20150101000000", "http://acme.com/"], ["20160101000000", "http://acme.com/"]],
                 ("/", "timestamp:6", "2015"): [["20150101000000", "http://acme.com/"], ["20150601000000", "http://acme.com/"]]}
        pages = {"20150101000000": PARKED, "20150601000000": FIRM, "20160101000000": FIRM}
        found = archive.earliest_capture(["acme.com"], [r"acme", r"funded"], session=_session(index, pages))
        self.assertEqual(found["date"], date(2015, 6, 1))

    def test_a_language_path_is_searched_beside_the_home_page(self):
        index = {("/", "timestamp:4", ""): [["20100101000000", "http://acme.com/"]],
                 ("/en/", "timestamp:4", ""): [["20200805000000", "https://acme.com/en/"]]}
        pages = {"20100101000000": PARKED, "20200805000000": FIRM}
        found = archive.earliest_capture(["acme.com"], [r"acme", r"funded"], session=_session(index, pages))
        self.assertEqual(found["original"], "https://acme.com/en/")

    def test_nothing_matching_is_none(self):
        index = {("/", "timestamp:4", ""): [["20100101000000", "http://acme.com/"]]}
        self.assertIsNone(archive.earliest_capture(["acme.com"], [r"acme"], session=_session(index, {"20100101000000": PARKED})))

    def test_a_page_the_archive_will_not_hand_over_stops_the_search(self):
        index = {("/", "timestamp:4", ""): [["20100101000000", "http://acme.com/"], ["20160101000000", "http://acme.com/"]]}
        with self.assertRaises(archive.ArchiveError):
            archive.earliest_capture(["acme.com"], [r"acme"], session=_session(index, {"20160101000000": FIRM}))


if __name__ == "__main__":
    unittest.main()
