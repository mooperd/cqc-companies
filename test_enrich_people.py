#!/usr/bin/env python3
"""Tests for Companies House officers + PSC → Person/Role (ADR 0014).

Pure-logic tests (name parsing, individual filters, correlation) plus sync tests
against in-memory SQLite — no Postgres or live API needed.

Run with: python test_enrich_people.py
"""

import datetime as dt
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import companies_house as ch
import enrich_people as ep
from companies_house import PSC, Officer
from model import Person, Provider, Role, db


def _officer(name, role="director", appointed=None, resigned=None, year=None, month=None, nat=None):
    return Officer(
        name=name, role=role,
        appointed_on=dt.date.fromisoformat(appointed) if appointed else None,
        resigned_on=dt.date.fromisoformat(resigned) if resigned else None,
        dob_year=year, dob_month=month, nationality=nat,
    )


def _psc(name, kind="individual-person-with-significant-control", natures=(),
         notified=None, ceased=None, year=None, month=None, nat=None):
    return PSC(
        name=name, kind=kind, natures_of_control=tuple(natures),
        notified_on=dt.date.fromisoformat(notified) if notified else None,
        ceased_on=dt.date.fromisoformat(ceased) if ceased else None,
        dob_year=year, dob_month=month, nationality=nat,
    )


def test_name_parsing():
    assert ep._split_officer_name("KHAN, Asam Tazeem") == ("khan", "asam tazeem")
    assert ep._split_psc_name("Mr Asam Khan") == ("khan", "asam")
    assert ep._split_psc_name("Ms Samia Tazeem Khan") == ("khan", "samia tazeem")
    # Officer and PSC forms of the same person reduce to matching surname + first
    # forename (middle name differs — must be ignored by correlation).
    o = ep.identity_from_officer(_officer("KHAN, Asam Tazeem", year=1982, month=2))
    p = ep.identity_from_psc(_psc("Mr Asam Khan", year=1982, month=2))
    assert o.surname == p.surname == "khan"
    assert o.first_forename == p.first_forename == "asam"
    print("OK — name parsing: officer/PSC forms reduce to matching surname+forename")


def test_individual_filters():
    assert ep.is_individual_director(_officer("X", "director"))
    assert ep.is_individual_director(_officer("X", "nominee-director"))
    assert not ep.is_individual_director(_officer("X", "corporate-director"))
    assert not ep.is_individual_director(_officer("X", "secretary"))
    assert ep.is_individual_psc(_psc("X", "individual-person-with-significant-control"))
    assert not ep.is_individual_psc(_psc("X", "corporate-entity-person-with-significant-control"))
    print("OK — individual filters: directors+individual PSCs in, corporates/secretaries out")


def _memory_session():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    return Session(engine)


def _provider(session):
    p = Provider(name="Acme Care Ltd", cqc_provider_id="1-X", companies_house_number="01234567")
    session.add(p)
    session.flush()
    return p


def test_correlation_links_officer_and_psc():
    # The headline case: the same human as a director and a PSC, different name
    # formats, same DOB → ONE Person with TWO Roles.
    with _memory_session() as s:
        provider = _provider(s)
        officers = [_officer("KHAN, Asam Tazeem", "director", appointed="2017-08-01",
                             year=1982, month=2, nat="British")]
        pscs = [_psc("Mr Asam Khan", natures=["ownership-of-shares-25-to-50-percent"],
                     notified="2017-08-01", year=1982, month=2, nat="British")]
        stats = ep.sync_provider(s, provider, officers, pscs)
        s.commit()

        assert stats["persons_created"] == 1, stats
        assert stats["roles_created"] == 2, stats
        people = s.query(Person).all()
        assert len(people) == 1, "officer + PSC of one human must be a single Person"
        roles = sorted(s.query(Role).all(), key=lambda r: r.role_type)
        assert [r.role_type for r in roles] == ["director", "psc"]
        director, psc = roles
        assert director.source == "companies_house:officers"
        assert psc.source == "companies_house:psc"
        assert psc.control_nature == "ownership-of-shares-25-to-50-percent"
        assert people[0].match_confidence == "high"
    print("OK — correlation: officer + PSC of one human → one Person, two Roles")


def test_corporates_and_secretaries_skipped():
    with _memory_session() as s:
        provider = _provider(s)
        officers = [
            _officer("SMITH, Jane", "director", year=1980, month=5),
            _officer("RSS GLOBAL LIMITED", "corporate-director"),
            _officer("ACME SECRETARIES LTD", "corporate-nominee-secretary"),
        ]
        pscs = [_psc("MGG Health Limited", "corporate-entity-person-with-significant-control")]
        stats = ep.sync_provider(s, provider, officers, pscs)
        s.commit()
        assert s.query(Person).count() == 1, "only the individual director"
        assert stats["roles_created"] == 1
    print("OK — sync: corporate directors/PSCs and secretaries skipped")


def test_sync_idempotent_and_dedupes_repeat_appointments():
    with _memory_session() as s:
        provider = _provider(s)
        # Same person appears twice (resigned spell + active spell) → one Role,
        # reflecting the active appointment.
        officers = [
            _officer("DOE, Jane", "director", appointed="2010-01-01", resigned="2015-01-01",
                     year=1975, month=3),
            _officer("DOE, Jane", "director", appointed="2018-01-01", year=1975, month=3),
        ]
        stats = ep.sync_provider(s, provider, officers, [])
        s.commit()
        assert s.query(Person).count() == 1 and s.query(Role).count() == 1, stats
        role = s.query(Role).one()
        assert role.start_date == dt.date(2018, 1, 1) and role.end_date is None

        # Re-run: idempotent. A genuine no-op re-sync changes nothing and emits
        # no events (the role's fields are already current).
        stats2 = ep.sync_provider(s, provider, officers, [])
        s.commit()
        assert stats2 == {"persons_created": 0, "roles_created": 0,
                          "roles_updated": 0, "events": []}, stats2
        assert s.query(Role).count() == 1
    print("OK — sync: idempotent; repeat appointments dedupe to the active Role")


def test_no_dob_low_confidence_and_idempotent():
    with _memory_session() as s:
        provider = _provider(s)
        officers = [_officer("NULL, Person", "director")]  # no DOB
        ep.sync_provider(s, provider, officers, [])
        s.commit()
        person = s.query(Person).one()
        assert person.match_confidence == "low" and person.dob_year is None
        # Re-run must not duplicate the no-DOB person.
        ep.sync_provider(s, provider, officers, [])
        s.commit()
        assert s.query(Person).count() == 1
    print("OK — no-DOB person: low confidence, idempotent by name")


def test_providers_with_ch_number_skip_enriched():
    with _memory_session() as s:
        a = Provider(name="A", cqc_provider_id="1-A", companies_house_number="01")
        b = Provider(name="B", cqc_provider_id="1-B", companies_house_number="02")
        c = Provider(name="C", cqc_provider_id="1-C", companies_house_number=None)
        s.add_all([a, b, c])
        s.flush()
        assert {p.name for p in ep.providers_with_ch_number(s)} == {"A", "B"}
        # Give A a CH role → skip_enriched drops it.
        person = Person(name="x")
        s.add(person)
        s.flush()
        s.add(Role(person_id=person.id, provider_id=a.id, role_type="director",
                   source="companies_house:officers"))
        s.commit()
        remaining = [p.name for p in ep.providers_with_ch_number(s, skip_enriched=True)]
        assert remaining == ["B"], remaining
    print("OK — providers_with_ch_number: CH-only + skip_enriched (by Role)")


# --- WS4: change-event production (filing-history gate, role events, seed) -----


def test_should_repoll():
    never = Provider(name="N", ch_enriched_at=None)
    assert ep._should_repoll(never, dt.date(2024, 1, 1)) is True, "never-enriched → poll"
    assert ep._should_repoll(never, None) is True

    enriched_no_wm = Provider(name="E", ch_enriched_at=dt.datetime(2024, 1, 1),
                              ch_filing_watermark=None)
    assert ep._should_repoll(enriched_no_wm, dt.date(2024, 1, 1)) is True, \
        "enriched before WS4 (no watermark) → poll once"
    assert ep._should_repoll(enriched_no_wm, None) is False, \
        "no officer/PSC filings on record → nothing to do"

    fresh = Provider(name="F", ch_enriched_at=dt.datetime(2024, 6, 1),
                     ch_filing_watermark="2024-05-01")
    assert ep._should_repoll(fresh, dt.date(2024, 5, 1)) is False, "same date → skip"
    assert ep._should_repoll(fresh, dt.date(2024, 4, 1)) is False, "older filing → skip"
    assert ep._should_repoll(fresh, dt.date(2024, 6, 2)) is True, "newer filing → poll"
    print("OK — _should_repoll: never-enriched / no-watermark / newer-filing gate")


def test_sync_emits_role_events():
    with _memory_session() as s:
        provider = _provider(s)
        observed = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)

        # First sync of a director → one role_appointed event + a ChangeEvent row.
        officers = [_officer("SMITH, Jane", "director", appointed="2020-01-01",
                             year=1980, month=5)]
        stats = ep.sync_provider(s, provider, officers, [], observed_at=observed)
        s.commit()
        assert [e["change_type"] for e in stats["events"]] == ["role_appointed"]
        ev = stats["events"][0]
        assert ev["source"] == "companies_house:officers"
        assert ev["effective_date"] == "2020-01-01"
        assert ev["provider"]["cqc_provider_id"] == "1-X"
        assert ev["person"]["surname"] == "smith"
        from model import ChangeEvent
        assert s.query(ChangeEvent).filter_by(change_type="role_appointed").count() == 1

        # Re-sync unchanged → no events, no new ChangeEvent rows.
        stats2 = ep.sync_provider(s, provider, officers, [], observed_at=observed)
        s.commit()
        assert stats2["events"] == []
        assert s.query(ChangeEvent).count() == 1

        # The director resigns → role_ended event.
        resigned = [_officer("SMITH, Jane", "director", appointed="2020-01-01",
                             resigned="2024-05-20", year=1980, month=5)]
        stats3 = ep.sync_provider(s, provider, resigned, [], observed_at=observed)
        s.commit()
        assert [e["change_type"] for e in stats3["events"]] == ["role_ended"]
        assert stats3["events"][0]["effective_date"] == "2024-05-20"
        assert s.query(ChangeEvent).filter_by(change_type="role_ended").count() == 1
    print("OK — sync events: appointed, no-op silence, ended")


def test_sync_role_changed_event():
    with _memory_session() as s:
        provider = _provider(s)
        observed = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
        psc = [_psc("Mr Asam Khan", natures=["ownership-of-shares-25-to-50-percent"],
                    notified="2020-01-01", year=1982, month=2)]
        ep.sync_provider(s, provider, [], psc, observed_at=observed)
        s.commit()
        # Control nature changes (25-50 → 75-100%) → role_changed, not appointed.
        psc2 = [_psc("Mr Asam Khan", natures=["ownership-of-shares-75-to-100-percent"],
                     notified="2020-01-01", year=1982, month=2)]
        stats = ep.sync_provider(s, provider, [], psc2, observed_at=observed)
        s.commit()
        assert [e["change_type"] for e in stats["events"]] == ["role_changed"]
        details = stats["events"][0]["details"]
        assert "ownership-of-shares-25-to-50-percent" in details["before"]["control_nature"]
        assert "ownership-of-shares-75-to-100-percent" in details["after"]["control_nature"]
    print("OK — sync events: a control-nature change is a role_changed event")


def test_build_seed_events():
    with _memory_session() as s:
        provider = _provider(s)
        officers = [_officer("SMITH, Jane", "director", appointed="2020-01-01",
                             year=1980, month=5)]
        pscs = [_psc("Mr Asam Khan", natures=["ownership-of-shares-25-to-50-percent"],
                     notified="2017-08-01", year=1982, month=2)]
        ep.sync_provider(s, provider, officers, pscs)
        s.commit()
        events = ep.build_seed_events(s)
        assert len(events) == 2, "two CH roles → two seed events"
        assert {e["change_type"] for e in events} == {"role_appointed"}
        assert {e["role"]["role_type"] for e in events} == {"director", "psc"}
    print("OK — build_seed_events: every current CH role as role_appointed")


class _FakeCH:
    """Stand-in for the companies_house module: scripted per-company responses."""

    CompaniesHouseError = ch.CompaniesHouseError

    def __init__(self, filings, officers, pscs):
        self._filings, self._officers, self._pscs = filings, officers, pscs
        self.officer_calls = []

    def resolve_env(self):
        return "test"

    def fetch_filing_history(self, number):
        return self._filings.get(number, [])

    def latest_relevant_filing_date(self, entries):
        return ch.latest_relevant_filing_date(entries)

    def fetch_officers(self, number):
        self.officer_calls.append(number)
        return self._officers.get(number, [])

    def fetch_psc(self, number):
        return self._pscs.get(number, [])


def _filing(category, date):
    return ch.FilingHistoryEntry(category=category, date=dt.date.fromisoformat(date))


def test_enrich_all_skips_unchanged_and_writes_file():
    import tempfile
    from pathlib import Path

    with _memory_session() as s:
        # A is fresh (watermark up to date, no new filing) → skipped.
        # B is never-enriched → re-polled, emits a role_appointed event.
        a = Provider(name="A", cqc_provider_id="1-A", companies_house_number="0001",
                     ch_enriched_at=dt.datetime(2024, 1, 1), ch_filing_watermark="2024-01-01")
        b = Provider(name="B", cqc_provider_id="1-B", companies_house_number="0002")
        s.add_all([a, b])
        s.commit()

        filings = {
            "0001": [_filing("officers", "2024-01-01")],          # not newer than A's watermark
            "0002": [_filing("officers", "2024-03-01")],
        }
        officers = {"0002": [_officer("DOE, John", "director", appointed="2024-02-20",
                                      year=1990, month=7)]}
        fake = _FakeCH(filings, officers, pscs={})

        out_dir = Path(tempfile.mkdtemp()) / "changes"
        orig = ep.ch
        ep.ch = fake
        try:
            totals = ep.enrich_all(s, sleep=0, out_dir=out_dir)
        finally:
            ep.ch = orig

        assert totals["skipped_unchanged"] == 1, totals
        assert totals["repolled"] == 1, totals
        assert fake.officer_calls == ["0002"], "only the changed company is re-polled"
        assert totals["roles_created"] == 1

        # B got a watermark + enriched timestamp; A's are untouched.
        assert b.ch_filing_watermark == "2024-03-01"
        assert b.ch_enriched_at is not None

        files = list(out_dir.glob("companies-house-*.json"))
        assert len(files) == 1, files
        payload = json.loads(files[0].read_text())
        assert payload["source"] == "companies_house"
        assert [e["change_type"] for e in payload["events"]] == ["role_appointed"]
        assert payload["events"][0]["provider"]["cqc_provider_id"] == "1-B"
    print("OK — enrich_all: cheap gate skips unchanged, re-polls changed, writes file")


if __name__ == "__main__":
    test_name_parsing()
    test_individual_filters()
    test_correlation_links_officer_and_psc()
    test_corporates_and_secretaries_skipped()
    test_sync_idempotent_and_dedupes_repeat_appointments()
    test_no_dob_low_confidence_and_idempotent()
    test_providers_with_ch_number_skip_enriched()
    test_should_repoll()
    test_sync_emits_role_events()
    test_sync_role_changed_event()
    test_build_seed_events()
    test_enrich_all_skips_unchanged_and_writes_file()
    print("\nAll enrich_people tests passed.")
