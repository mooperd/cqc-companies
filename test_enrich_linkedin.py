#!/usr/bin/env python3
"""Offline tests for LinkedIn ingestion (ADR 0016 WS3) — profile→Person/Role
correlation and the PhantomRun lifecycle, against in-memory SQLite with a mocked
Phantombuster. No live key or scrape.

Run with: python test_enrich_linkedin.py
"""

import os

# A key for the per-user secret round-trip the live-driver test exercises.
os.environ.setdefault("APP_SECRETS_KEY", __import__("secrets_box").generate_key())

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import enrich_linkedin as el
import linkedin_profiles as lp
from model import Person, Provider, Role, User, db


def _session():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    return Session(engine)


def _provider(s):
    p = Provider(name="Acme Care Ltd", cqc_provider_id="1-X")
    s.add(p)
    s.flush()
    return p


def _profile(name, url=None, headline=None, company="Acme Care Ltd", location=None):
    return lp.ScrapedProfile(name=name, linkedin_url=url, headline=headline,
                             company=company, location=location)


def test_sync_creates_low_confidence_person_and_role():
    with _session() as s:
        provider = _provider(s)
        profiles = [_profile("Jane Smith", "https://linkedin.com/in/jane-smith",
                             "Director of Care")]
        stats = el.sync_profiles(s, provider, profiles, "company-people-scraper")
        s.commit()
        assert stats == {"persons_created": 1, "roles_created": 1,
                         "roles_updated": 0, "profiles": 1}, stats
        person = s.query(Person).one()
        assert person.match_confidence == "low" and person.dob_year is None
        assert person.linkedin_url == "https://linkedin.com/in/jane-smith"
        role = s.query(Role).one()
        assert role.source == "phantombuster:company-people-scraper"
        assert role.role_type == "influencer" and role.confidence == "low"
        assert role.control_nature == "Director of Care"  # the scraped headline
    print("OK — sync_profiles: low-confidence Person + phantombuster:<phantom> Role")


def test_linkedin_url_is_the_dedup_key():
    with _session() as s:
        provider = _provider(s)
        url = "https://linkedin.com/in/jane-smith"
        # Re-scrape: same URL, but the display name came back slightly different.
        el.sync_profiles(s, provider, [_profile("Jane Smith", url, "Director of Care")],
                         "company-people-scraper")
        s.commit()
        el.sync_profiles(s, provider, [_profile("Jane A Smith", url, "Care Director")],
                         "company-people-scraper")
        s.commit()
        assert s.query(Person).count() == 1, "same linkedin_url must be one Person"
        # Same person+provider+source → the role updates in place, not duplicates.
        assert s.query(Role).count() == 1
        assert s.query(Role).one().control_nature == "Care Director"
    print("OK — linkedin_url dedup: re-scrape (same URL) is one Person, role updates in place")


def test_no_auto_merge_into_companies_house_director():
    with _session() as s:
        provider = _provider(s)
        # A Companies House director: DOB-anchored, high confidence.
        ch_person = Person(name="KHAN, Asam", surname="khan", forenames="asam",
                           normalized_name="khan asam", dob_year=1982, dob_month=2,
                           match_confidence="high")
        s.add(ch_person)
        s.flush()
        s.add(Role(person_id=ch_person.id, provider_id=provider.id, role_type="director",
                   source="companies_house:officers", confidence="high"))
        s.commit()

        # LinkedIn scrapes the same human (no DOB). It must NOT be absorbed into
        # the DOB-anchored CH director — a separate low-confidence Person.
        el.sync_profiles(s, provider, [_profile("Asam Khan", "https://linkedin.com/in/asam",
                                               "Managing Director")],
                         "company-people-scraper")
        s.commit()
        khans = s.query(Person).filter_by(surname="khan").all()
        assert len(khans) == 2, "CH director and LinkedIn profile stay distinct people"
        confidences = sorted(p.match_confidence for p in khans)
        assert confidences == ["high", "low"], confidences
        # The CH director's authoritative role is untouched.
        assert s.query(Role).filter_by(source="companies_house:officers").count() == 1
    print("OK — no auto-merge: LinkedIn profile never absorbs a DOB-anchored CH director")


def test_sync_is_idempotent():
    with _session() as s:
        provider = _provider(s)
        profiles = [_profile("Bob Jones", "https://linkedin.com/in/bob", "Registered Manager")]
        el.sync_profiles(s, provider, profiles, "company-people-scraper")
        s.commit()
        stats2 = el.sync_profiles(s, provider, profiles, "company-people-scraper")
        s.commit()
        assert stats2 == {"persons_created": 0, "roles_created": 0,
                          "roles_updated": 0, "profiles": 1}, stats2
        assert s.query(Person).count() == 1 and s.query(Role).count() == 1
    print("OK — sync_profiles: a no-op re-sync changes nothing")


def test_run_identification_phantom_full_lifecycle():
    with _session() as s:
        provider = _provider(s)
        user = User(name="Rob", email="rob@shape.build")
        user.phantombuster_api_key = "pb-key"
        user.linkedin_session_cookie = "li_at=cookie"
        s.add(user)
        s.flush()

        launched = {}

        # A stand-in for phantombuster-lib's Phantombuster client (PWS1). The live
        # driver builds one from the user's key; get_result returns raw phantom rows
        # (dicts), which the driver maps via linkedin_profiles.parse_profiles.
        class FakeClient:
            def __init__(self, api_key):
                launched["api_key"] = api_key

            def launch(self, agent_id, argument=None):
                launched["agent_id"] = agent_id
                launched["argument"] = argument
                return "C1"

            def get_container(self, container_id):
                return {"status": "finished", "lastEndStatus": "success", "creditUsed": 3}

            def get_result(self, container_id):
                return [{"name": "Jane Smith", "profileUrl": "https://linkedin.com/in/jane",
                         "job": "Director of Care"},
                        {"name": "Bob Jones", "profileUrl": "https://linkedin.com/in/bob",
                         "job": "Registered Manager"}]

        orig = (el.Phantombuster, el.time.sleep)
        el.Phantombuster = FakeClient
        el.time.sleep = lambda _s: None
        try:
            run = el.run_identification_phantom(
                s, user, "company-people-scraper", "AGENT1",
                {"companyUrl": "https://linkedin.com/company/acme"}, provider=provider)
            s.commit()
        finally:
            el.Phantombuster, el.time.sleep = orig

        # Ran as the user: their key (client built from it) + injected session cookie.
        assert launched["api_key"] == "pb-key"
        assert launched["argument"]["sessionCookie"] == "li_at=cookie"
        # Run closed out and profiles ingested.
        assert run.status == "finished" and run.credits_spent == 3
        assert run.finished_at is not None and run.provider_id == provider.id
        assert s.query(Person).count() == 2 and s.query(Role).count() == 2
        assert {r.source for r in s.query(Role)} == {"phantombuster:company-people-scraper"}
    print("OK — run_identification_phantom: launch→poll→fetch→ingest, run under the user")


if __name__ == "__main__":
    test_sync_creates_low_confidence_person_and_role()
    test_linkedin_url_is_the_dedup_key()
    test_no_auto_merge_into_companies_house_director()
    test_sync_is_idempotent()
    test_run_identification_phantom_full_lifecycle()
    print("\nAll LinkedIn ingestion tests passed.")
