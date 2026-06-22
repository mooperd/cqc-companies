#!/usr/bin/env python3
"""Tests for apply_events — CQC change-event files → DB projection (ADR 0015 WS3c).

In-memory SQLite; no network. Run: python test_apply_events.py
"""

import json
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import apply_events as ae
from model import AppliedEventFile, ChangeEvent, Facility, Provider, db


def _dir_row(pid, lid, name="Loc", provider_name="Prov", website=""):
    return {
        "CQC Provider ID (for office use only)": pid,
        "CQC Location ID (for office use only)": lid,
        "Name": name, "Provider name": provider_name, "Website": website,
    }


def _loc_row(lid, **extra):
    return {"Location ID": lid, **extra}


def _session_with_seed():
    eng = create_engine("sqlite://")
    db.metadata.create_all(eng)
    s = Session(eng)
    # Seed: provider A with location L1; provider C with its only location L4.
    pa = Provider(name="A", cqc_provider_id="1-A", active=True)
    pc = Provider(name="C", cqc_provider_id="1-C", active=True)
    s.add_all([pa, pc])
    s.flush()
    s.add_all([
        Facility(name="L1 old", cqc_location_id="1-L1", provider_id=pa.id, active=True),
        Facility(name="L4", cqc_location_id="1-L4", provider_id=pc.id, active=True),
    ])
    s.commit()
    return s


def _by_loc(s, lid):
    return s.query(Facility).filter_by(cqc_location_id=lid).first()


def _by_prov(s, pid):
    return s.query(Provider).filter_by(cqc_provider_id=pid).first()


def test_apply_cqc_file_add_change_remove_deactivate():
    with _session_with_seed() as s:
        payload = {
            "directory": {
                "added": [
                    _dir_row("1-A", "1-L2", name="L2"),        # new location, existing provider
                    _dir_row("1-B", "1-L3", name="L3", provider_name="B"),  # new provider + location
                ],
                "changed": [_dir_row("1-A", "1-L1", name="L1 NEW")],  # rename existing facility
                "removed": [{"id": "1-L4", "name": "L4"}],     # provider C's only location
            },
            "locations": {
                "added": [], "removed": [],
                "changed": [_loc_row("1-L1", **{
                    "Registered manager": "Jane", "Care homes beds": "10",
                    "Provider Companies House Number": "07654321",
                })],
            },
        }
        import datetime as dt
        stats = ae.apply_cqc_file(s, payload, dt.datetime(2026, 2, 1))
        s.commit()

        # Facilities
        assert _by_loc(s, "1-L1").name == "L1 NEW"                      # changed
        assert _by_loc(s, "1-L1").registered_manager == "Jane"          # enriched
        assert _by_loc(s, "1-L1").care_home_beds == 10
        assert _by_loc(s, "1-L2").active and _by_loc(s, "1-L3").active  # added
        assert _by_loc(s, "1-L4").active is False                       # removed (soft-delete)
        # Providers
        assert _by_prov(s, "1-B") is not None                          # new provider
        assert _by_prov(s, "1-A").companies_house_number == "07654321"  # CH from enrichment
        assert _by_prov(s, "1-A").active is True                        # still has active facilities
        assert _by_prov(s, "1-C").active is False                       # no active facilities left
        # Events
        types = sorted(e.change_type for e in s.query(ChangeEvent))
        for expected in ("provider_added", "provider_removed", "location_added",
                         "location_removed", "location_updated"):
            assert expected in types, (expected, types)
        assert stats["providers_deactivated"] == 1
    print("OK — apply_cqc_file: add/change/remove, enrich, soft-delete, deactivate, events")


def test_apply_pending_idempotent():
    with _session_with_seed() as s, tempfile.TemporaryDirectory() as tmp:
        changes = Path(tmp)
        (changes / "cqc-2026-02-01.json").write_text(json.dumps({
            "directory": {"added": [_dir_row("1-B", "1-L3", provider_name="B")],
                          "changed": [], "removed": []},
            "locations": {"added": [], "changed": [], "removed": []},
        }), encoding="utf-8")

        first = ae.apply_pending(s, changes_dir=changes)
        assert first["files"] == 1 and _by_prov(s, "1-B") is not None
        assert {f.filename for f in s.query(AppliedEventFile)} == {"cqc-2026-02-01.json"}

        # Re-run: ledger skips the already-applied file.
        second = ae.apply_pending(s, changes_dir=changes)
        assert second["files"] == 0
        assert s.query(Provider).filter_by(cqc_provider_id="1-B").count() == 1  # no dup
    print("OK — apply_pending: ledger makes re-application a no-op")


if __name__ == "__main__":
    test_apply_cqc_file_add_change_remove_deactivate()
    test_apply_pending_idempotent()
    print("\nAll apply_events tests passed.")
