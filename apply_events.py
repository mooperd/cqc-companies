"""Apply CQC change-event files to the database projection (ADR 0015 WS3).

The git-committed files under data/changes/ are canonical; this updates the DB
projection from them. `apply_pending` applies files not yet in the
applied_event_file ledger, in (source, date) order — idempotent and resumable.
Each change writes a ChangeEvent row; removals soft-delete the facility, and a
provider left with no active facilities is marked inactive.

CQC files (cqc-*.json) carry `directory` (output.csv shape) and `locations`
(Locations.csv shape) changes. Companies House files are produced/applied by
enrich_people (WS4); this module skips them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import cqc_mapping as cm
from model import AppliedEventFile, ChangeEvent, Facility, Provider, db

logger = logging.getLogger(__name__)

CHANGES_DIR = Path("data/changes")


def _set(obj, fields: dict) -> None:
    for k, v in fields.items():
        setattr(obj, k, v)


def _event(observed_at, change_type, provider_id, details) -> ChangeEvent:
    return ChangeEvent(
        observed_at=observed_at, source="cqc", change_type=change_type,
        provider_id=provider_id, details=details,
    )


def _removed_id(entry) -> str:
    return entry["id"] if isinstance(entry, dict) else entry


def apply_cqc_file(session, payload: dict, observed_at) -> dict:
    """Apply one cqc-*.json payload to the DB; returns counts. Caller commits."""
    prov_cache: dict[str, Provider | None] = {}
    fac_cache: dict[str, Facility | None] = {}
    touched_providers: set[int] = set()
    events: list[ChangeEvent] = []

    def provider_for(pid):
        if pid not in prov_cache:
            prov_cache[pid] = session.query(Provider).filter_by(cqc_provider_id=pid).first()
        return prov_cache[pid]

    def facility_for(lid):
        if lid not in fac_cache:
            fac_cache[lid] = session.query(Facility).filter_by(cqc_location_id=lid).first()
        return fac_cache[lid]

    directory = payload.get("directory", {})
    locations = payload.get("locations", {})

    # --- directory: upsert Provider + Facility -------------------------------
    for is_add, rows in (("added", directory.get("added", [])),
                         ("changed", directory.get("changed", []))):
        added = is_add == "added"
        for row in rows:
            pid = cm.directory_provider_key(row)
            if not pid:
                continue
            prov = provider_for(pid)
            if prov is None:
                prov = Provider(**cm.provider_fields(row), active=True)
                session.add(prov)
                session.flush()
                prov_cache[pid] = prov
                events.append(_event(observed_at, "provider_added", prov.id, {"cqc_provider_id": pid}))
            else:
                _set(prov, cm.provider_fields(row))
            touched_providers.add(prov.id)

            lid = cm.directory_location_key(row)
            if not lid:
                continue
            fac = facility_for(lid)
            if fac is None:
                fac = Facility(**cm.facility_fields(row), provider_id=prov.id, active=True)
                session.add(fac)
                session.flush()
                fac_cache[lid] = fac
                events.append(_event(observed_at, "location_added", prov.id, {"cqc_location_id": lid}))
            else:
                _set(fac, cm.facility_fields(row))
                fac.provider_id = prov.id
                fac.active, fac.removed_at = True, None
                if not added:
                    events.append(_event(observed_at, "location_updated", prov.id, {"cqc_location_id": lid}))

    for entry in directory.get("removed", []):
        fac = facility_for(_removed_id(entry))
        if fac and fac.active:
            fac.active, fac.removed_at = False, observed_at
            touched_providers.add(fac.provider_id)
            events.append(_event(observed_at, "location_removed", fac.provider_id, {"cqc_location_id": fac.cqc_location_id}))

    # --- locations: enrich Facility + provider CH number ---------------------
    for row in locations.get("added", []) + locations.get("changed", []):
        fac = facility_for(cm.locations_location_key(row))
        if fac is None:
            continue  # enrichment for a location not in the directory — skip
        _set(fac, cm.facility_enrichment_fields(row))
        ch = cm.provider_companies_house_number(row)
        if ch:
            prov = session.get(Provider, fac.provider_id)
            if prov:
                prov.companies_house_number = ch
        touched_providers.add(fac.provider_id)
        events.append(_event(observed_at, "location_updated", fac.provider_id,
                             {"cqc_location_id": fac.cqc_location_id, "enriched": True}))

    for entry in locations.get("removed", []):
        fac = facility_for(_removed_id(entry))
        if fac and fac.active:
            fac.active, fac.removed_at = False, observed_at
            touched_providers.add(fac.provider_id)
            events.append(_event(observed_at, "location_removed", fac.provider_id, {"cqc_location_id": fac.cqc_location_id}))

    # --- derive Provider.active = has any active facility ---------------------
    deactivated = 0
    for prov_id in touched_providers:
        prov = session.get(Provider, prov_id)
        if prov is None:
            continue
        has_active = session.query(Facility).filter_by(provider_id=prov_id, active=True).first() is not None
        if prov.active and not has_active:
            prov.active, prov.removed_at = False, observed_at
            events.append(_event(observed_at, "provider_removed", prov_id, {"active": False}))
            deactivated += 1
        elif not prov.active and has_active:
            prov.active, prov.removed_at = True, None

    session.add_all(events)
    return {"events": len(events), "providers_deactivated": deactivated}


def apply_pending(session, changes_dir: Path = CHANGES_DIR) -> dict:
    """Apply every cqc-*.json not yet in the ledger, in date order. Idempotent.

    Note: companies-house-*.json files are produced AND applied inline by
    enrich_people (WS4) — it writes the ChangeEvent rows as it polls, so it does
    not glob them here. A standalone `apply_ch_file` (file → DB, with ledger
    entries) for a from-scratch CH rebuild is deferred alongside the CQC
    `--rebuild` path (see docs/plans/data-freshness.md WS3/WS4)."""
    applied = {f.filename for f in session.query(AppliedEventFile)}
    pending = sorted(p for p in changes_dir.glob("cqc-*.json") if p.name not in applied)
    totals = {"files": 0, "events": 0, "providers_deactivated": 0}
    for path in pending:
        payload = json.loads(path.read_text(encoding="utf-8"))
        observed_at = dt.datetime.now(dt.timezone.utc)
        stats = apply_cqc_file(session, payload, observed_at)
        session.add(AppliedEventFile(filename=path.name, applied_at=observed_at))
        session.commit()
        totals["files"] += 1
        totals["events"] += stats["events"]
        totals["providers_deactivated"] += stats["providers_deactivated"]
        logger.info("applied %s — %s", path.name, stats)
    if not pending:
        logger.info("no pending change-event files")
    return totals


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="apply_events", description="Apply CQC change-event files to the DB.").parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)
    engine = create_engine(os.getenv("DATABASE_URL", "postgresql://darwinist:darwinist@localhost:5432/darwinist"))
    db.metadata.create_all(engine)
    with Session(engine) as session:
        totals = apply_pending(session)
    logger.info("done: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
