"""The Czech ARES register (Administrativní registr ekonomických subjektů), as JSON.

``https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}`` answers
the entity with no key or login and 404 for an unknown ICO (verified 2026-10-14). The
fields read: ``obchodniJmeno`` (name), ``datumVzniku`` (founded), ``datumZaniku`` (ended,
absent while the entity lives), ``sidlo.textovaAdresa`` (seat), ``pravniForma`` (legal
form code). The human page is a JavaScript app at ``https://ares.gov.cz/ekonomicke-
subjekty?ico={ico}``; it is the link a reader gets.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import requests

from .base import ACTIVE, DISSOLVED, RegistryEntry, RegistryError

API = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
PAGE = "https://ares.gov.cz/ekonomicke-subjekty?ico={ico}"
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
    return parse(payload, ico)
