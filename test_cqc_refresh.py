#!/usr/bin/env python3
"""Tests for the CQC change-event diff/replay logic (ADR 0015 WS2).

Pure functions — no network, no live CQC. Run: python test_cqc_refresh.py
"""

import json
import tempfile
from pathlib import Path

import cqc_refresh as cr

_HDR = ["id", "Name", "val"]


def _rows(*triples):
    return [{"id": i, "Name": n, "val": v} for i, n, v in triples]


def test_index_by_key_projects_and_skips_empty():
    idx = cr._index_by_key(
        [{"id": "1", "Name": "A", "val": "x", "extra": "drop"},
         {"id": "", "Name": "blank", "val": "y"}],
        "id", _HDR,
    )
    assert set(idx) == {"1"}                       # empty-key row skipped
    assert idx["1"] == {"id": "1", "Name": "A", "val": "x"}  # projected to header
    print("OK — _index_by_key: projects to header, skips empty keys")


def test_diff_index():
    baseline = cr._index_by_key(_rows(("1", "A", "x"), ("2", "B", "y"), ("3", "C", "z")), "id", _HDR)
    new = cr._index_by_key(_rows(("1", "A", "x"), ("2", "B", "CHANGED"), ("4", "D", "w")), "id", _HDR)
    diff = cr._diff_index(baseline, new, "Name")
    assert [r["id"] for r in diff["added"]] == ["4"]
    assert [r["id"] for r in diff["changed"]] == ["2"]
    assert diff["removed"] == [{"id": "3", "name": "C"}]
    print("OK — _diff_index: added / changed / removed by id")


def test_apply_change_to_index():
    idx = cr._index_by_key(_rows(("1", "A", "x"), ("2", "B", "y")), "id", _HDR)
    change = {
        "added": _rows(("3", "C", "z")),
        "changed": _rows(("1", "A", "NEW")),
        "removed": [{"id": "2", "name": "B"}],
    }
    cr._apply_change_to_index(idx, change, "id")
    assert set(idx) == {"1", "3"}
    assert idx["1"]["val"] == "NEW"
    print("OK — _apply_change_to_index: add/change/remove")


def test_replay_reconstructs_state():
    # Invariant: seed + replay(delta) == the new snapshot the delta was diffed to.
    # Uses the real DIRECTORY_KEY so _replay_prior_deltas (which hardcodes it) works.
    k = cr.DIRECTORY_KEY
    hdr = [k, "Name", "val"]

    def rows(*triples):
        return [{k: i, "Name": n, "val": v} for i, n, v in triples]

    baseline = cr._index_by_key(rows(("1", "A", "x"), ("2", "B", "y"), ("3", "C", "z")), k, hdr)
    new = cr._index_by_key(rows(("1", "A", "x"), ("2", "B", "CHANGED"), ("4", "D", "w")), k, hdr)
    diff = cr._diff_index(baseline, new, "Name")

    with tempfile.TemporaryDirectory() as tmp:
        changes = Path(tmp)
        (changes / "cqc-2026-01-01.json").write_text(
            json.dumps({"directory": diff, "locations": {"added": [], "changed": [], "removed": []}}),
            encoding="utf-8",
        )
        orig = cr.CHANGES_DIR
        cr.CHANGES_DIR = changes
        try:
            reconstructed = cr._index_by_key(
                rows(("1", "A", "x"), ("2", "B", "y"), ("3", "C", "z")), k, hdr)
            cr._replay_prior_deltas(reconstructed, {}, exclude="none.json")
        finally:
            cr.CHANGES_DIR = orig
    assert reconstructed == new, "seed + replay(delta) must equal the new snapshot"
    print("OK — replay reconstructs state (seed + delta == new)")


if __name__ == "__main__":
    test_index_by_key_projects_and_skips_empty()
    test_diff_index()
    test_apply_change_to_index()
    test_replay_reconstructs_state()
    print("\nAll cqc_refresh delta tests passed.")
