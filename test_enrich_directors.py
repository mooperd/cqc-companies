#!/usr/bin/env python3
"""Tests for the Companies House officer → director Person mapping (WS2).

Pure-logic tests (role filter, dedupe) plus a sync test against an in-memory
SQLite database — no Postgres or live API needed, so this runs anywhere.

Run with: python test_enrich_directors.py
"""

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import enrich_directors as ed
from companies_house import Officer
from model import Person, Provider, db


def _officer(name, role="director", appointed=None, resigned=None):
    return Officer(
        name=name,
        role=role,
        appointed_on=dt.date.fromisoformat(appointed) if appointed else None,
        resigned_on=dt.date.fromisoformat(resigned) if resigned else None,
    )


def test_is_director_role():
    assert ed.is_director_role("director")
    assert ed.is_director_role("corporate-director")
    assert ed.is_director_role("nominee-director")
    assert not ed.is_director_role("secretary")
    assert not ed.is_director_role("corporate-nominee-secretary")
    assert not ed.is_director_role("")
    assert not ed.is_director_role(None)
    print("OK — is_director_role: directors in, secretaries/nominees out")


def test_dedupe_prefers_active_then_latest():
    # Same person resigned once and is now active again → active wins.
    resigned = _officer("SMITH, Jane", appointed="2010-01-01", resigned="2015-01-01")
    active = _officer("Smith,  Jane", appointed="2018-01-01")  # name normalises equal
    out = ed.dedupe_by_identity([resigned, active])
    assert len(out) == 1
    assert out[0].is_active, "active appointment should win over resigned"

    # Two resigned spells for one person → latest appointment wins.
    older = _officer("JONES, Bob", appointed="2000-01-01", resigned="2003-01-01")
    newer = _officer("JONES, Bob", appointed="2008-01-01", resigned="2011-01-01")
    out = ed.dedupe_by_identity([older, newer])
    assert len(out) == 1 and out[0].appointed_on == dt.date(2008, 1, 1)
    print("OK — dedupe_by_identity: active beats resigned; latest appointment wins")


def _memory_session():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    return Session(engine)


def _make_provider(session):
    p = Provider(name="Acme Care Ltd", cqc_provider_id="1-X", companies_house_number="01234567")
    session.add(p)
    session.flush()
    return p


def test_sync_creates_directors_and_skips_secretaries():
    with _memory_session() as s:
        provider = _make_provider(s)
        officers = [
            _officer("SMITH, Jane", "director", appointed="2018-04-01"),
            _officer("JONES, Robert", "director", appointed="2010-06-15", resigned="2021-09-30"),
            _officer("ACME SECRETARIES LTD", "corporate-nominee-secretary", appointed="2009-01-02"),
        ]
        stats = ed.sync_provider_directors(s, provider.id, officers)
        s.commit()

        assert stats == {"created": 2, "updated": 0, "skipped_non_director": 1}, stats
        people = s.query(Person).order_by(Person.name).all()
        assert [p.name for p in people] == ["JONES, Robert", "SMITH, Jane"]
        jane = next(p for p in people if p.name == "SMITH, Jane")
        assert jane.source == "companies_house"
        assert jane.confidence == "high"
        assert jane.appointment_date == dt.date(2018, 4, 1)
        assert jane.resignation_date is None
        robert = next(p for p in people if p.name == "JONES, Robert")
        assert robert.resignation_date == dt.date(2021, 9, 30)
    print("OK — sync: creates directors, skips secretaries, maps fields")


def test_sync_is_idempotent():
    with _memory_session() as s:
        provider = _make_provider(s)
        officers = [_officer("SMITH, Jane", "director", appointed="2018-04-01")]
        ed.sync_provider_directors(s, provider.id, officers)
        s.commit()

        # Re-run with an updated resignation date — must update, not duplicate.
        officers = [_officer("SMITH, Jane", "director", appointed="2018-04-01", resigned="2024-01-01")]
        stats = ed.sync_provider_directors(s, provider.id, officers)
        s.commit()

        assert stats == {"created": 0, "updated": 1, "skipped_non_director": 0}, stats
        people = s.query(Person).all()
        assert len(people) == 1, "re-run must not duplicate"
        assert people[0].resignation_date == dt.date(2024, 1, 1)
    print("OK — sync: idempotent on re-run, updates in place")


def test_sync_leaves_manual_rows_untouched():
    with _memory_session() as s:
        provider = _make_provider(s)
        # A manually-entered person with the same name as a CH director.
        manual = Person(
            name="SMITH, Jane", role="Owner", source="manual", confidence="high",
            provider_id=provider.id,
        )
        s.add(manual)
        s.flush()

        officers = [_officer("SMITH, Jane", "director", appointed="2018-04-01")]
        stats = ed.sync_provider_directors(s, provider.id, officers)
        s.commit()

        # WS2 only touches companies_house rows → the manual row survives, and a
        # separate CH row is created (cross-source merge is WS3, not here).
        assert stats["created"] == 1, stats
        manual_rows = s.query(Person).filter_by(source="manual").all()
        assert len(manual_rows) == 1 and manual_rows[0].role == "Owner"
        ch_rows = s.query(Person).filter_by(source="companies_house").all()
        assert len(ch_rows) == 1
    print("OK — sync: leaves manual-sourced rows untouched (WS3 boundary)")


if __name__ == "__main__":
    test_is_director_role()
    test_dedupe_prefers_active_then_latest()
    test_sync_creates_directors_and_skips_secretaries()
    test_sync_is_idempotent()
    test_sync_leaves_manual_rows_untouched()
    print("\nAll enrich_directors tests passed.")
