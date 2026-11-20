import json

from keel_crawler import quotes
from keel_crawler.proxy import batch
from keel_crawler.tradingview import LONG, SHORT, feed_url, looks_like_feed, parse_feed, read_idea

FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<title>TradingView Ideas</title>
<item>
  <title>EURUSD 4H: Lower Highs, Two Sell Zones</title>
  <link>https://www.tradingview.com/chart/EURUSD/AbC123xy-EURUSD-4H-Lower-Highs/</link>
  <description>https://www.tradingview.com/x/Eu4Q3Eru/   Price keeps failing at 1.0953 &amp; 1.0990. Target 1.0880, 25% of the move done.</description>
  <pubDate>Fri, 20 Nov 2026 11:45:58 +0000</pubDate>
  <content:encoded><![CDATA[<div class="avatar"><a href="https://www.tradingview.com/u/SomeAuthor/" title="User Profile"></a></div><img src="https://s3.tradingview.com/userpics/1-a_mid.png"/><img src="https://s3.tradingview.com/a/AbC123xy_mid.png"/>]]></content:encoded>
</item>
<item>
  <title>no link item</title>
  <pubDate>Fri, 20 Nov 2026 11:45:58 +0000</pubDate>
</item>
</channel></rss>"""


def test_feed_url_keeps_the_exchange_colon():
    assert feed_url("FX:EURUSD") == "https://www.tradingview.com/feed/?sort=recent&symbol=FX:EURUSD"


def test_parse_feed_reads_id_author_time_and_clean_text():
    ideas = parse_feed(FEED)
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.id == "AbC123xy"
    assert idea.author == "SomeAuthor"
    assert idea.author_url == "https://www.tradingview.com/u/SomeAuthor/"
    assert idea.published.isoformat() == "2026-11-20T11:45:58+00:00"
    assert "tradingview.com/x/" not in idea.text
    assert idea.image == "https://s3.tradingview.com/a/AbC123xy_mid.png"
    assert "1.0953 & 1.0990" in idea.text


def test_parse_feed_survives_garbage():
    assert parse_feed("<html>blocked</html") == []


def test_looks_like_feed():
    assert looks_like_feed(200, FEED)
    assert not looks_like_feed(200, "<html>captcha</html>")
    assert not looks_like_feed(403, FEED)


def test_read_idea_direction_timeframe_and_levels():
    idea = parse_feed(FEED)[0]
    reading = read_idea(idea.title, idea.text, ref_price=1.0970)
    assert reading.direction == SHORT
    assert reading.timeframe == "4h" and reading.timeframe_minutes == 240
    assert reading.levels == [1.088, 1.0953, 1.099]
    assert not reading.promo


def test_read_idea_title_outweighs_body_and_ties_stay_unknown():
    assert read_idea("GBPUSD Buy!", "a short pullback first").direction == LONG
    assert read_idea("GBPUSD", "buy the dip or sell the rip").direction == ""


def test_read_idea_timeframe_spellings():
    assert read_idea("EURUSD H1 setup", "").timeframe == "1h"
    assert read_idea("EURUSD 15M analysis", "").timeframe == "15m"
    assert read_idea("EURUSD daily view", "").timeframe == "1D"
    assert read_idea("EURUSD idea", "").timeframe == ""


def test_read_idea_levels_need_a_reference_and_skip_percentages():
    assert read_idea("x", "1.0950 and 12.5% and 150.20", ref_price=None).levels == []
    assert read_idea("x", "1.0950 and 1.25% and 150.20", ref_price=1.0970).levels == [1.095]
    assert read_idea("x", "155.20 then 154.8", ref_price=155.0).levels == [154.8, 155.2]


def test_read_idea_flags_adverts():
    assert read_idea("EURUSD buy", "Join our VIP Telegram for more").promo
    assert not read_idea("EURUSD buy", "Clean break of the range").promo


def test_parse_chart_skips_empty_bars():
    body = json.dumps({"chart": {"result": [{"timestamp": [1795170000, 1795173600, 1795177200],
                                             "indicators": {"quote": [{"close": [1.1, None, 1.2]}]}}]}})
    bars = quotes.parse_chart(body)
    assert [c for _, c in bars] == [1.1, 1.2]
    assert quotes.looks_like_chart(200, body)
    assert not quotes.looks_like_chart(200, "{}")
    assert quotes.forex_symbol("eur", "usd") == "EURUSD=X"


def test_fetch_many_retries_on_another_address(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    calls = []

    class FakePool:
        def __init__(self):
            self.n = 0

        def acquire(self, max_wait=0):
            self.n += 1
            return f"p{self.n}"

        def release(self, proxy):
            pass

        def report_ok(self, proxy):
            calls.append(("ok", proxy))

        def report_blocked(self, proxy):
            calls.append(("blocked", proxy))

        def report_failure(self, proxy):
            calls.append(("fail", proxy))

        def stop(self):
            calls.append(("stop", None))

    answers = iter([(403, ""), (200, FEED), (0, ""), (0, ""), (0, "")])
    monkeypatch.setattr(batch.ProxyPool, "build", classmethod(lambda cls, *a, **k: FakePool()))
    monkeypatch.setattr(batch, "fetch_through", lambda proxy, url, timeout=0: next(answers))
    out = batch.fetch_many(["https://a", "https://b"], accept=looks_like_feed, tries=3)
    assert out == {"https://a": FEED, "https://b": None}
    assert ("blocked", "p1") in calls and ("ok", "p2") in calls and calls[-1] == ("stop", None)
