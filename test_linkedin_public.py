"""Offline tests for the no-auth LinkedIn resolver (linkedin_public + the
public_resolver wrapper). No network — `_fetch`/`fetch_company` are stubbed.
Run: python test_linkedin_public.py"""

import linkedin_public as lp

# A trimmed public company page carrying the fields the parser reads.
_PAGE = (
    '<title>Barchester Healthcare | LinkedIn</title>'
    '<meta property="og:url" content="https://uk.linkedin.com/company/barchester-healthcare">'
    '...urn:li:organization:80128...'
    '"addressLocality":"London","addressRegion":"England"'
    '"sameAs":"https://barchestercareers.com"'  # unreliable — deliberately NOT returned
)


def test_slug_candidates():
    c = lp.slug_candidates("Barchester Healthcare Homes Limited")
    assert c[0] == "barchester-healthcare-homes", c          # 'limited' dropped, longest first
    assert "barchester-healthcare" in c
    assert "barchester-healthcare-homes-limited" not in c    # legal suffix stripped
    assert lp.slug_candidates("Bupa") == ["bupa"]            # single-token brand
    assert lp.slug_candidates("HC-One")[0] == "hc-one"       # hyphen → slug
    assert len(lp.slug_candidates("a b c d e f g h")) <= lp._MAX_CANDIDATES
    print("OK — slug_candidates: strips legal suffix, shortens longest-first, capped")


def test_fetch_company_extracts_id_name_hq():
    orig = lp._fetch
    lp._fetch = lambda url: _PAGE if url.endswith("/company/barchester-healthcare/") else None
    try:
        page = lp.fetch_company("barchester-healthcare")
        assert page["companyId"] == "80128", page
        assert page["name"] == "Barchester Healthcare", page
        assert page["headquarters"] == "London, England", page
        assert page["vanity"] == "barchester-healthcare"
        assert "website" not in page          # sameAs deliberately omitted (unreliable)
        assert lp.fetch_company("does-not-exist") is None   # 404 → None
        # a page with no company id → None
        lp._fetch = lambda url: "<title>X | LinkedIn</title>no id here"
        assert lp.fetch_company("x") is None
    finally:
        lp._fetch = orig
    print("OK — fetch_company: extracts id/name/HQ, omits website, None without an id")


def test_public_resolver_picks_name_matching_page():
    from resolve_company_id import public_resolver
    orig = lp.fetch_company
    # First candidate 404s (None), second is the real, name-matching company.
    real = {"companyId": "80128", "name": "Barchester Healthcare",
            "vanity": "barchester-healthcare", "headquarters": "London, England"}
    lp.fetch_company = lambda slug: real if slug == "barchester-healthcare" else None
    try:
        got = public_resolver()("Barchester Healthcare Homes Limited")
        assert got and got["companyId"] == "80128", got

        # A page whose name doesn't match the term is skipped (guards a short slug
        # that hits an unrelated firm).
        lp.fetch_company = lambda slug: {"companyId": "999", "name": "Unrelated Widgets",
                                         "vanity": slug}
        assert public_resolver()("Sunny Meadows Care Home") is None
    finally:
        lp.fetch_company = orig
    print("OK — public_resolver: returns the name-matching page, skips mismatches")


def test_all_boilerplate_brand_still_matches():
    """A name that's entirely boilerplate ('Care UK' → care, uk both stopwords) must
    still match — common in this sector; the empty-token fallback handles it."""
    from resolve_company_id import _names_similar
    assert _names_similar("Care UK", "Care UK")
    assert _names_similar("Care UK Community Partnerships Ltd", "Care UK")
    assert not _names_similar("Care UK", "Health UK")     # different brand, not similar
    print("OK — _names_similar: all-boilerplate brand ('Care UK') still matches")


if __name__ == "__main__":
    test_slug_candidates()
    test_fetch_company_extracts_id_name_hq()
    test_public_resolver_picks_name_matching_page()
    test_all_boilerplate_brand_still_matches()
    print("\nAll linkedin_public tests passed.")
