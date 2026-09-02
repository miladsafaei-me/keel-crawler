"""Where free proxies come from, and how each list's format is read.

Diversity here is not tidiness, it is availability. Any single list can go
stale, rename a branch, rate-limit, or become unreachable from one network —
two of the candidates evaluated for this module (proxyscrape and geonode) are
simply unroutable from the machine it was written on, by curl as well as by
Python, so they would have been a silent single point of failure for anyone who
depended on them. Sixteen lists across nine independent publishers means a
harvest survives several of them being down without anyone noticing.

Every source is fetched independently at runtime: a list that fails, 404s or
returns nothing is skipped and the rest still load. Nothing here raises.

Formats seen in the wild, all handled by :func:`parse`:

* ``ip:port`` — the common case.
* ``scheme://ip:port`` — proxifly and others prefix the protocol.
* ``ip:port:Country`` — hideip.me appends metadata, which is a bonus rather
  than a nuisance: it is the only free source here that labels geography.
* JSON with a ``data`` array — geonode's shape, kept because the source may be
  reachable from other networks even though it is not from this one.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) keel-crawler/proxy-pool"

_IPPORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


@dataclass(frozen=True)
class Source:
    """One published proxy list."""

    name: str
    kind: str
    url: str
    publisher: str


# Verified reachable and productive on 2026-09-02. Grouped by publisher so the
# redundancy is visible: losing any one publisher costs at most two lists.
SOURCES: tuple[Source, ...] = (
    Source("speedx-http", "http",
           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "TheSpeedX"),
    Source("speedx-socks5", "socks5",
           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "TheSpeedX"),
    Source("proxifly-all", "http",
           "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
           "proxifly"),
    Source("sunny9577-http", "http",
           "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt",
           "sunny9577"),
    Source("jetkai-http", "http",
           "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
           "jetkai"),
    Source("jetkai-socks5", "socks5",
           "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
           "jetkai"),
    Source("monosans-http", "http",
           "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "monosans"),
    Source("monosans-socks5", "socks5",
           "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "monosans"),
    Source("vakhov-http", "http",
           "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt", "vakhov"),
    Source("vakhov-socks5", "socks5",
           "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt", "vakhov"),
    Source("hookzof-socks5", "socks5",
           "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "hookzof"),
    Source("prxchk-http", "http",
           "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt", "prxchk"),
    Source("prxchk-socks5", "socks5",
           "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt", "prxchk"),
    Source("roosterkid-socks5", "socks5",
           "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
           "roosterkid"),
    # The only free source that labels country, via a third colon-separated field.
    Source("hideip-http", "http",
           "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt", "hideip.me"),
    Source("hideip-socks5", "socks5",
           "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt", "hideip.me"),
)


def parse(text: str) -> list[tuple[str, str]]:
    """Read any of the published formats into (addr, country) pairs.

    Country is "" when the source does not say, which is all of them but one.
    """
    text = text.strip()
    if text.startswith("{"):
        try:
            rows = json.loads(text).get("data", [])
        except (ValueError, AttributeError):
            return []
        found = []
        for row in rows:
            ip, port = row.get("ip"), row.get("port")
            if ip and port:
                found.append((f"{ip}:{port}", (row.get("country") or "").strip()))
        return found

    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            line = line.split("://", 1)[1]
        # Split on colons before whitespace, not after: the country field can
        # contain a space ("United States"), so trimming at the first space
        # would silently truncate every multi-word country to its first word.
        fields = line.split(":")
        if len(fields) < 2:
            continue
        host = fields[0].strip()
        port = fields[1].split()[0].strip() if fields[1].split() else ""
        country = ":".join(fields[2:]).strip() if len(fields) > 2 else ""
        addr = f"{host}:{port}"
        if _IPPORT.match(addr):
            found.append((addr, country))
    return found


def fetch(source: Source, timeout: float = 25.0) -> list[tuple[str, str]]:
    """Pull and parse one list. Never raises — a dead source yields nothing."""
    try:
        request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - one dead list must not end a refresh
        return []


def fetch_all(sources: tuple[Source, ...] = SOURCES, timeout: float = 25.0,
              workers: int = 8) -> dict[str, dict]:
    """Every source at once, de-duplicated by address.

    A proxy published by several lists keeps every publisher's name. An address
    that independent lists agree on is better evidence than one appearing once,
    and the store uses that to decide what to check first.
    """
    from concurrent.futures import ThreadPoolExecutor

    merged: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for source, rows in zip(sources, pool.map(lambda s: fetch(s, timeout), sources)):
            for addr, country in rows:
                entry = merged.setdefault(
                    addr, {"addr": addr, "kind": source.kind, "country": country,
                           "publishers": set()}
                )
                entry["publishers"].add(source.publisher)
                if country and not entry["country"]:
                    entry["country"] = country
    for entry in merged.values():
        entry["publishers"] = sorted(entry["publishers"])
    return merged
