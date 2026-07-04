#!/usr/bin/env python3
"""Offline tests for durable erasure + suppression + retention (ADR 0017 §5/§4).

The load-bearing guarantee: an erased contact must stay erased despite the
pipeline re-scraping their company. All against in-memory SQLite.

Run with: python test_suppression.py
"""

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import enrich_linkedin as el
import enrich_people as ep
import suppression as sup
from enrich_people import Identity
from linkedin_profiles import ScrapedProfile
from model import Person, Provider, Role, SuppressedContact, db


def _session():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    return Session(engine)


def _provider(s):
    p = Provider(name="Acme Care Ltd", cqc_provider_id="1-X")
    s.add(p)
    s.flush()
    return p


def _profile(name, url, headline="Registered Manager"):
    return ScrapedProfile(name=name, linkedin_url=url, headline=headline,
                          company="Acme Care Ltd", location=None)


def test_hash_is_deterministic_over_the_canonical_form():
    # The hash is verbatim over the caller's canonical key (no in-module
    # "normalisation") — so the suppression key == the ADR 0014 correlation key.
    assert sup.hash_identifier("smith jane") == sup.hash_identifier("smith jane")
    assert len(sup.hash_identifier("smith jane")) == 64  # sha-256 hex
    assert sup.hash_identifier("smith jane") != sup.hash_identifier("smith john")
    # Different representations are NOT folded together here — canonicalisation is
    # the caller's job (Identity.normalized_name), because names can't be normalised.
    assert sup.hash_identifier("Smith Jane") != sup.hash_identifier("smith jane")
    print("OK — hash_identifier: verbatim over the caller's canonical form + sha-256")


def test_suppress_is_idempotent_and_never_stores_the_value():
    with _session() as s:
        sup.suppress(s, linkedin_url="https://linkedin.com/in/jane",
                     normalized_name="jane smith", reason="erasure request")
        s.commit()
        sup.suppress(s, linkedin_url="https://linkedin.com/in/jane",
                     normalized_name="jane smith", reason="erasure request")
        s.commit()
        rows = s.query(SuppressedContact).all()
        assert len(rows) == 2, [r.key_type for r in rows]  # one per identifier, no dupes
        # The raw identifier never appears — only its hash.
        assert all("jane" not in r.identifier_hash for r in rows)
        assert {r.key_type for r in rows} == {"linkedin_url", "name"}
    print("OK — suppress: idempotent, one tombstone per identifier, value never stored")


def test_erase_person_deletes_person_and_roles_and_writes_tombstone():
    with _session() as s:
        provider = _provider(s)
        el.sync_profiles(s, provider, [_profile("Jane Smith", "https://linkedin.com/in/jane")],
                         "company-people-scraper")
        s.commit()
        person = s.query(Person).one()
        sup.erase_person(s, person, reason="objection")
        s.commit()
        assert s.query(Person).count() == 0 and s.query(Role).count() == 0
        assert sup.is_suppressed(s, linkedin_url="https://linkedin.com/in/jane")
        assert sup.is_suppressed(s, normalized_name="smith jane") or \
            sup.is_suppressed(s, normalized_name=person.normalized_name)
    print("OK — erase_person: Person + Roles deleted, tombstones written")


def test_rescrape_cannot_resurrect_an_erased_person():
    """The load-bearing guarantee (ADR 0017 §5)."""
    with _session() as s:
        provider = _provider(s)
        profile = _profile("Jane Smith", "https://linkedin.com/in/jane")
        el.sync_profiles(s, provider, [profile], "company-people-scraper")
        s.commit()
        person = s.query(Person).one()
        sup.erase_person(s, person, reason="erasure request")
        s.commit()

        # The monthly re-scrape returns Jane again — she must NOT come back.
        stats = el.sync_profiles(s, provider, [profile], "company-people-scraper")
        s.commit()
        assert s.query(Person).count() == 0, "erased person was resurrected by a re-scrape!"
        assert stats["suppressed"] == 1 and stats["persons_created"] == 0, stats
    print("OK — re-scrape of an erased person creates nothing (suppression holds)")


def test_suppression_by_name_blocks_a_companies_house_director():
    with _session() as s:
        sup.suppress(s, normalized_name="khan asam", reason="objection")
        s.commit()
        # CH ingest tries to create the same-named director → gated before creation.
        identity = Identity("Asam Khan", "khan", "asam", None, None, None)
        person, created = ep.find_or_create_person(s, identity)
        assert person is None and created is False
        assert s.query(Person).count() == 0
    print("OK — name suppression blocks the CH ingest path too (find_or_create_person)")


def test_retention_purge_removes_stale_scraped_contacts_only():
    with _session() as s:
        provider = _provider(s)
        now = dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc)
        old = now - dt.timedelta(days=800)   # past the 730d window
        recent = now - dt.timedelta(days=30)

        # 1) stale low-confidence scraped contact → purged
        stale = Person(name="Old Contact", surname="contact", normalized_name="contact old",
                       match_confidence="low", acquired_at=old)
        # 2) recent low-confidence → kept
        fresh = Person(name="New Contact", surname="contact2", normalized_name="contact new",
                       match_confidence="low", acquired_at=recent)
        # 3) high-confidence (CH director) stale → kept (stronger basis)
        director = Person(name="Dir Ector", surname="ector", normalized_name="ector dir",
                          match_confidence="high", acquired_at=old)
        # 4) stale low-confidence BUT has a manual role (live relationship) → kept
        touched = Person(name="Known Contact", surname="known", normalized_name="known c",
                         match_confidence="low", acquired_at=old)
        s.add_all([stale, fresh, director, touched])
        s.flush()
        s.add(Role(person_id=touched.id, provider_id=provider.id,
                   role_type="contact", source="manual"))
        s.commit()

        purged = sup.purge_stale(s, retention_days=730, now=now)
        s.commit()
        assert purged == 1, purged
        remaining = {p.name for p in s.query(Person)}
        assert remaining == {"New Contact", "Dir Ector", "Known Contact"}, remaining
    print("OK — purge_stale: only stale, relationship-less, scraped contacts removed")


if __name__ == "__main__":
    test_hash_is_deterministic_over_the_canonical_form()
    test_suppress_is_idempotent_and_never_stores_the_value()
    test_erase_person_deletes_person_and_roles_and_writes_tombstone()
    test_rescrape_cannot_resurrect_an_erased_person()
    test_suppression_by_name_blocks_a_companies_house_director()
    test_retention_purge_removes_stale_scraped_contacts_only()
    print("\nAll suppression / erasure / retention tests passed.")
