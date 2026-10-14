"""Official company registers, read by script (layer 6, 2026-10-14).

A consumer that names a legal entity (a broker, a prop firm, a publisher) can check that
entity against the official register of its own country and print what the register says:
the registered name, whether the company is active, when it was incorporated, where it is
seated, and the register's own page for it. The register decides only whether the entity
exists and since when; what the entity is called on the consumer's pages stays the
consumer's business, so nothing here knows what a firm is.

Two registers are readable without a login or a key today, and both were verified live on
2026-10-14 against real numbers (name matches, 404 on an unknown number):

- the United Kingdom's Companies House, as its public company page
  (``companies_house.py``; the REST API needs a free key and is not used);
- the Czech ARES register, as JSON (``ares.py``).

Every other register surveyed that day (Cyprus, Hong Kong, Malta, Gibraltar, Mauritius,
Liechtenstein, the US states, Canada, the Netherlands, the UAE free zones) offers a search
form and no page by number, and Saint Lucia, Saint Vincent and the Comoros offer nothing a
script can read. ``SEARCH_PAGES`` names a search page for those, so a consumer can still
send a reader to the register; ``lookup`` raises ``Unsupported`` for them.

    from keel_crawler.registries import lookup, normalise_number, jurisdiction_for
    entry = lookup("gb", "12553363")      # RegistryEntry or None when the register has no such number
"""
from .base import (SEARCH_PAGES, SUPPORTED, RegistryEntry, RegistryError, Unsupported,
                   jurisdiction_for, lookup, normalise_number)

__all__ = ["SEARCH_PAGES", "SUPPORTED", "RegistryEntry", "RegistryError", "Unsupported",
           "jurisdiction_for", "lookup", "normalise_number"]
