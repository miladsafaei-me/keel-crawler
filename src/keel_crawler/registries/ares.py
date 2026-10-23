"""The Czech ARES register (Administrativní registr ekonomických subjektů), as JSON.

``https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}`` answers
the entity with no key or login and 404 for an unknown ICO (verified 2026-10-14). The
fields read: ``obchodniJmeno`` (name), ``datumVzniku`` (founded), ``datumZaniku`` (ended,
absent while the entity lives), ``sidlo.textovaAdresa`` (seat), ``pravniForma`` (legal
form code). The human page is a JavaScript app at ``https://ares.gov.cz/ekonomicke-
subjekty?ico={ico}``; it is the link a reader gets.

The statutory body comes from the commercial-register view of the same entity,
``.../ekonomicke-subjekty-vr/{ico}`` (2026-10-23): ``zaznamy[0].statutarniOrgany[]``, each
with ``clenoveOrganu[]`` whose ``fyzickaOsoba`` (``jmeno``, ``prijmeni``) or
``pravnickaOsoba`` (``obchodniJmeno``) is the member, ``clenstvi.funkce.nazev`` the role
and ``datumVymazu`` the day a former member was struck off. Only current members are kept.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import requests

from .base import ACTIVE, DISSOLVED, RegistryEntry, RegistryError

API = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
PAGE = "https://ares.gov.cz/ekonomicke-subjekty?ico={ico}"
COMMERCIAL_REGISTER = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-vr/{ico}"

# The Czech role words a trading firm's statutory body carries, in English beside them.
ROLES = {"jednatel": "Executive director", "předseda představenstva": "Chair of the board",
         "místopředseda představenstva": "Vice-chair of the board", "člen představenstva": "Board member",
         "předseda správní rady": "Chair of the administrative board", "člen správní rady": "Board member",
         "statutární ředitel": "Statutory director"}
REGISTER = "ARES"

# The legal-form codes a reader meets on a trading firm; the rest print their code.
LEGAL_FORMS = {"112": "s.r.o.", "121": "a.s.", "101": "fyzická osoba", "421": "odštěpný závod"}


def _date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def parse(payload: dict[str, Any], ico: str) -> RegistryEntry:
    name = str(payload.get("obchodniJmeno") or "").strip()
    if not name:
        raise RegistryError("not an ARES entity")
    ended = _date(payload.get("datumZaniku"))
    form = str(payload.get("pravniForma") or "")
    return RegistryEntry(
        jurisdiction="cz", number=ico, name=name,
        status=DISSOLVED if ended else ACTIVE, raw_status="zaniklý" if ended else "aktivní",
        incorporated_on=_date(payload.get("datumVzniku")), dissolved_on=ended,
        address=str((payload.get("sidlo") or {}).get("textovaAdresa") or "").strip(),
        legal_form=LEGAL_FORMS.get(form, form), url=PAGE.format(ico=ico), register=REGISTER,
        raw={k: payload.get(k) for k in ("obchodniJmeno", "datumVzniku", "datumZaniku", "pravniForma", "datumAktualizace")},
    )


def _person_name(member: dict[str, Any]) -> tuple[str, str]:
    person = member.get("fyzickaOsoba") or {}
    if person:
        given = str(person.get("jmeno") or "").strip()
        family = str(person.get("prijmeni") or "").strip()
        register_name = f"{given} {family}".strip()
        display = " ".join(w.capitalize() if w.isupper() else w for w in register_name.split())
        return display, register_name
    company = str((member.get("pravnickaOsoba") or {}).get("obchodniJmeno") or "").strip()
    return company, company


def parse_officers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The current members of the entity's statutory body."""
    records = payload.get("zaznamy") or []
    record = next((r for r in records if r.get("primarniZaznam")), records[0] if records else {})
    out = []
    for body in record.get("statutarniOrgany") or []:
        for member in body.get("clenoveOrganu") or []:
            if member.get("datumVymazu"):
                continue
            name, register_name = _person_name(member)
            if not name:
                continue
            raw_role = str(((member.get("clenstvi") or {}).get("funkce") or {}).get("nazev") or "").strip()
            role = ROLES.get(raw_role.lower(), raw_role or str(member.get("nazevAngazma") or ""))
            since = _date(((member.get("clenstvi") or {}).get("funkce") or {}).get("vznikFunkce")
                          or member.get("datumZapisu"))
            out.append({"name": name, "register_name": register_name, "role": role,
                        "appointed_on": since.isoformat() if since else None})
    return out


def read_officers(ico: str, *, session: requests.Session, timeout: float) -> list[dict[str, Any]]:
    """The current statutory body, or an empty list when the register view did not answer."""
    try:
        answer = session.get(COMMERCIAL_REGISTER.format(ico=ico), timeout=timeout,
                             headers={"Accept": "application/json"})
        if answer.status_code != 200:
            return []
        return parse_officers(answer.json())
    except (requests.RequestException, ValueError):
        return []


def read(ico: str, *, session: requests.Session, timeout: float) -> RegistryEntry | None:
    try:
        answer = session.get(API.format(ico=ico), timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        raise RegistryError(f"ARES did not answer: {exc}") from exc
    if answer.status_code == 404:
        return None
    if answer.status_code != 200:
        raise RegistryError(f"ARES answered {answer.status_code}")
    try:
        payload = answer.json()
    except ValueError as exc:
        raise RegistryError("ARES answered something that is not JSON") from exc
    entry = parse(payload, ico)
    entry.officers = read_officers(ico, session=session, timeout=timeout)
    return entry
