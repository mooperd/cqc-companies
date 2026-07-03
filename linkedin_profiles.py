"""The LinkedIn profile ingest contract: a `ScrapedProfile` and the row→profile
parser (ADR 0016 §3).

This is deliberately *transport-free* — it maps whatever result rows an
acquisition layer returns into the small set of fields the CRM needs, tolerant of
the key-name variation across phantoms. It used to live in the stdlib
`phantombuster.py` transport module; that transport was retired in favour of
[`phantombuster-lib`](https://github.com/mooperd/phantombuster-lib) (PWS1), but
the row→identity mapping stays ours and is reused by the acquisition consumer
(PWS3): the lib *acquires* rows, this module turns them into `ScrapedProfile`s,
and `enrich_linkedin` correlates those into `Person`/`Role`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScrapedProfile:
    """One LinkedIn profile a phantom returned — only the fields the CRM needs."""

    name: str
    linkedin_url: str | None
    headline: str | None
    company: str | None
    location: str | None


def _first(item: dict, *keys: str) -> str | None:
    """First non-empty value among `keys` (lowercase; caller lowercases the row)."""
    for key in keys:
        value = item.get(key)
        if value:
            return str(value).strip()
    return None


def parse_profile(item: dict) -> ScrapedProfile:
    """Map one phantom result row to a ScrapedProfile, tolerant of key variation.
    Keys are matched case-insensitively — Phantombuster is inconsistent about
    `linkedIn` vs `linkedin` casing across phantoms (spike: phantombuster-api)."""
    item = {str(k).lower(): v for k, v in item.items()}
    name = _first(item, "fullname", "name")
    if not name:
        first = _first(item, "firstname")
        last = _first(item, "lastname")
        name = " ".join(p for p in (first, last) if p) or ""
    return ScrapedProfile(
        name=name,
        linkedin_url=_first(
            item, "linkedinprofileurl", "profileurl", "linkedinurl", "profilelink",
        ),
        # `job` is the Company Employees Export field ("Nursing Manager at Care UK");
        # the others cover Profile/Search phantoms (confirmed live 2026-07-02).
        headline=_first(item, "job", "headline", "title", "jobtitle", "occupation"),
        # No companyName in Company Employees Export output — the company is in
        # `job` and, authoritatively, in `query` (the input company URL).
        company=_first(item, "companyname", "company", "currentcompany"),
        location=_first(item, "location", "locationname"),
    )


def parse_profiles(rows: list[dict]) -> list[ScrapedProfile]:
    """Map a phantom's result rows to ScrapedProfiles, dropping rows with no name
    (corporate/blank entries — `Person` is contactable humans only, ADR 0014 §5)."""
    profiles = [parse_profile(row) for row in rows]
    return [p for p in profiles if p.name]
