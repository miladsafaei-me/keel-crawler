"""Price bars from Yahoo Finance's public chart endpoint, stdlib only.

``https://query1.finance.yahoo.com/v8/finance/chart/<symbol>?interval=1h&range=1mo``
answers JSON with one timestamp and one close per bar and needs no key. Forex
symbols are written ``EURUSD=X``. Fetch it through the proxy pool
(:func:`keel_crawler.proxy.batch.fetch_many` with :func:`looks_like_chart`), never
from a production host's own address.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range}"


def forex_symbol(base: str, quote_ccy: str) -> str:
    """Yahoo's spelling of a currency pair: ``forex_symbol("EUR", "USD") == "EURUSD=X"``."""
    return "%s%s=X" % (base.upper(), quote_ccy.upper())


def chart_url(symbol: str, interval: str = "1h", range_: str = "1mo") -> str:
    return CHART.format(symbol=quote(symbol, safe="="), interval=interval, range=range_)


def looks_like_chart(status: int, body: str) -> bool:
    """Acceptance test for a pool: a 200 that holds at least one bar."""
    return status == 200 and bool(parse_chart(body))


def parse_chart(body: str) -> list:
    """``[(utc datetime, close), ...]`` oldest first; bars without a close are skipped."""
    try:
        result = json.loads(body)["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (ValueError, KeyError, IndexError, TypeError):
        return []
    bars = []
    for ts, close in zip(stamps, closes):
        if ts is None or close is None:
            continue
        bars.append((datetime.fromtimestamp(int(ts), tz=timezone.utc), float(close)))
    return bars
