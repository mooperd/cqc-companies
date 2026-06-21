"""Map Companies House officers onto director `Person` rows.

WS2 of `docs/plans/companies-house-enrichment.md`, implementing
`docs/adr/0013-companies-house-source.md`. Given a provider and the officers
Companies House reports for its company number, create/update director `Person`
rows with `source='companies_house'`.

Scope (WS2): the director-role filter, the Officer→Person field mapping, and
idempotent upsert keyed on (provider, person name) so a re-run updates rather
than duplicates. This only ever touches `source='companies_house'` rows.

NOT in scope here (that's WS3): cross-source conflict resolution — manual rows
winning, LinkedIn filling the non-director gap, role-currency merge precedence.
The CLI that walks every provider and calls the live API (WS1) + this sync is
WS4.

Pure functions (`is_director_role`, `dedupe_by_identity`) need no DB; the
`Officer` type comes from `companies_house` and the `Person` model from `model`.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable

from companies_house import Officer
from model import Person

logger = logging.getLogger(__name__)

SOURCE = "companies_house"
# CH is authoritative for director identity + appointment status (ADR 0013 §3),
# so director rows it asserts are high-confidence.
CONFIDENCE = "high"


def is_director_role(role: str | None) -> bool:
    """True for director-class officer roles (director, corporate-director,
    nominee-director, …); False for secretaries and nominee-secretaries."""
    return "director" in (role or "").lower()


def _name_key(name: str | None) -> str:
    """Identity key for a person: whitespace-collapsed, case-folded name."""
    return " ".join((name or "").split()).casefold()


def dedupe_by_identity(officers: Iterable[Officer]) -> list[Officer]:
    """One officer per person-name. When a name appears more than once (resigned
    then re-appointed), prefer an active appointment, then the latest
    `appointed_on` — so the surviving row reflects the person's current standing.

    Sort so the preferred record comes last (active after resigned, later
    appointment after earlier), then let the dict keep the last value per name.
    """
    keyed = [(o, _name_key(o.name)) for o in officers if _name_key(o.name)]
    keyed.sort(key=lambda t: (t[0].is_active, t[0].appointed_on or dt.date.min))
    return list({key: officer for officer, key in keyed}.values())


def sync_provider_directors(
    session, provider_id: int, officers: Iterable[Officer]
) -> dict[str, int]:
    """Upsert director `Person` rows for one provider from CH officers.

    Idempotent on (provider, name): an existing `companies_house`-sourced Person
    is updated in place; a new one is inserted. Non-director officers are
    skipped. Returns counts {created, updated, skipped_non_director}; note
    created+updated count distinct directors (post-dedupe) while
    skipped_non_director counts raw non-director officers, so the three need not
    sum to the total when a person has repeat appointments.

    Caller is responsible for committing the session.
    """
    officers = list(officers)
    director_officers = [o for o in officers if is_director_role(o.role)]
    directors = dedupe_by_identity(director_officers)

    existing = {
        _name_key(p.name): p
        for p in session.query(Person).filter_by(
            provider_id=provider_id, source=SOURCE
        )
    }

    created = updated = 0
    for o in directors:
        person = existing.get(_name_key(o.name))
        if person is None:
            person = Person(name=o.name, provider_id=provider_id, source=SOURCE)
            session.add(person)
            created += 1
        else:
            updated += 1
        person.role = o.role
        person.confidence = CONFIDENCE
        person.appointment_date = o.appointed_on
        person.resignation_date = o.resigned_on

    skipped = len(officers) - len(director_officers)
    return {"created": created, "updated": updated, "skipped_non_director": skipped}
