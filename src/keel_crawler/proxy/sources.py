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
* JSON with a ``data`` array — geonode's shape, which also carries the country
  and the protocol per row.
* An HTML table of ``<td>ip</td><td>port</td>`` — several sites publish nothing
  else, and one GET plus a regex is cheaper than admitting a browser here.

**A list that still downloads is not a list that is still maintained.** Two of
the nine publishers this module started with had stopped committing — jetkai in
April 2023, prxchk in April 2024 — and nothing ever failed, because their files
are still served. Measured 2026-09-04, they published 2,056 addresses no other
source had, 23.5% of the whole candidate pool, of which **2.8%** would even
accept a TCP connection against 42.5% for everything else. They are gone from
the list below, and the lesson is that a source is judged by its last commit
date, not by whether the URL answers.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import NamedTuple

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) keel-crawler/proxy-pool"

_IPPORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


@dataclass(frozen=True)
class Source:
    """One published proxy list."""

    name: str
    kind: str
    url: str
    publisher: str


# Verified reachable and productive on 2026-09-04: every URL fetched, parsed and
# counted, with the addresses it publishes that no other source does. Grouped by
# publisher so the redundancy is visible, and ordered within a group by what the
# measurement said each file was worth. The full table, including what was
# rejected and why, is in docs/proxy-sources.md.
#
# GitHub raw text and JSON. These are reachable from anywhere, including a laptop.
GITHUB_SOURCES: tuple[Source, ...] = (
    Source("speedx-http", "http",
           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "TheSpeedX"),
    Source("speedx-socks5", "socks5",
           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "TheSpeedX"),
    Source("speedx-socks4", "socks4",
           "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "TheSpeedX"),
    Source("tuanminpay-http", "http",
           "https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/http.txt", "TuanMinPay"),
    Source("tuanminpay-socks5", "socks5",
           "https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/socks5.txt", "TuanMinPay"),
    Source("xyzs996-http", "http",
           "https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/http.txt",
           "xyzs996"),
    Source("xyzs996-socks5", "socks5",
           "https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/socks5.txt",
           "xyzs996"),
    Source("dpangestuw-http", "http",
           "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt",
           "dpangestuw"),
    Source("dpangestuw-socks5", "socks5",
           "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt",
           "dpangestuw"),
    Source("proxifly-all", "http",
           "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
           "proxifly"),
    Source("sunny9577-http", "http",
           "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt",
           "sunny9577"),
    Source("sunny9577-socks5", "socks5",
           "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt",
           "sunny9577"),
    Source("sunny9577-socks4", "socks4",
           "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt",
           "sunny9577"),
    Source("monosans-http", "http",
           "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "monosans"),
    Source("monosans-socks5", "socks5",
           "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "monosans"),
    # Scheme-prefixed and genuinely mixed: the protocol is read per line, not
    # taken from this entry.
    Source("monosans-all", "http",
           "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt", "monosans"),
    Source("aliilapro-http", "http",
           "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt", "ALIILAPRO"),
    Source("aliilapro-socks5", "socks5",
           "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt", "ALIILAPRO"),
    Source("aliilapro-socks4", "socks4",
           "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt", "ALIILAPRO"),
    Source("mrmarble-all", "http",
           "https://raw.githubusercontent.com/MrMarble/proxy-list/main/all.txt", "MrMarble"),
    # Published as already-checked subsets rather than raw scrapes.
    Source("nikolait-http", "http",
           "https://raw.githubusercontent.com/NikolaiT/free-proxy-list/main/proxies/http_working.txt",
           "NikolaiT"),
    Source("nikolait-socks5", "socks5",
           "https://raw.githubusercontent.com/NikolaiT/free-proxy-list/main/proxies/socks5_working.txt",
           "NikolaiT"),
    Source("nikolait-socks4", "socks4",
           "https://raw.githubusercontent.com/NikolaiT/free-proxy-list/main/proxies/socks4_working.txt",
           "NikolaiT"),
    Source("elliott-mix-checked", "http",
           "https://raw.githubusercontent.com/elliottophellia/proxylist/master/results/mix_checked.txt",
           "elliottophellia"),
    Source("zaeem20-http", "http",
           "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt", "Zaeem20"),
    Source("zaeem20-socks5", "socks5",
           "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt", "Zaeem20"),
    Source("zaeem20-socks4", "socks4",
           "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt", "Zaeem20"),
    Source("vakhov-http", "http",
           "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt", "vakhov"),
    Source("vakhov-socks5", "socks5",
           "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt", "vakhov"),
    Source("hookzof-socks5", "socks5",
           "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "hookzof"),
    Source("roosterkid-socks5", "socks5",
           "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
           "roosterkid"),
    Source("roosterkid-socks4", "socks4",
           "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt",
           "roosterkid"),
    Source("themiralay-all", "http",
           "https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt",
           "themiralay"),
    Source("thordata-highanon", "http",
           "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/high-anon.txt",
           "Thordata"),
    Source("thordata-stable", "http",
           "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/stable.txt",
           "Thordata"),
    # The three files that label country. connect.txt is the largest of them and
    # was published for years before anyone here read it.
    Source("hideip-connect", "http",
           "https://raw.githubusercontent.com/zloi-user/hideip.me/main/connect.txt", "hideip.me"),
    Source("hideip-https", "http",
           "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt", "hideip.me"),
    Source("hideip-http", "http",
           "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt", "hideip.me"),
    Source("hideip-socks5", "socks5",
           "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt", "hideip.me"),
    Source("watchttvv-socks5", "socks5",
           "https://raw.githubusercontent.com/watchttvv/free-proxy-list/main/proxy.txt", "watchttvv"),
)

# Sites and keyless APIs. **These are not reachable from every network.** Measured
# 2026-09-04, essentially every proxy-list domain fails the TLS handshake at SNI
# from one laptop while google.com and github.com answer normally from the same
# shell; the same URLs answer 200 from the production harvest host. So a source
# here that returns nothing locally has not been proven dead - which is exactly
# the mistake that recorded proxyscrape and geonode as "unroutable" when this
# module was written. Judge them from the machine that will do the harvesting.
WEB_SOURCES: tuple[Source, ...] = (
    # Six pages of 500. The only source that labels country for every row and
    # names the protocol, so it is worth the six requests.
    *(Source(f"geonode-p{page}", "http",
             "https://proxylist.geonode.com/api/proxy-list?limit=500&page="
             f"{page}&sort_by=lastChecked&sort_type=desc", "geonode")
      for page in range(1, 7)),
    Source("proxyscrape-v4", "http",
           "https://api.proxyscrape.com/v4/free-proxy-list/get"
           "?request=display_proxies&proxy_format=protocolipport&format=text", "proxyscrape"),
    Source("spysme-http", "http", "https://spys.me/proxy.txt", "spys.me"),
    Source("spysme-socks", "socks5", "https://spys.me/socks.txt", "spys.me"),
    Source("socks-proxy-net", "socks5", "https://www.socks-proxy.net/", "free-proxy-list.net"),
    Source("free-proxy-list-net", "http", "https://free-proxy-list.net/", "free-proxy-list.net"),
    Source("hide-mn", "http", "https://hide.mn/en/proxy-list/", "hide.mn"),
    Source("flamingoproxies", "http", "https://flamingoproxies.com/free-proxies",
           "flamingoproxies"),
    Source("premiumproxy", "http", "https://premiumproxy.net/", "premiumproxy"),
    Source("freeproxyupdate", "http", "https://freeproxyupdate.com/", "freeproxyupdate"),
)

SOURCES: tuple[Source, ...] = GITHUB_SOURCES + WEB_SOURCES


# A protocol named on the line itself beats the one the source is filed under: a
# "mixed" list carries all three, and calling a socks5 address http means every
# request through it fails for a reason no log explains.
_SCHEMES = {"http": "http", "https": "http", "socks4": "socks4",
            "socks4a": "socks4", "socks5": "socks5", "socks5h": "socks5"}

_TABLE_ROW = re.compile(
    r"<td[^>]*>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td[^>]*>\s*(\d{2,5})\s*</td>"
    r"(?:\s*<td[^>]*>\s*([A-Za-z]{2})\s*</td>)?",
    re.I)


class Row(NamedTuple):
    """One address as a source published it."""

    addr: str
    country: str = ""
    kind: str = "http"


def _rows_from_json(text: str, default_kind: str) -> list[Row]:
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip, port = row.get("ip"), row.get("port")
        if not (ip and port):
            continue
        protocols = row.get("protocols") or []
        kind = _SCHEMES.get(str(protocols[0]).lower(), default_kind) if protocols else default_kind
        found.append(Row(f"{ip}:{port}", (row.get("country") or "").strip(), kind))
    return found


def _rows_from_html(text: str, default_kind: str) -> list[Row]:
    """Read the ``<td>ip</td><td>port</td>`` tables several sites publish.

    A regex rather than a parser, and a browser least of all: these tables are
    server-rendered and two cells wide at the point that matters, so the cheap
    read is also the complete one. A site that changes shape yields nothing,
    which is the same outcome as a site that goes down and is handled the same
    way — skipped, with the rest of the refresh unaffected.
    """
    found = []
    for ip, port, code in _TABLE_ROW.findall(text):
        found.append(Row(f"{ip}:{port}", (code or "").upper(), default_kind))
    return found


def parse(text: str, default_kind: str = "http") -> list[Row]:
    """Read any of the published formats into rows.

    ``country`` is "" when the source does not say, which is most of them, and
    ``kind`` falls back to the source's own protocol when the line does not name
    one.
    """
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return _rows_from_json(text, default_kind)
    if "<td" in text[:200000].lower():
        return _rows_from_html(text, default_kind)

    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind = default_kind
        if "://" in line:
            scheme, _, line = line.partition("://")
            kind = _SCHEMES.get(scheme.strip().lower(), default_kind)
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
            found.append(Row(addr, country, kind))
    return found


def decode(raw: bytes) -> str:
    """Text from a published list, trying UTF-8 first and Windows-1252 second.

    None of these lists sends a usable charset, and a few serve country names in
    Windows-1252. Decoding those as UTF-8 with ``errors="replace"`` does not
    merely mangle a glyph, it destroys the label: "Türkiye" becomes "T\ufffdrkiye",
    which then matches no country name and is stored as a country of its own.
    Fourteen addresses were held under exactly that label before this existed.
    """
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def fetch(source: Source, timeout: float = 25.0) -> list[Row]:
    """Pull and parse one list. Never raises — a dead source yields nothing."""
    try:
        request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse(decode(response.read()), source.kind)
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
            for addr, country, kind in rows:
                entry = merged.setdefault(
                    addr, {"addr": addr, "kind": kind, "country": country,
                           "publishers": set()}
                )
                entry["publishers"].add(source.publisher)
                # Take the new label when there is none, and also when the one
                # held does not resolve to a country while the new one does.
                # Without the second half, a label corrupted before `decode`
                # existed - or simply misspelled by one publisher - is kept
                # forever and no later refresh can repair it.
                if country and (not entry["country"]
                                or (not normalize_country(entry["country"])
                                    and normalize_country(country))):
                    entry["country"] = country
    for entry in merged.values():
        entry["publishers"] = sorted(entry["publishers"])
    return merged


# Where an address actually is. Only a handful of the lists label country, so
# the rest are resolved here. ip-api.com takes 100 addresses per call, needs no
# key, and allows 45 calls a minute - and because an address does not move, a
# lookup is paid once ever and then lives in the store.
GEO_ENDPOINT = "http://ip-api.com/batch?fields=query,countryCode,status"
GEO_BATCH = 100


def geolocate(ips, timeout: float = 20.0, batch: int = GEO_BATCH) -> dict:
    """Map addresses to ISO country codes. Never raises; unknown ones are absent.

    Country matters here because the endpoints these proxies are pointed at
    answer differently depending on where the request comes from, so a result
    without its country is a result nobody can interpret.
    """
    import json
    import urllib.request

    found: dict[str, str] = {}
    ips = [ip for ip in dict.fromkeys(ips) if ip]
    for start in range(0, len(ips), batch):
        chunk = ips[start:start + batch]
        try:
            request = urllib.request.Request(
                GEO_ENDPOINT, data=json.dumps(chunk).encode(),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                rows = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a country label is never worth failing a run
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("status") == "success":
                code = (row.get("countryCode") or "").strip()
                if code:
                    found[row.get("query", "")] = code
    return found


# Country names as the published lists write them, mapped to ISO codes. The
# lists and the geolocation service disagree on form - one says "United States",
# the other "US" - and left alone that splits a single country into two labels
# nobody can group by. Measured on one harvest: 78 distinct labels for about 50
# countries, with United States/US and France/FR each appearing twice.
#
# Every name below was observed in real data; the rest are common enough to be
# worth having before they are.
COUNTRY_CODES = {
    "united states": "US", "united kingdom": "GB", "hong kong": "HK",
    "singapore": "SG", "france": "FR", "saudi arabia": "SA", "netherlands": "NL",
    "the netherlands": "NL", "kosovo": "XK", "switzerland": "CH",
    "south korea": "KR", "korea": "KR", "indonesia": "ID", "russia": "RU",
    "russian federation": "RU", "philippines": "PH", "india": "IN",
    "united arab emirates": "AE", "sweden": "SE", "vietnam": "VN",
    "viet nam": "VN", "thailand": "TH", "colombia": "CO", "south africa": "ZA",
    "canada": "CA", "mexico": "MX", "japan": "JP", "serbia": "RS",
    "argentina": "AR", "tanzania": "TZ", "kenya": "KE", "iraq": "IQ",
    "kazakhstan": "KZ", "ecuador": "EC", "dominican republic": "DO",
    "chile": "CL", "lithuania": "LT", "finland": "FI", "malaysia": "MY",
    "syria": "SY", "montenegro": "ME", "spain": "ES", "peru": "PE",
    "cambodia": "KH", "egypt": "EG", "albania": "AL", "venezuela": "VE",
    "bangladesh": "BD", "libya": "LY", "germany": "DE", "brazil": "BR",
    "china": "CN", "turkey": "TR", "türkiye": "TR", "italy": "IT",
    "poland": "PL", "ukraine": "UA", "iran": "IR", "pakistan": "PK",
    "nigeria": "NG", "czechia": "CZ", "czech republic": "CZ", "romania": "RO",
    "bulgaria": "BG", "israel": "IL", "australia": "AU", "new zealand": "NZ",
    "norway": "NO", "denmark": "DK", "austria": "AT", "belgium": "BE",
    "portugal": "PT", "greece": "GR", "hungary": "HU", "ireland": "IE",
    "taiwan": "TW", "nepal": "NP", "sri lanka": "LK", "myanmar": "MM",
    "morocco": "MA", "algeria": "DZ", "tunisia": "TN", "ghana": "GH",
    "uganda": "UG", "ethiopia": "ET", "sudan": "SD", "yemen": "YE",
    "jordan": "JO", "lebanon": "LB", "kuwait": "KW", "qatar": "QA",
    "bahrain": "BH", "oman": "OM", "uzbekistan": "UZ", "azerbaijan": "AZ",
    "georgia": "GE", "armenia": "AM", "belarus": "BY", "moldova": "MD",
    "latvia": "LV", "estonia": "EE", "slovakia": "SK", "slovenia": "SI",
    "croatia": "HR", "bosnia and herzegovina": "BA", "north macedonia": "MK",
    "cyprus": "CY", "malta": "MT", "iceland": "IS", "luxembourg": "LU",
    "bolivia": "BO", "paraguay": "PY", "uruguay": "UY", "costa rica": "CR",
    "panama": "PA", "guatemala": "GT", "honduras": "HN", "el salvador": "SV",
    "nicaragua": "NI", "cuba": "CU", "jamaica": "JM", "puerto rico": "PR",
    # Observed in the store on 2026-09-03, each one losing its country because
    # the map did not carry the name the list publishes.
    "seychelles": "SC", "kyrgyzstan": "KG", "afghanistan": "AF",
    "palestine": "PS", "papua new guinea": "PG", "zimbabwe": "ZW",
    "british virgin islands": "VG", "somalia": "SO", "gabon": "GA",
    "belize": "BZ",
    # The long ISO-3166 forms, which geolocation services return where the
    # published lists use the short name. A comma or a parenthesis is enough to
    # miss the entry and drop the country.
    "korea, republic of": "KR", "korea, democratic people's republic of": "KP",
    "iran (islamic republic of)": "IR", "iran, islamic republic of": "IR",
    "russian federation, the": "RU", "syrian arab republic": "SY",
    "tanzania, united republic of": "TZ", "bolivia (plurinational state of)": "BO",
    "venezuela (bolivarian republic of)": "VE", "moldova, republic of": "MD",
    "viet nam, socialist republic of": "VN", "lao people's democratic republic": "LA",
    "taiwan, province of china": "TW", "macao": "MO", "macau": "MO",
    "côte d'ivoire": "CI", "cote d'ivoire": "CI", "curaçao": "CW",
    "congo, the democratic republic of the": "CD", "congo": "CG",
    "united states of america": "US", "great britain": "GB",
}


def normalize_country(label: str) -> str:
    """One country, one label: an ISO-3166 alpha-2 code, or "" if unrecognised.

    Applied wherever a country is read rather than only where it is written, so
    data already collected under the older mixed labels normalises on the way out
    instead of needing to be gathered again.
    """
    label = (label or "").strip()
    if len(label) == 2 and label.isalpha():
        return label.upper()
    return COUNTRY_CODES.get(label.lower(), "")
