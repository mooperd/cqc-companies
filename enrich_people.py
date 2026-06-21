"""Populate Person + Role from Companies House officers and persons with
significant control.

WS2/WS4 of `docs/plans/companies-house-enrichment.md`, implementing
[ADR 0013](docs/adr/0013-companies-house-source.md) (officers + PSC) and
[ADR 0014](docs/adr/0014-person-role-correlation-model.md) (Person/Role + global
correlation).

Per provider with a Companies House number: fetch officers and PSCs, keep
individuals only (directors and persons with significant control), correlate
each into a global `Person`, and attach a `Role` (role_type, namespaced source,
dates, control_nature).

Correlation (ADR 0014): a record matches an existing Person when the partial DOB
(year+month), normalized surname, and normalized first forename all match and
nationality doesn't conflict. Officer names parse as "SURNAME, Forenames"; PSC
names as "[Title] Forenames Surname" — both reduce to the same identity so the
same human links across endpoints. Records without a DOB aren't merged into a
DOB-anchored identity; they correlate only by exact normalized name and are
flagged match_confidence='low'.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
import time
from dataclasses import dataclass

from sqlalchemy import create_engine, exists
from sqlalchemy.orm import Session

import companies_house as ch
from model import Person, Provider, Role, db

logger = logging.getLogger(__name__)

SOURCE_OFFICERS = "companies_house:officers"
SOURCE_PSC = "companies_house:psc"
# CH is authoritative for the director/PSC facts it reports (ADR 0013 §3).
CONFIDENCE = "high"

_DEFAULT_SLEEP = 0.5
_COMMIT_EVERY = 50
_MIN_DATE = dt.date.min
# Name prefixes to drop before parsing PSC "[Title] Forenames Surname".
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "sir", "dame", "lord", "lady",
           "prof", "professor", "rev", "mx", "the"}


# --- Identity parsing + correlation (pure) ------------------------------------


@dataclass(frozen=True)
class Identity:
    display_name: str
    surname: str       # normalized (lowercase)
    forenames: str     # normalized (lowercase, space-joined)
    dob_year: int | None
    dob_month: int | None
    nationality: str | None

    @property
    def first_forename(self) -> str:
        return self.forenames.split()[0] if self.forenames else ""

    @property
    def normalized_name(self) -> str:
        return f"{self.surname} {self.forenames}".strip()


def _norm(value: str | None) -> str:
    """Lowercase and strip punctuation (keep letters, digits, spaces, hyphens)."""
    return re.sub(r"[^a-z0-9\s-]", "", (value or "").lower()).strip()


def _split_officer_name(name: str) -> tuple[str, str]:
    """Officer format 'SURNAME, Forenames' → (surname, forenames), normalized."""
    surname, _, forenames = name.partition(",")
    return _norm(surname), _norm(forenames)


def _split_psc_name(name: str) -> tuple[str, str]:
    """PSC format '[Title] Forenames Surname' → (surname, forenames), normalized."""
    tokens = _norm(name).split()
    while tokens and tokens[0] in _TITLES:
        tokens = tokens[1:]
    if not tokens:
        return "", ""
    return tokens[-1], " ".join(tokens[:-1])


def identity_from_officer(o: ch.Officer) -> Identity:
    surname, forenames = _split_officer_name(o.name)
    return Identity(o.name, surname, forenames, o.dob_year, o.dob_month, o.nationality)


def identity_from_psc(p: ch.PSC) -> Identity:
    surname, forenames = _split_psc_name(p.name)
    return Identity(p.name, surname, forenames, p.dob_year, p.dob_month, p.nationality)


def is_individual_director(o: ch.Officer) -> bool:
    """An individual director-class officer (not a corporate director, not a
    secretary). nominee-director (a real person) is kept."""
    role = (o.role or "").lower()
    return "director" in role and "corporate" not in role


def is_individual_psc(p: ch.PSC) -> bool:
    """An individual person with significant control (not a corporate/legal
    entity, not a super-secure anonymized entry)."""
    return p.kind == "individual-person-with-significant-control"


def _nationalities_conflict(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return _norm(a) != _norm(b)


# --- Person find-or-create (correlation) --------------------------------------


def find_or_create_person(session, identity: Identity) -> tuple[Person, bool]:
    """Resolve `identity` to a global Person, creating one if none matches.
    Returns (person, created). See ADR 0014 for the correlation rules."""
    if identity.dob_year and identity.dob_month:
        # DOB-anchored: candidates share DOB + surname; confirm first forename
        # and a non-conflicting nationality.
        candidates = session.query(Person).filter_by(
            dob_year=identity.dob_year,
            dob_month=identity.dob_month,
            surname=identity.surname,
        )
        for person in candidates:
            person_first = (person.forenames or "").split()
            if person_first and person_first[0] == identity.first_forename and \
                    not _nationalities_conflict(person.nationality, identity.nationality):
                return person, False
        match_confidence = "high"
    else:
        # No DOB anchor: correlate only by exact normalized name, among other
        # low-confidence (DOB-less) people, so a re-run stays idempotent without
        # merging into a DOB-anchored identity.
        existing = session.query(Person).filter_by(
            normalized_name=identity.normalized_name, dob_year=None
        ).first()
        if existing is not None:
            return existing, False
        match_confidence = "low"

    person = Person(
        name=identity.display_name,
        surname=identity.surname,
        forenames=identity.forenames,
        normalized_name=identity.normalized_name,
        dob_year=identity.dob_year,
        dob_month=identity.dob_month,
        nationality=identity.nationality,
        match_confidence=match_confidence,
    )
    session.add(person)
    session.flush()  # assign id + make findable by later lookups this run
    return person, True


# --- Role mapping + per-provider sync -----------------------------------------


def _role_from_officer(o: ch.Officer) -> dict:
    return {
        "role_type": "director",
        "source": SOURCE_OFFICERS,
        "start_date": o.appointed_on,
        "end_date": o.resigned_on,
        "control_nature": None,
    }


def _role_from_psc(p: ch.PSC) -> dict:
    return {
        "role_type": "psc",
        "source": SOURCE_PSC,
        "start_date": p.notified_on,
        "end_date": p.ceased_on,
        "control_nature": ", ".join(p.natures_of_control) or None,
    }


def _supersedes(a: dict, b: dict) -> bool:
    """Prefer an active role (no end_date), then the later start_date — so when
    one person has repeat appointments the surviving Role reflects current
    standing. Ties keep the later-seen role."""
    a_active, b_active = a["end_date"] is None, b["end_date"] is None
    if a_active != b_active:
        return a_active
    return (a["start_date"] or _MIN_DATE) >= (b["start_date"] or _MIN_DATE)


def sync_provider(session, provider_id: int, officers, pscs) -> dict[str, int]:
    """Correlate a provider's individual officers + PSCs into Person rows and
    upsert their Roles. Idempotent on (person, provider, source); only touches
    companies_house:* roles. Caller commits."""
    records = [(identity_from_officer(o), _role_from_officer(o))
               for o in officers if is_individual_director(o)]
    records += [(identity_from_psc(p), _role_from_psc(p))
                for p in pscs if is_individual_psc(p)]

    persons_created = 0
    best: dict[tuple[int, str], dict] = {}  # (person_id, source) -> best role
    for identity, role in records:
        if not identity.surname:
            continue  # unparseable name — skip rather than create a junk Person
        person, created = find_or_create_person(session, identity)
        persons_created += int(created)
        key = (person.id, role["source"])
        if key not in best or _supersedes(role, best[key]):
            best[key] = role

    # Existing CH roles for this provider, in one query — keyed (person, source)
    # to match `best` for the upsert.
    existing = {
        (r.person_id, r.source): r
        for r in session.query(Role).filter(
            Role.provider_id == provider_id,
            Role.source.like("companies_house%"),
        )
    }
    roles_created = roles_updated = 0
    for (person_id, source), role in best.items():
        existing_role = existing.get((person_id, source))
        if existing_role is None:
            existing_role = Role(person_id=person_id, provider_id=provider_id, source=source)
            session.add(existing_role)
            roles_created += 1
        else:
            roles_updated += 1
        existing_role.role_type = role["role_type"]
        existing_role.confidence = CONFIDENCE
        existing_role.start_date = role["start_date"]
        existing_role.end_date = role["end_date"]
        existing_role.control_nature = role["control_nature"]

    return {
        "persons_created": persons_created,
        "roles_created": roles_created,
        "roles_updated": roles_updated,
    }


# --- WS4: walk providers and enrich from the live API -------------------------


def providers_with_ch_number(session, limit: int | None = None, skip_enriched: bool = False):
    """Providers carrying a Companies House number, ordered by id. With
    `skip_enriched`, exclude providers that already have a companies_house Role
    (resume an interrupted run)."""
    query = (
        session.query(Provider)
        .filter(
            Provider.companies_house_number.isnot(None),
            Provider.companies_house_number != "",
        )
        .order_by(Provider.id)
    )
    if skip_enriched:
        already = exists().where(
            (Role.provider_id == Provider.id) & (Role.source.like("companies_house%"))
        )
        query = query.filter(~already)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def enrich_all(
    session,
    limit: int | None = None,
    sleep: float = _DEFAULT_SLEEP,
    dry_run: bool = False,
    skip_enriched: bool = False,
) -> dict[str, int]:
    """Walk providers with a CH number; for each, fetch officers + PSCs (WS1) and
    sync Person+Role (WS2). Commits in batches; a 404 on officers skips that
    provider, a bad key aborts. Returns aggregate counts."""
    providers = providers_with_ch_number(session, limit, skip_enriched=skip_enriched)
    logger.info("enriching %d providers (env=%s)", len(providers), ch.resolve_env())

    totals = {"providers": 0, "persons_created": 0, "roles_created": 0,
              "roles_updated": 0, "not_found": 0}
    for i, provider in enumerate(providers, 1):
        number = (provider.companies_house_number or "").strip()
        try:
            officers = ch.fetch_officers(number)
        except ch.CompaniesHouseError as err:
            totals["not_found"] += 1
            logger.warning("skip provider %s (CH %s): %s", provider.id, number, err)
            continue
        pscs = ch.fetch_psc(number)  # 404 → [] inside fetch_psc

        stats = sync_provider(session, provider.id, officers, pscs)
        totals["providers"] += 1
        for k in ("persons_created", "roles_created", "roles_updated"):
            totals[k] += stats[k]

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
        prog="enrich_people",
        description="Populate Person + Role from Companies House officers + PSC.",
    )
    p.add_argument("--limit", type=int, default=None, help="process only the first N providers")
    p.add_argument(
        "--sleep", type=float, default=_DEFAULT_SLEEP,
        help=f"seconds between API calls (default {_DEFAULT_SLEEP}; rate-limit pacing)",
    )
    p.add_argument("--dry-run", action="store_true", help="fetch + sync, then roll back")
    p.add_argument(
        "--skip-enriched", action="store_true",
        help="skip providers that already have CH roles (resume a run)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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
            session,
            limit=args.limit,
            sleep=args.sleep,
            dry_run=args.dry_run,
            skip_enriched=args.skip_enriched,
        )
    logger.info("done: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
