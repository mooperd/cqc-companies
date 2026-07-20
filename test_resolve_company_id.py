#!/usr/bin/env python3
"""Offline tests for CQC provider → LinkedIn company-id resolution (ADR 0016 PWS2).

Exercises the verify gate and the resolve/persist/cache orchestration against
in-memory SQLite with a fake CQC client and a fake resolver — no live keys, no
scrape, no credits.

Run with: python test_resolve_company_id.py
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import resolve_company_id as rc
from model import Provider, db


def _session():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    return Session(engine)


def _provider(s, cqc_id, name="Acme Care Ltd", website=None, town=None):
    p = Provider(name=name, cqc_provider_id=cqc_id, website=website, town_city=town)
    s.add(p)
    s.flush()
    return p


class FakeCQC:
    """Stand-in for phantombuster-lib's CQC Syndication client."""

    def __init__(self, providers):
        self._providers = providers

    def get_provider(self, provider_id):
        return self._providers[provider_id]


def _counting_resolver(row):
    """A resolver returning a fixed LinkedIn row, counting how often it's called."""
    calls = {"n": 0}

    def resolve(term):
        calls["n"] += 1
        return row

    return resolve, calls


# --- verify_match (pure) ------------------------------------------------------

def test_verify_website_domain_match_and_mismatch():
    li = {"companyId": "1", "name": "Practice Plus Group",
          "website": "https://www.practiceplusgroup.com/about"}
    ok, why = rc.verify_match(li, brand_name="Practice Plus Group",
                              website="http://practiceplusgroup.com")
    assert ok, why
    bad, why = rc.verify_match(li, brand_name="Practice Plus Group",
                               website="https://someotherfirm.co.uk")
    assert not bad and "mismatch" in why, why
    print("OK — verify_match: website domain agree ⇒ pass, disagree ⇒ hard reject")


def test_verify_name_similarity_rejects_wrong_company():
    # The canonical fuzzy-search failure: Scarborough Hall → rbrecycling.
    li = {"companyId": "111", "name": "rbrecycling", "website": None,
          "headquarters": "Leeds"}
    ok, why = rc.verify_match(li, brand_name="Scarborough Hall", website=None,
                              town="Scarborough")
    assert not ok and "no confirming signal" in why, why
    print("OK — verify_match: unrelated name (rbrecycling) is rejected")


def test_verify_name_match_with_and_against_town():
    li = {"companyId": "5", "name": "Sunrise Senior Living",
          "website": None, "headquarters": "Woking, Surrey, United Kingdom"}
    ok, why = rc.verify_match(li, brand_name="Sunrise Senior Living", town="Woking")
    assert ok and "town" in why, why
    # Same name but the town contradicts → reject.
    li2 = dict(li, headquarters="Manchester, England")
    bad, why = rc.verify_match(li2, brand_name="Sunrise Senior Living", town="Woking")
    assert not bad and "town does not" in why, why
    print("OK — verify_match: name match confirmed by town; contradicted by town ⇒ reject")


# --- resolve_provider (orchestration + persistence + cache) -------------------

def test_resolve_stores_verified_company_id_and_caches_by_brand():
    with _session() as s:
        p = _provider(s, "1-A", "Practice Plus Group Hospitals Limited",
                      website="https://practiceplusgroup.com")
        cqc = FakeCQC({"1-A": {
            "brandName": "BRAND Practice Plus Group", "brandId": "BD122",
            "website": "https://www.practiceplusgroup.com/", "postalAddressTownCity": "Bristol",
        }})
        li = {"companyId": "68842389", "name": "Practice Plus Group",
              "website": "https://practiceplusgroup.com", "headquarters": "Reading, England"}
        resolve, calls = _counting_resolver(li)
        cache = {}
        out = rc.resolve_provider(p, cqc_client=cqc, resolve=resolve, cache=cache)
        s.commit()
        assert out.status == "resolved", out
        assert p.linkedin_company_id == "68842389"
        assert cache == {"BD122": "68842389"}
        assert calls["n"] == 1
    print("OK — resolve_provider: verified match stored + cached by brandId")


def test_brand_cache_hit_skips_the_resolver():
    with _session() as s:
        # A sibling provider under an already-resolved brand.
        p = _provider(s, "1-B", "Practice Plus Group Clinics Limited")
        cqc = FakeCQC({"1-B": {"brandName": "BRAND Practice Plus Group",
                               "brandId": "BD122", "website": None}})
        resolve, calls = _counting_resolver({"companyId": "SHOULD_NOT_BE_USED"})
        out = rc.resolve_provider(p, cqc_client=cqc, resolve=resolve,
                                  cache={"BD122": "68842389"})
        s.commit()
        assert out.status == "cached", out
        assert p.linkedin_company_id == "68842389"
        assert calls["n"] == 0, "a brand cache hit must not run the (paid) resolver"
    print("OK — resolve_provider: brandId cache hit reuses the id, no resolver call")


def test_bad_match_is_rejected_and_nothing_is_stored():
    with _session() as s:
        p = _provider(s, "1-C", "Scarborough Hall", town="Scarborough")
        cqc = FakeCQC({"1-C": {"brandName": "Scarborough Hall", "brandId": "BD999",
                               "website": None, "postalAddressTownCity": "Scarborough"}})
        resolve, _ = _counting_resolver({"companyId": "111", "name": "rbrecycling",
                                         "website": "https://rb-recycling.co.uk",
                                         "headquarters": "Leeds"})
        cache = {}
        out = rc.resolve_provider(p, cqc_client=cqc, resolve=resolve, cache=cache)
        s.commit()
        assert out.status == "rejected", out
        assert p.linkedin_company_id is None
        assert cache == {}, "a rejected match must not poison the brand cache"
    print("OK — resolve_provider: unverified match rejected, linkedin_company_id stays NULL")


def test_deregistered_provider_is_skipped():
    with _session() as s:
        p = _provider(s, "1-D", "Closed Care Ltd")
        cqc = FakeCQC({"1-D": {"brandName": "Closed Care", "brandId": "BD000",
                               "registrationStatus": "Deregistered"}})
        resolve, calls = _counting_resolver({"companyId": "x"})
        out = rc.resolve_provider(p, cqc_client=cqc, resolve=resolve, cache={})
        s.commit()
        assert out.status == "skipped" and p.linkedin_company_id is None, out
        assert calls["n"] == 0, "no resolver call for a deregistered provider"
    print("OK — resolve_provider: deregistered provider skipped before any lookup")


def test_no_match_when_resolver_finds_nothing():
    with _session() as s:
        p = _provider(s, "1-E", "Obscure Care Ltd")
        cqc = FakeCQC({"1-E": {"brandName": "Obscure Care", "brandId": "BD111"}})
        out = rc.resolve_provider(p, cqc_client=cqc, resolve=lambda term: None, cache={})
        s.commit()
        assert out.status == "no-match" and p.linkedin_company_id is None, out
    print("OK — resolve_provider: resolver returning nothing ⇒ no-match, nothing stored")


# --- resolve_all (batch resilience) ------------------------------------------

class RaisingCQC:
    """CQC client that raises for one provider id (a transient 5xx), else returns
    a resolvable row — to prove one flaky provider doesn't abort the batch."""

    def __init__(self, bad_id):
        self._bad = bad_id

    def get_provider(self, provider_id):
        if provider_id == self._bad:
            raise RuntimeError(f"CQC API 500 for {provider_id}")
        return {"brandName": "Acme", "brandId": f"BD-{provider_id}", "website": None}


def test_resolve_all_continues_past_a_failing_provider():
    with _session() as s:
        for pid in ("1-OK1", "1-BOOM", "1-OK2"):
            s.add(Provider(name=f"P {pid}", cqc_provider_id=pid, active=True))
        s.flush()
        li = {"companyId": "999", "name": "Acme", "website": None, "headquarters": None}
        tally = rc.resolve_all(s, RaisingCQC(bad_id="1-BOOM"), resolve=lambda term: li)
        s.commit()
        assert tally.get("error") == 1, tally
        assert tally.get("resolved") == 2, tally  # both good providers still resolved
        assert s.query(Provider).filter(Provider.linkedin_company_id == "999").count() == 2
    print("OK — resolve_all: a provider raising (CQC 5xx) is tallied 'error'; batch continues")


if __name__ == "__main__":
    test_verify_website_domain_match_and_mismatch()
    test_verify_name_similarity_rejects_wrong_company()
    test_verify_name_match_with_and_against_town()
    test_resolve_stores_verified_company_id_and_caches_by_brand()
    test_brand_cache_hit_skips_the_resolver()
    test_bad_match_is_rejected_and_nothing_is_stored()
    test_deregistered_provider_is_skipped()
    test_no_match_when_resolver_finds_nothing()
    print("\nAll company-id resolution tests passed.")
