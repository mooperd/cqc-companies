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
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, exists
from sqlalchemy.orm import Session

import companies_house as ch
# Canonical change-event directory (ADR 0015): the CH producer writes one
# companies-house-YYYY-MM-DD.json here per run, alongside cqc_refresh's cqc-*.json.
# Shared with the applier rather than re-declared so the location has one home.
import suppression
from apply_events import CHANGES_DIR
from model import ChangeEvent, Person, Provider, Role, db

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


def find_or_create_person(session, identity: Identity) -> tuple[Person | None, bool]:
    """Resolve `identity` to a global Person, creating one if none matches.
    Returns (person, created), or **(None, False) if the contact is suppressed**
    (ADR 0017 §5): an erased person must never be re-created by any ingest path, so
    the suppression check gates creation here, the single Person-creation choke
    point. See ADR 0014 for the correlation rules."""
    if suppression.is_suppressed(session, normalized_name=identity.normalized_name):
        return None, False

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
        acquired_at=dt.datetime.now(dt.timezone.utc),  # retention anchor (ADR 0017 §4)
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


# --- Change-event construction (ADR 0015 WS4) ---------------------------------

# Role fields whose change between two syncs is materially a "role changed".
_STATE_FIELDS = ("role_type", "start_date", "end_date", "control_nature", "confidence")


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def _role_state(role_obj: Role) -> dict:
    """The mutable facts of a persisted Role, for before/after change detection."""
    return {f: getattr(role_obj, f) for f in _STATE_FIELDS}


def _isoify(state: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in state.items()}


def _apply_role_fields(role_obj: Role, role: dict) -> None:
    role_obj.role_type = role["role_type"]
    role_obj.confidence = CONFIDENCE
    role_obj.start_date = role["start_date"]
    role_obj.end_date = role["end_date"]
    role_obj.control_nature = role["control_nature"]


def _person_payload(person: Person) -> dict:
    return {
        "name": person.name,
        "surname": person.surname,
        "forenames": person.forenames,
        "dob_year": person.dob_year,
        "dob_month": person.dob_month,
        "nationality": person.nationality,
    }


def _role_payload(role: dict) -> dict:
    return {
        "role_type": role["role_type"],
        "source": role["source"],
        "start_date": _iso(role["start_date"]),
        "end_date": _iso(role["end_date"]),
        "control_nature": role["control_nature"],
    }


def _role_dict_from_orm(role_obj: Role) -> dict:
    """A role dict (the shape `_role_from_officer/psc` produce) from a stored Role,
    so the seed dump and the live diff share one event format."""
    return {
        "role_type": role_obj.role_type,
        "source": role_obj.source,
        "start_date": role_obj.start_date,
        "end_date": role_obj.end_date,
        "control_nature": role_obj.control_nature,
    }


def _role_change(observed_at, change_type, provider, person, role_obj, role: dict,
                 details: dict | None = None):
    """Build the (file_event, change_event) pair for one role change.
    `effective_date` is the date the change took effect at source — the end date
    for an ending, else the start date. The DB `ChangeEvent` is None when
    `observed_at` is None (the seed dump and count-only syncs emit the file event
    but write no projection row)."""
    eff = role["end_date"] if change_type == "role_ended" else role["start_date"]
    payload = _role_payload(role)
    file_event = {
        "change_type": change_type,
        "source": role["source"],
        "effective_date": _iso(eff),
        "provider": {"cqc_provider_id": provider.cqc_provider_id, "name": provider.name},
        "person": _person_payload(person),
        "role": payload,
        "details": details or {},
    }
    change_event = None
    if observed_at is not None:
        change_event = ChangeEvent(
            observed_at=observed_at, effective_date=eff, source=role["source"],
            change_type=change_type, provider_id=provider.id, person_id=person.id,
            role_id=role_obj.id, details={"role": payload, **(details or {})},
        )
    return file_event, change_event


def sync_provider(session, provider: Provider, officers, pscs, observed_at=None) -> dict:
    """Correlate a provider's individual officers + PSCs into Person rows and
    upsert their Roles. Idempotent on (person, provider, source); only touches
    companies_house:* roles. Caller commits.

    Returns counts plus an `events` list of change-event dicts (role_appointed /
    role_ended / role_changed) for the canonical file. When `observed_at` is
    given, a matching `ChangeEvent` row is also written to the DB projection — so
    enrich's live walk both emits the file and updates the queryable table; the
    tests that only want correlation counts omit it."""
    provider_id = provider.id
    records = [(identity_from_officer(o), _role_from_officer(o))
               for o in officers if is_individual_director(o)]
    records += [(identity_from_psc(p), _role_from_psc(p))
                for p in pscs if is_individual_psc(p)]

    persons_created = 0
    best: dict[tuple[int, str], dict] = {}  # (person_id, source) -> best role
    persons_by_id: dict[int, Person] = {}
    for identity, role in records:
        if not identity.surname:
            continue  # unparseable name — skip rather than create a junk Person
        person, created = find_or_create_person(session, identity)
        if person is None:
            continue  # suppressed (erased) — never re-create (ADR 0017 §5)
        persons_created += int(created)
        persons_by_id[person.id] = person
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
    events: list[dict] = []
    for (person_id, source), role in best.items():
        existing_role = existing.get((person_id, source))
        person = persons_by_id[person_id]
        if existing_role is None:
            existing_role = Role(person_id=person_id, provider_id=provider_id, source=source)
            session.add(existing_role)
            _apply_role_fields(existing_role, role)
            session.flush()  # assign role.id for the ChangeEvent FK
            roles_created += 1
            file_event, change_event = _role_change(
                observed_at, "role_appointed", provider, person, existing_role, role)
        else:
            before = _role_state(existing_role)
            _apply_role_fields(existing_role, role)
            after = _role_state(existing_role)
            if before == after:
                continue  # genuine no-op re-sync — no change, no event
            roles_updated += 1
            change_type = ("role_ended"
                           if before["end_date"] is None and after["end_date"] is not None
                           else "role_changed")
            details = {"before": _isoify(before), "after": _isoify(after)}
            file_event, change_event = _role_change(
                observed_at, change_type, provider, person, existing_role, role, details)
        events.append(file_event)
        if change_event is not None:
            session.add(change_event)

    return {
        "persons_created": persons_created,
        "roles_created": roles_created,
        "roles_updated": roles_updated,
        "events": events,
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


def _should_repoll(provider: Provider, latest_filing_date: dt.date | None) -> bool:
    """Whether to re-fetch officers+PSC for this provider (ADR 0015 WS4). The
    cheap filing-history call gates the expensive re-poll: poll iff we've never
    successfully enriched it (the never-checked / previously-errored providers),
    or a newer officer/PSC filing has landed than our stored watermark."""
    if provider.ch_enriched_at is None:
        return True  # never enriched, or last attempt errored — always retry
    if latest_filing_date is None:
        return False  # no officer/PSC filings on record; nothing could have changed
    if not provider.ch_filing_watermark:
        return True  # enriched before WS4 existed — re-poll once to set a watermark
    # ISO dates sort lexically, so compare the strings directly — no parse needed.
    return latest_filing_date.isoformat() > provider.ch_filing_watermark


def _event_file_path(observed_at: dt.datetime, out_dir: Path = CHANGES_DIR) -> Path:
    return out_dir / f"companies-house-{observed_at.date():%Y-%m-%d}.json"


def write_event_file(events: list[dict], observed_at: dt.datetime,
                     out_dir: Path = CHANGES_DIR) -> Path:
    """Write the run's role change-events as the canonical companies-house-*.json
    (ADR 0015): the DB is a projection of this file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _event_file_path(observed_at, out_dir)
    payload = {
        "generated_at": observed_at.isoformat(),
        "source": "companies_house",
        "events": events,
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def build_seed_events(session) -> list[dict]:
    """Every current Companies House Role as a `role_appointed` event — the first
    companies-house-*.json (the seed). File-only: the roles already exist in the
    DB from the live enrichment run, so this serialises the baseline rather than
    mutating anything."""
    rows = (
        session.query(Role, Person, Provider)
        .join(Person, Role.person_id == Person.id)
        .join(Provider, Role.provider_id == Provider.id)
        .filter(Role.source.like("companies_house%"))
        .order_by(Provider.id, Role.id)
    )
    return [
        _role_change(None, "role_appointed", provider, person, role,
                     _role_dict_from_orm(role))[0]
        for role, person, provider in rows
    ]


def enrich_all(
    session,
    limit: int | None = None,
    sleep: float = _DEFAULT_SLEEP,
    dry_run: bool = False,
    skip_enriched: bool = False,
    out_dir: Path = CHANGES_DIR,
) -> dict[str, int]:
    """Walk providers with a CH number (ADR 0015 WS4). For each, one cheap
    filing-history call; only when a newer officer/PSC filing has landed (or the
    provider was never successfully enriched) do we re-fetch officers+PSC and
    sync Person+Role, emitting role change-events. Writes the run's events to a
    companies-house-*.json and updates each polled provider's watermark +
    ch_enriched_at. Commits in batches; a 404 skips that provider, a bad key
    aborts."""
    providers = providers_with_ch_number(session, limit, skip_enriched=skip_enriched)
    observed_at = dt.datetime.now(dt.timezone.utc)
    logger.info("checking %d providers (env=%s)", len(providers), ch.resolve_env())

    totals = {"checked": 0, "repolled": 0, "skipped_unchanged": 0,
              "persons_created": 0, "roles_created": 0, "roles_updated": 0,
              "events": 0, "not_found": 0, "errors": 0}
    all_events: list[dict] = []
    for i, provider in enumerate(providers, 1):
        # One pacing sleep per provider regardless of which branch we exit on
        # (rate-limit pacing); the `finally` runs before each `continue`.
        try:
            number = (provider.companies_house_number or "").strip()
            totals["checked"] += 1
            # Cheap gate: one filing-history call. A 404 here means an unknown or
            # dissolved company — skip it (no company left to poll). A transient
            # error that survived retries also skips; a bad key (401) raises
            # RuntimeError and aborts the whole run.
            try:
                filings = ch.fetch_filing_history(number)
            except ch.CompaniesHouseError as err:
                totals["not_found" if err.status == 404 else "errors"] += 1
                logger.warning("skip provider %s (CH %s, filing-history): %s",
                               provider.id, number, err)
                continue

            latest = ch.latest_relevant_filing_date(filings)
            if not _should_repoll(provider, latest):
                totals["skipped_unchanged"] += 1
                continue

            # A newer officer/PSC filing (or never enriched) — re-poll the heavy
            # endpoints and diff into role change-events.
            try:
                officers = ch.fetch_officers(number)
                pscs = ch.fetch_psc(number)  # 404 → [] inside fetch_psc
            except ch.CompaniesHouseError as err:
                totals["not_found" if err.status == 404 else "errors"] += 1
                logger.warning("skip provider %s (CH %s): %s", provider.id, number, err)
                continue

            stats = sync_provider(session, provider, officers, pscs, observed_at=observed_at)
            all_events.extend(stats["events"])
            totals["repolled"] += 1
            totals["events"] += len(stats["events"])
            for k in ("persons_created", "roles_created", "roles_updated"):
                totals[k] += stats[k]

            # Mark freshness: we polled now, and advance the watermark to the
            # latest officer/PSC filing so unchanged future runs skip it.
            provider.ch_enriched_at = observed_at
            if latest is not None:
                provider.ch_filing_watermark = latest.isoformat()

            if i % _COMMIT_EVERY == 0:
                if not dry_run:
                    session.commit()
                logger.info("  ...%d/%d checked; %s", i, len(providers), totals)
        finally:
            if sleep:
                time.sleep(sleep)

    if dry_run:
        session.rollback()
        logger.info("--dry-run: rolled back, no changes persisted, no file written")
    else:
        session.commit()
        if all_events:
            path = write_event_file(all_events, observed_at, out_dir)
            logger.info("wrote %s — %d role events", path, len(all_events))
        else:
            logger.info("no role changes this run — no event file written")
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
    p.add_argument(
        "--seed", action="store_true",
        help="write the seed companies-house-*.json (all current CH roles as "
             "role_appointed); no API calls, no DB changes",
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
        if args.seed:
            observed_at = dt.datetime.now(dt.timezone.utc)
            events = build_seed_events(session)
            path = write_event_file(events, observed_at)
            logger.info("seed: wrote %s — %d role events", path, len(events))
            return 0
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
