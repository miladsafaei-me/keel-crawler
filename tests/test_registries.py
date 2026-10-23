"""Tests for keel_crawler.registries: the two readable registers, parsed from fixed
answers, and the jurisdiction and number vocabulary. No network.

Run: python -m unittest tests.test_registries
"""
import unittest
from datetime import date
from unittest import mock

from keel_crawler.registries import (SEARCH_PAGES, RegistryError, Unsupported, jurisdiction_for,
                                     lookup, normalise_number)
from keel_crawler.registries import ares, companies_house

CH_PAGE = """<html><head><title>FIVE PERCENT ONLINE LTD overview</title></head><body>
<div id="content-container">
<h1 class="heading-xlarge" style="display: inline;">FIVE PERCENT ONLINE LTD</h1>
<dl><dt>Registered office address</dt><dd style="margin-top: 5px"><span class="text data" id="roa-address">
71-75 Shelton Street, Covent Garden, London, WC2H 9JQ
</span></dd></dl>
<dl class="column-two-thirds"><dt>Company status</dt><dd class="text data" id="company-status">
Active
</dd></dl>
<dl><dt>Company type</dt><dd id="company-type"><span class="text data" id="company-type-value">
Private limited Company
</span></dd></dl>
<dl class="column-third"><dt id="company-birth-type">Incorporated on</dt>
<dd class="data" id="company-creation-date">9 April 2020</dd></dl>
</div></body></html>"""

ARES_PAYLOAD = {
    "ico": "09213651", "obchodniJmeno": "FTMO Evaluation Global s.r.o.",
    "sidlo": {"textovaAdresa": "Purkyňova 2121/3, Nové Město, 11000 Praha 1"},
    "pravniForma": "112", "datumVzniku": "2020-06-02", "datumAktualizace": "2025-09-03",
}


CH_OFFICERS = """<html><body><h2 id="company-appointments">3 officers / 1 resignation</h2>
<div class="appointments-list">
<div class="appointment-1"><h2><span id="officer-name-1"><a href="/officers/x/appointments">BEN HUR, Gil</a></span></h2>
<dl><dt>Role <span id="officer-status-tag-1" class="status-tag">Active</span></dt><dd id="officer-role-1"> Director </dd></dl>
<dl><dt>Date of birth</dt><dd id="officer-date-of-birth-1">January 1972</dd></dl>
<dl><dt>Appointed on</dt><dd id="officer-appointed-on-1"> 30 August 2022 </dd></dl></div>
<div class="appointment-2"><h2><span id="officer-name-2"><a href="/officers/y/appointments">SMITH, Jane Anne</a></span></h2>
<dl><dt>Role <span id="officer-status-tag-2" class="status-tag">Resigned</span></dt><dd id="officer-role-2"> Secretary </dd></dl>
<dl><dt>Appointed on</dt><dd id="officer-appointed-on-2"> 9 April 2020 </dd></dl></div>
</div></body></html>"""

ARES_VR = {"zaznamy": [{"primarniZaznam": True, "statutarniOrgany": [{"clenoveOrganu": [
    {"datumZapisu": "2020-06-02", "datumVymazu": "2023-09-01",
     "clenstvi": {"funkce": {"vznikFunkce": "2020-06-02", "nazev": "jednatel"}},
     "fyzickaOsoba": {"jmeno": "OTAKAR", "prijmeni": "ŠUFFNER", "datumNarozeni": "1989-04-23"}},
    {"datumZapisu": "2023-09-01",
     "clenstvi": {"funkce": {"vznikFunkce": "2020-06-02", "nazev": "jednatel"}},
     "fyzickaOsoba": {"jmeno": "OTAKAR", "prijmeni": "ŠUFFNER", "datumNarozeni": "1989-04-23"}},
]}]}]}


class _Answer:
    def __init__(self, status_code, text="", payload=None):
        self.status_code, self.text, self._payload = status_code, text, payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class CompaniesHouseTests(unittest.TestCase):
    def test_parses_name_status_date_address_and_form(self):
        e = companies_house.parse(CH_PAGE, "12553363")
        self.assertEqual(e.name, "FIVE PERCENT ONLINE LTD")
        self.assertEqual(e.status, "active")
        self.assertEqual(e.raw_status, "Active")
        self.assertEqual(e.incorporated_on, date(2020, 4, 9))
        self.assertEqual(e.address, "71-75 Shelton Street, Covent Garden, London, WC2H 9JQ")
        self.assertEqual(e.legal_form, "Private limited Company")
        self.assertEqual(e.url, "https://find-and-update.company-information.service.gov.uk/company/12553363")
        self.assertEqual(e.register, "Companies House")

    def test_a_dissolved_company_reads_as_dissolved(self):
        page = CH_PAGE.replace(">\nActive\n<", ">\nDissolved\n<")
        self.assertEqual(companies_house.parse(page, "12553363").status, "dissolved")

    def test_a_page_that_is_not_an_overview_is_refused(self):
        with self.assertRaises(RegistryError):
            companies_house.parse("<html><body><h1>Sign in</h1></body></html>", "1")

    def test_404_means_no_such_company(self):
        session = mock.Mock()
        session.get.return_value = _Answer(404)
        self.assertIsNone(lookup("gb", "00000000", session=session))

    def test_lookup_pads_the_number_and_reads_the_page(self):
        session = mock.Mock()
        session.get.return_value = _Answer(200, CH_PAGE)
        e = lookup("gb", "12553363", session=session)
        self.assertEqual(e.number, "12553363")
        self.assertTrue(session.get.call_args_list[0].args[0].endswith("/company/12553363"))


class OfficerTests(unittest.TestCase):
    def test_companies_house_keeps_active_officers_in_a_readers_spelling(self):
        rows = companies_house.parse_officers(CH_OFFICERS)
        self.assertEqual(rows, [{"name": "Gil Ben Hur", "register_name": "BEN HUR, Gil", "role": "Director",
                                 "appointed_on": "2022-08-30"}])

    def test_no_birth_date_or_address_is_kept(self):
        rows = companies_house.parse_officers(CH_OFFICERS) + ares.parse_officers(ARES_VR)
        self.assertTrue(all(set(r) == {"name", "register_name", "role", "appointed_on"} for r in rows))

    def test_ares_keeps_the_current_member_once_and_names_the_role_in_english(self):
        rows = ares.parse_officers(ARES_VR)
        self.assertEqual(rows, [{"name": "Otakar Šuffner", "register_name": "OTAKAR ŠUFFNER",
                                 "role": "Executive director", "appointed_on": "2020-06-02"}])

    def test_lookup_reads_the_officers_beside_the_entry(self):
        session = mock.Mock()
        session.get.side_effect = [_Answer(200, CH_PAGE), _Answer(200, CH_OFFICERS)]
        e = lookup("gb", "12553363", session=session)
        self.assertEqual([o["name"] for o in e.officers], ["Gil Ben Hur"])
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("/company/12553363/officers"))

    def test_an_unreadable_officer_list_leaves_the_entry_standing(self):
        session = mock.Mock()
        session.get.side_effect = [_Answer(200, CH_PAGE), _Answer(503)]
        e = lookup("gb", "12553363", session=session)
        self.assertEqual(e.name, "FIVE PERCENT ONLINE LTD")
        self.assertEqual(e.officers, [])


class AresTests(unittest.TestCase):
    def test_parses_a_living_entity(self):
        e = ares.parse(ARES_PAYLOAD, "09213651")
        self.assertEqual(e.name, "FTMO Evaluation Global s.r.o.")
        self.assertEqual(e.status, "active")
        self.assertEqual(e.incorporated_on, date(2020, 6, 2))
        self.assertIsNone(e.dissolved_on)
        self.assertEqual(e.legal_form, "s.r.o.")
        self.assertEqual(e.address, "Purkyňova 2121/3, Nové Město, 11000 Praha 1")
        self.assertEqual(e.url, "https://ares.gov.cz/ekonomicke-subjekty?ico=09213651")

    def test_an_ended_entity_reads_as_dissolved(self):
        e = ares.parse({**ARES_PAYLOAD, "datumZaniku": "2024-01-31"}, "09213651")
        self.assertEqual(e.status, "dissolved")
        self.assertEqual(e.dissolved_on, date(2024, 1, 31))

    def test_lookup_reads_json_and_404_is_none(self):
        session = mock.Mock()
        session.get.return_value = _Answer(200, payload=ARES_PAYLOAD)
        self.assertEqual(lookup("cz", "9213651", session=session).number, "09213651")
        session.get.return_value = _Answer(404)
        self.assertIsNone(lookup("cz", "00000000", session=session))


class VocabularyTests(unittest.TestCase):
    def test_jurisdiction_from_free_text(self):
        self.assertEqual(jurisdiction_for("United Kingdom (England and Wales)"), "gb")
        self.assertEqual(jurisdiction_for("England and Wales"), "gb")
        self.assertEqual(jurisdiction_for("Czech Republic"), "cz")
        self.assertEqual(jurisdiction_for("Delaware, United States"), "us")
        self.assertEqual(jurisdiction_for("Ajman Free Zone, United Arab Emirates"), "ae")
        self.assertEqual(jurisdiction_for("Saint Lucia"), "lc")
        self.assertEqual(jurisdiction_for(""), "")

    def test_numbers_are_normalised_per_register(self):
        self.assertEqual(normalise_number("gb", "1255336"), "01255336")
        self.assertEqual(normalise_number("gb", "SC 123456"), "SC123456")
        self.assertEqual(normalise_number("cz", "9213651"), "09213651")
        self.assertEqual(normalise_number("au", "ACN 131 376 415"), "ACN131376415")

    def test_an_unreadable_jurisdiction_is_refused_and_has_a_search_page(self):
        with self.assertRaises(Unsupported):
            lookup("cy", "HE471627")
        self.assertIn("cy", SEARCH_PAGES)
        with self.assertRaises(Unsupported):
            lookup("", "1")


if __name__ == "__main__":
    unittest.main()
