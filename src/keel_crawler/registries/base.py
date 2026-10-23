"""The shape every register answers in, the dispatch, and the jurisdiction vocabulary."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import requests

USER_AGENT = "keel-crawler registry check (+https://github.com/miladsafaei-me/keel-crawler)"
TIMEOUT = 20

# The comparable status vocabulary. ``raw_status`` keeps the register's own words.
ACTIVE = "active"
DISSOLVED = "dissolved"
OTHER = "other"


class RegistryError(RuntimeError):
    """The register did not answer, or answered something that is not an entry."""


class Unsupported(RegistryError):
    """No register this package can read by number for that jurisdiction."""


@dataclass
class RegistryEntry:
    jurisdiction: str
    number: str
    name: str
    status: str
    raw_status: str = ""
    incorporated_on: date | None = None
    dissolved_on: date | None = None
    address: str = ""
    legal_form: str = ""
    url: str = ""
    register: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # The people the register names as the company's current officers (directors, the
    # statutory body), each ``{"name", "role", "appointed_on"}`` with the name as a reader
    # writes it and the register's own spelling in ``register_name``. Only what a reader
    # needs to know who runs the company: never a birth date or a home address, which the
    # registers publish and a consumer has no reason to repeat. Empty when the register's
    # officer list could not be read; the entry itself still stands.
    officers: list[dict[str, Any]] = field(default_factory=list)


# How a consumer's free-text country becomes a jurisdiction key. The first pattern that
# matches wins, so "United Kingdom (England and Wales)" is ``gb`` and "Delaware, United
# States" is ``us``.
_JURISDICTIONS: tuple[tuple[str, str], ...] = (
    (r"\b(united kingdom|england|wales|scotland|great britain|\buk\b)", "gb"),
    (r"\b(czech|czechia)", "cz"),
    (r"\bcyprus", "cy"),
    (r"\bhong kong", "hk"),
    (r"\bmalta", "mt"),
    (r"\bgibraltar", "gi"),
    (r"\bmauritius", "mu"),
    (r"\bliechtenstein", "li"),
    (r"\bswitzerland|\bswiss", "ch"),
    (r"\baustralia", "au"),
    (r"\bitaly", "it"),
    (r"\bcanada", "ca"),
    (r"\bnetherlands", "nl"),
    (r"\bisrael", "il"),
    (r"\b(united arab emirates|\buae\b|dubai|ajman|dmcc|free zone)", "ae"),
    (r"\b(united states|\busa?\b|delaware|wyoming|nevada|illinois|california|texas|florida)", "us"),
    (r"\bsaint lucia|\bst\.? lucia", "lc"),
    (r"\bsaint vincent|\bst\.? vincent", "vc"),
    (r"\bcomoros|\banjouan|\bmwali", "km"),
    (r"\bcayman", "ky"),
)

# A register's own search page, for a jurisdiction a script cannot read by number. A
# consumer may link a reader there; ``lookup`` does not use them.
SEARCH_PAGES: dict[str, tuple[str, str]] = {
    "cy": ("Cyprus Department of the Registrar of Companies",
           "https://efiling.drcor.mcit.gov.cy/DrcorPublic/SearchResults.aspx"),
    "hk": ("Hong Kong Companies Registry", "https://www.e-services.cr.gov.hk/ICRIS3EP/"),
    "mt": ("Malta Business Registry", "https://registry.mbr.mt/ROC/"),
    "gi": ("Companies House Gibraltar", "https://www.companieshouse.gi/"),
    "mu": ("Mauritius Corporate and Business Registration Department", "https://onlinesearch.mns.global/"),
    "li": ("Liechtenstein Handelsregister", "https://www.handelsregister.li/"),
    "ch": ("Swiss Zefix", "https://www.zefix.ch/"),
    "au": ("ASIC Connect", "https://connectonline.asic.gov.au/"),
    "ca": ("Corporations Canada", "https://ised-isde.canada.ca/cc/lgcy/fdrlCrpSrch.html"),
    "nl": ("KVK", "https://www.kvk.nl/en/search/"),
    "us": ("the state's Secretary of State", ""),
}


def jurisdiction_for(country: str) -> str:
    """The jurisdiction key for a consumer's free-text country, or ``""`` when none matches."""
    text = (country or "").strip().lower()
    for pattern, key in _JURISDICTIONS:
        if re.search(pattern, text):
            return key
    return ""


def normalise_number(jurisdiction: str, text: str) -> str:
    """The number as the register wants it: Companies House pads to eight characters and
    keeps a two-letter prefix; ARES wants eight digits. Other jurisdictions keep the
    consumer's spelling with the whitespace stripped."""
    raw = re.sub(r"\s+", "", str(text or "")).upper()
    if jurisdiction == "gb":
        digits = re.sub(r"\D", "", raw)
        prefix = re.match(r"^[A-Z]{1,2}", raw)
        if prefix and not raw.startswith(prefix.group(0) + digits):
            prefix = None
        if prefix:
            return prefix.group(0) + digits.zfill(8 - len(prefix.group(0)))
        return digits.zfill(8) if digits else ""
    if jurisdiction == "cz":
        digits = re.sub(r"\D", "", raw)
        return digits.zfill(8) if digits else ""
    return raw


def _session(session: requests.Session | None) -> requests.Session:
    if session is not None:
        return session
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _readers() -> dict[str, Callable[..., RegistryEntry | None]]:
    from . import ares, companies_house

    return {"gb": companies_house.read, "cz": ares.read}


SUPPORTED: tuple[str, ...] = ("gb", "cz")


def lookup(jurisdiction: str, number: str, *, session: requests.Session | None = None,
           timeout: float = TIMEOUT) -> RegistryEntry | None:
    """The register's entry for one company number, or ``None`` when the register says
    there is no such number. Raises ``Unsupported`` for a jurisdiction with no readable
    register and ``RegistryError`` when the register did not answer."""
    reader = _readers().get(jurisdiction)
    if reader is None:
        raise Unsupported(jurisdiction or "unknown")
    clean = normalise_number(jurisdiction, number)
    if not clean:
        raise RegistryError(f"no usable company number in {number!r}")
    return reader(clean, session=_session(session), timeout=timeout)
