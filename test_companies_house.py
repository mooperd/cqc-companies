#!/usr/bin/env python3
"""Offline tests for the Companies House client parsing + pagination.

Exercises everything that doesn't need a live API key, against a fixture
modelled on a real /officers response. The live-API exit criterion
(`docs/plans/companies-house-enrichment.md` WS1) needs a real key — run:

    COMPANIES_HOUSE_API_KEY=... python -m companies_house officers 02518546

Run these offline tests with: python test_companies_house.py
"""

import datetime as dt

import companies_house as ch

# A representative /officers page: one active director, one resigned director,
# one active secretary — enough to check date parsing, the active/resigned
# distinction, and that role filtering is left to the caller.
_FIXTURE_PAGE = {
    "total_results": 3,
    "items_per_page": 50,
    "start_index": 0,
    "items": [
        {
            "name": "SMITH, Jane Alice",
            "officer_role": "director",
            "appointed_on": "2018-04-01",
        },
        {
            "name": "JONES, Robert",
            "officer_role": "director",
            "appointed_on": "2010-06-15",
            "resigned_on": "2021-09-30",
        },
        {
            "name": "ACME SECRETARIES LIMITED",
            "officer_role": "secretary",
            "appointed_on": "2009-01-02",
        },
    ],
}


def test_parse_officers_payload():
    officers = ch.parse_officers_payload(_FIXTURE_PAGE)
    assert len(officers) == 3, officers

    jane, robert, secretary = officers
    assert jane.name == "SMITH, Jane Alice"
    assert jane.role == "director"
    assert jane.appointed_on == dt.date(2018, 4, 1)
    assert jane.resigned_on is None
    assert jane.is_active is True

    assert robert.resigned_on == dt.date(2021, 9, 30)
    assert robert.is_active is False, "resigned officer must not be active"

    assert secretary.role == "secretary"
    assert secretary.is_active is True
    print("OK — parse_officers_payload: roles, dates, active/resigned distinction")


def test_parse_date():
    assert ch._parse_date(None) is None
    assert ch._parse_date("") is None
    assert ch._parse_date("2024-12-31") == dt.date(2024, 12, 31)
    print("OK — _parse_date")


def test_fetch_officers_paginates():
    # Two pages of 50 + 1, total 51 — fetch_officers must follow pagination.
    page1 = {
        "total_results": 51,
        "items": [
            {"name": f"D{i}", "officer_role": "director", "appointed_on": "2020-01-01"}
            for i in range(50)
        ],
    }
    page2 = {
        "total_results": 51,
        "items": [
            {
                "name": "OLD, Director",
                "officer_role": "director",
                "appointed_on": "2005-01-01",
                "resigned_on": "2019-01-01",
            }
        ],
    }
    calls = []

    def fake_get_json(path, _api_key):
        calls.append(path)
        return page1 if "start_index=0" in path else page2

    original = ch._get_json
    ch._get_json = fake_get_json
    try:
        officers = ch.fetch_officers("00000000", api_key="dummy")
        assert len(officers) == 51, f"expected 51, got {len(officers)}"
        assert len(calls) == 2, f"expected 2 pages, got {len(calls)}"

        active = ch.fetch_officers("00000000", api_key="dummy", active_only=True)
        assert len(active) == 50, f"active_only should drop the resigned one: {len(active)}"
    finally:
        ch._get_json = original
    print("OK — fetch_officers: pagination + active_only filter")


def test_resolve_api_key_missing():
    import os

    saved = os.environ.pop(ch.API_KEY_ENV, None)
    try:
        try:
            ch.resolve_api_key(None)
            assert False, "resolve_api_key must raise when no key is set"
        except RuntimeError:
            pass
        assert ch.resolve_api_key("explicit-key") == "explicit-key"
    finally:
        if saved is not None:
            os.environ[ch.API_KEY_ENV] = saved
    print("OK — resolve_api_key: raises when missing, honours explicit key")


if __name__ == "__main__":
    test_parse_officers_payload()
    test_parse_date()
    test_fetch_officers_paginates()
    test_resolve_api_key_missing()
    print("\nAll Companies House offline tests passed.")
