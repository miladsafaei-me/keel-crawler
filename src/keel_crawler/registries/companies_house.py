"""The United Kingdom's Companies House, read from its public company page.

``https://find-and-update.company-information.service.gov.uk/company/{number}`` answers
200 with the company's overview and 404 for a number it does not hold (verified
2026-10-14). The page is GOV.UK markup with stable ids: the name in the ``h1``, the status
in ``#company-status``, the incorporation date in ``#company-creation-date``, the
registered address in ``#roa-address``. The REST API needs a free
key and a quota, and the page carries the same fields, so the page is read.

The officers are read from the company's ``/officers`` page (2026-10-23): one
``appointment-N`` block per person with ``#officer-name-N`` ("SURNAME, Given"),
``#officer-role-N``, ``#officer-appointed-on-N`` and ``#officer-status-tag-N``
("Active" or "Resigned"). Only active appointments are kept.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser

import requests

from .base import ACTIVE, DISSOLVED, OTHER, RegistryEntry, RegistryError

PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}"
OFFICERS = PAGE + "/officers"
REGISTER = "Companies House"

_STATUS = {
    "active": ACTIVE,
    "dissolved": DISSOLVED,
}


class _Fields(HTMLParser):
    """Collects the text of the elements the overview page carries its facts in."""

    WANTED = {"company-status", "company-creation-date", "company-type-value", "company-dissolved-date",
              "roa-address"}

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self._into: str | None = None
        self._depth = 0
        self._h1: list[str] = []
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h1":
            self._in_h1 = True
        ident = a.get("id") or ""
        if ident in self.WANTED:
            self._into, self._depth = ident, 1
            self.fields.setdefault(ident, "")
        elif self._into:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False
        if self._into:
            self._depth -= 1
            if self._depth <= 0:
                self._into = None

    def handle_data(self, data):
        if self._in_h1:
            self._h1.append(data)
        if self._into:
            self.fields[self._into] += data

    @property
    def name(self) -> str:
        return _squash("".join(self._h1))

    @property
    def address(self) -> str:
        return _squash(self.fields.get("roa-address", ""))


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def _date(text: str) -> date | None:
    text = _squash(text)
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse(html: str, number: str) -> RegistryEntry:
    """The entry on one overview page. Raises ``RegistryError`` when the page is not one."""
    p = _Fields()
    p.feed(html)
    name = p.name
    raw_status = _squash(p.fields.get("company-status", ""))
    if not name or not raw_status:
        raise RegistryError("not a Companies House overview page")
    status = _STATUS.get(raw_status.lower().split()[0], OTHER)
    return RegistryEntry(
        jurisdiction="gb", number=number, name=name, status=status, raw_status=raw_status,
        incorporated_on=_date(p.fields.get("company-creation-date", "")),
        dissolved_on=_date(p.fields.get("company-dissolved-date", "")),
        address=p.address, legal_form=_squash(p.fields.get("company-type-value", "")),
        url=PAGE.format(number=number), register=REGISTER,
        raw={k: _squash(v) for k, v in p.fields.items()},
    )


class _Officers(HTMLParser):
    """Collects each appointment's name, role, appointment date and status by its id."""

    FIELD = re.compile(r"^officer-(name|role|appointed-on|status-tag)-(\d+)$")

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[int, dict[str, str]] = {}
        self._into: tuple[int, str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        m = self.FIELD.match(dict(attrs).get("id") or "")
        if m:
            self._into, self._depth = (int(m.group(2)), m.group(1)), 1
            self.rows.setdefault(int(m.group(2)), {}).setdefault(m.group(1), "")
        elif self._into:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._into:
            self._depth -= 1
            if self._depth <= 0:
                self._into = None

    def handle_data(self, data):
        if self._into:
            n, key = self._into
            self.rows[n][key] += data


def reader_name(register_name: str) -> str:
    """"BEN HUR, Gil" as a reader writes it: "Gil Ben Hur"."""
    surname, _, given = _squash(register_name).partition(",")
    surname = " ".join(w.capitalize() if w.isupper() else w for w in surname.split())
    return f"{given.strip()} {surname}".strip() if given else surname


def parse_officers(html: str) -> list[dict]:
    """The active officers on one ``/officers`` page, in the page's order."""
    p = _Officers()
    p.feed(html)
    out = []
    for n in sorted(p.rows):
        row = {k: _squash(v) for k, v in p.rows[n].items()}
        if not row.get("name") or row.get("status-tag", "").lower() != "active":
            continue
        appointed = _date(row.get("appointed-on", ""))
        out.append({"name": reader_name(row["name"]), "register_name": row["name"],
                    "role": row.get("role", ""), "appointed_on": appointed.isoformat() if appointed else None})
    return out


def read_officers(number: str, *, session: requests.Session, timeout: float) -> list[dict]:
    """The company's active officers, or an empty list when the page did not answer."""
    try:
        answer = session.get(OFFICERS.format(number=number), timeout=timeout)
    except requests.RequestException:
        return []
    if answer.status_code != 200:
        return []
    return parse_officers(answer.text)


def read(number: str, *, session: requests.Session, timeout: float) -> RegistryEntry | None:
    url = PAGE.format(number=number)
    try:
        answer = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise RegistryError(f"Companies House did not answer: {exc}") from exc
    if answer.status_code == 404:
        return None
    if answer.status_code != 200:
        raise RegistryError(f"Companies House answered {answer.status_code}")
    entry = parse(answer.text, number)
    entry.officers = read_officers(number, session=session, timeout=timeout)
    return entry
