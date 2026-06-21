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

import argparse
import datetime as dt
import logging
import os
import sys
import time
from collections.abc import Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import companies_house as ch
from companies_house import Officer
from model import Provider, db
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


# --- WS4: walk providers and enrich from the live API -------------------------

# Companies House allows ~600 requests / 5 min (≈2/s). Pace requests so a full
# walk stays under the ceiling; the client's 429 backoff is the safety net.
_DEFAULT_SLEEP = 0.5
_COMMIT_EVERY = 50


def providers_with_ch_number(session, limit: int | None = None):
    """Providers that carry a Companies House number, ordered by id."""
    query = (
        session.query(Provider)
        .filter(
            Provider.companies_house_number.isnot(None),
            Provider.companies_house_number != "",
        )
        .order_by(Provider.id)
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def enrich_all(
    session, limit: int | None = None, sleep: float = _DEFAULT_SLEEP, dry_run: bool = False
) -> dict[str, int]:
    """Walk providers with a CH number, fetch officers (WS1) and sync director
    Person rows (WS2) for each. Commits in batches; a 404 skips that provider, a
    bad key (RuntimeError) aborts. Returns aggregate counts."""
    providers = providers_with_ch_number(session, limit)
    logger.info("enriching %d providers (env=%s)", len(providers), ch.resolve_env())

    totals = {"providers": 0, "created": 0, "updated": 0, "not_found": 0}
    for i, provider in enumerate(providers, 1):
        number = (provider.companies_house_number or "").strip()
        try:
            officers = ch.fetch_officers(number)
        except ch.CompaniesHouseError as err:
            totals["not_found"] += 1
            logger.warning("skip provider %s (CH %s): %s", provider.id, number, err)
            continue

        stats = sync_provider_directors(session, provider.id, officers)
        totals["providers"] += 1
        totals["created"] += stats["created"]
        totals["updated"] += stats["updated"]

        if i % _COMMIT_EVERY == 0:
            if not dry_run:
                session.commit()
            logger.info("  ...%d/%d providers; %s", i, len(providers), totals)
        if sleep:
            time.sleep(sleep)

    if dry_run:
        session.rollback()
        logger.info("--dry-run: rolled back, no changes persisted")
    else:
        session.commit()
    return totals


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enrich_directors",
        description="Populate director Person rows from Companies House (WS4).",
    )
    p.add_argument("--limit", type=int, default=None, help="process only the first N providers")
    p.add_argument(
        "--sleep", type=float, default=_DEFAULT_SLEEP,
        help=f"seconds between API calls (default {_DEFAULT_SLEEP}; rate-limit pacing)",
    )
    p.add_argument("--dry-run", action="store_true", help="fetch + sync, then roll back")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Loads COMPANIES_HOUSE_* and DATABASE_URL; .env.local overrides .env.
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    database_url = os.getenv(
        "DATABASE_URL", "postgresql://darwinist:darwinist@localhost:5432/darwinist"
    )
    engine = create_engine(database_url)
    db.metadata.create_all(engine)

    with Session(engine) as session:
        totals = enrich_all(
            session, limit=args.limit, sleep=args.sleep, dry_run=args.dry_run
        )
    logger.info("done: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
