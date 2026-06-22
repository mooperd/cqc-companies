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

    # DOB + nationality (the correlation signals) parse when present.
    with_dob = ch.parse_officers_payload({"items": [{
        "name": "KHAN, Asam Tazeem", "officer_role": "director",
        "appointed_on": "2017-08-01",
        "date_of_birth": {"month": 2, "year": 1982}, "nationality": "British",
    }]})[0]
    assert with_dob.dob_year == 1982 and with_dob.dob_month == 2
    assert with_dob.nationality == "British"
    print("OK — parse_officers_payload: roles, dates, DOB, active/resigned distinction")


def test_parse_psc_payload():
    page = {"items": [
        {
            "name": "Mr Asam Khan",
            "kind": "individual-person-with-significant-control",
            "natures_of_control": ["ownership-of-shares-25-to-50-percent",
                                   "voting-rights-25-to-50-percent"],
            "notified_on": "2017-08-01",
            "date_of_birth": {"month": 2, "year": 1982}, "nationality": "British",
        },
        {
            "name": "Rss Global Limited",
            "kind": "corporate-entity-person-with-significant-control",
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
            "notified_on": "2023-03-03", "ceased_on": "2023-03-03",
        },
    ]}
    pscs = ch.parse_psc_payload(page)
    assert len(pscs) == 2
    asam, rss = pscs
    assert asam.kind == "individual-person-with-significant-control"
    assert asam.natures_of_control == ("ownership-of-shares-25-to-50-percent",
                                       "voting-rights-25-to-50-percent")
    assert asam.dob_year == 1982 and asam.dob_month == 2 and asam.nationality == "British"
    assert asam.is_active is True
    assert rss.ceased_on == dt.date(2023, 3, 3) and rss.is_active is False
    print("OK — parse_psc_payload: kind, natures, DOB, active/ceased distinction")


def test_filing_history_parse_and_latest():
    page = {"items": [
        {"category": "accounts", "date": "2024-09-30"},
        {"category": "officers", "date": "2024-03-15"},
        {"category": "confirmation-statement", "date": "2024-02-01"},
        {"category": "persons-with-significant-control", "date": "2024-05-20"},
        {"category": "officers", "date": "2023-01-01"},
    ]}
    entries = ch.parse_filing_history_payload(page)
    assert len(entries) == 5
    # The watermark is the newest officer/PSC filing — ignoring accounts (which
    # is more recent but irrelevant) and confirmation statements.
    assert ch.latest_relevant_filing_date(entries) == dt.date(2024, 5, 20)
    # A company with no officer/PSC filings → no watermark.
    only_accounts = ch.parse_filing_history_payload(
        {"items": [{"category": "accounts", "date": "2024-09-30"}]})
    assert ch.latest_relevant_filing_date(only_accounts) is None
    assert ch.latest_relevant_filing_date([]) is None
    print("OK — filing history: parse + latest officer/PSC date (ignores accounts)")


def test_fetch_filing_history_paginates_and_404():
    page1 = {"items": [{"category": "officers", "date": "2024-01-01"}] * 50}
    page2 = {"items": [{"category": "officers", "date": "2024-06-01"}]}
    calls = []

    def fake_get_json(path, _key):
        calls.append(path)
        return page1 if "start_index=0" in path else page2

    original = ch._get_json
    ch._get_json = fake_get_json
    try:
        entries = ch.fetch_filing_history("00000000", api_key="dummy")
        assert len(entries) == 51 and len(calls) == 2
        assert ch.latest_relevant_filing_date(entries) == dt.date(2024, 6, 1)
    finally:
        ch._get_json = original

    # A 404 (unknown/dissolved company) propagates so the caller can skip it.
    def raise_404(_path, _key):
        raise ch.CompaniesHouseError("not found", status=404)

    ch._get_json = raise_404
    try:
        ch.fetch_filing_history("99999999", api_key="dummy")
        assert False, "404 must propagate from fetch_filing_history"
    except ch.CompaniesHouseError as err:
        assert err.status == 404
    finally:
        ch._get_json = original
    print("OK — fetch_filing_history: paginates; 404 propagates as the cheap-gate skip")


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


def _clear_ch_env():
    """Remove every Companies House env var and return them for restoration."""
    import os

    names = [ch.ENV_VAR, ch.API_KEY_ENV, *ch._KEY_VARS.values()]
    return {name: os.environ.pop(name, None) for name in names}


def _restore_env(saved):
    import os

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_get_json_retries_5xx():
    import io
    import urllib.error

    calls = []

    def fake_urlopen(req, timeout=60):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)
        return io.BytesIO(b'{"items": []}')

    orig_open, orig_sleep = ch.urllib.request.urlopen, ch.time.sleep
    ch.urllib.request.urlopen = fake_urlopen
    ch.time.sleep = lambda _s: None
    try:
        assert ch._get_json("/x", "key") == {"items": []}
        assert len(calls) == 2, "should retry the 502 once then succeed"
    finally:
        ch.urllib.request.urlopen = orig_open
        ch.time.sleep = orig_sleep
    print("OK — _get_json retries transient 5xx then succeeds")


def test_resolve_api_key_missing():
    saved = _clear_ch_env()
    try:
        try:
            ch.resolve_api_key(None)
            assert False, "resolve_api_key must raise when no key is set"
        except RuntimeError:
            pass
        assert ch.resolve_api_key("explicit-key") == "explicit-key"
    finally:
        _restore_env(saved)
    print("OK — resolve_api_key: raises when missing, honours explicit key")


def test_env_selects_key_and_base():
    import os

    saved = _clear_ch_env()
    try:
        # Default env is live.
        assert ch.resolve_env() == "live"
        assert ch._api_base() == ch.LIVE_API_BASE

        # test env selects the sandbox base and the test-specific key var;
        # "sandbox" is accepted as an alias for "test".
        os.environ[ch.ENV_VAR] = "test"
        os.environ["COMPANIES_HOUSE_TEST_KEY"] = "test-key"
        os.environ["COMPANIES_HOUSE_LIVE_KEY"] = "live-key"
        assert ch.resolve_env() == "test"
        assert ch._api_base() == ch.SANDBOX_API_BASE
        assert ch.resolve_api_key() == "test-key"

        os.environ[ch.ENV_VAR] = "live"
        assert ch.resolve_api_key() == "live-key"

        # Generic fallback used when the env-specific key is absent.
        del os.environ["COMPANIES_HOUSE_LIVE_KEY"]
        os.environ[ch.API_KEY_ENV] = "fallback-key"
        assert ch.resolve_api_key() == "fallback-key"

        # An unknown env value is rejected loudly.
        os.environ[ch.ENV_VAR] = "staging"
        try:
            ch.resolve_env()
            assert False, "unknown COMPANIES_HOUSE_ENV must raise"
        except RuntimeError:
            pass
    finally:
        _restore_env(saved)
    print("OK — env switch selects matching key + base, rejects bad env")


if __name__ == "__main__":
    test_parse_officers_payload()
    test_parse_psc_payload()
    test_filing_history_parse_and_latest()
    test_fetch_filing_history_paginates_and_404()
    test_parse_date()
    test_get_json_retries_5xx()
    test_fetch_officers_paginates()
    test_resolve_api_key_missing()
    test_env_selects_key_and_base()
    print("\nAll Companies House offline tests passed.")
