#!/usr/bin/env python3
"""Tests for Companies House officers + PSC → Person/Role (ADR 0014).

Pure-logic tests (name parsing, individual filters, correlation) plus sync tests
against in-memory SQLite — no Postgres or live API needed.

Run with: python test_enrich_people.py
"""

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

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
        stats = ep.sync_provider(s, provider.id, officers, pscs)
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
        stats = ep.sync_provider(s, provider.id, officers, pscs)
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
        stats = ep.sync_provider(s, provider.id, officers, [])
        s.commit()
        assert s.query(Person).count() == 1 and s.query(Role).count() == 1, stats
        role = s.query(Role).one()
        assert role.start_date == dt.date(2018, 1, 1) and role.end_date is None

        # Re-run: idempotent, updates in place.
        stats2 = ep.sync_provider(s, provider.id, officers, [])
        s.commit()
        assert stats2 == {"persons_created": 0, "roles_created": 0, "roles_updated": 1}, stats2
        assert s.query(Role).count() == 1
    print("OK — sync: idempotent; repeat appointments dedupe to the active Role")


def test_no_dob_low_confidence_and_idempotent():
    with _memory_session() as s:
        provider = _provider(s)
        officers = [_officer("NULL, Person", "director")]  # no DOB
        ep.sync_provider(s, provider.id, officers, [])
        s.commit()
        person = s.query(Person).one()
        assert person.match_confidence == "low" and person.dob_year is None
        # Re-run must not duplicate the no-DOB person.
        ep.sync_provider(s, provider.id, officers, [])
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


if __name__ == "__main__":
    test_name_parsing()
    test_individual_filters()
    test_correlation_links_officer_and_psc()
    test_corporates_and_secretaries_skipped()
    test_sync_idempotent_and_dedupes_repeat_appointments()
    test_no_dob_low_confidence_and_idempotent()
    test_providers_with_ch_number_skip_enriched()
    print("\nAll enrich_people tests passed.")
