# Plan — Change-event files: git event log → DB projection (CQC + Companies House)

**Status:** Proposed.

<!-- Status lifecycle: Proposed → Active → Closed (YYYY-MM-DD) -->

## Goal

Implement [ADR 0015](../adr/0015-data-freshness-strategy.md): each monthly refresh
writes a git-committed, per-source, timestamped **change-event file**; the
database becomes a **projection** that those files rebuild (replay) or update
(apply-latest). CQC files come from a bulk snapshot-diff; Companies House files
from a filing-history check that re-polls only changed companies. The event log
is also the outreach-trigger substrate (Phase 4).

## Prerequisites

- [ADR 0015](../adr/0015-data-freshness-strategy.md) Accepted.
- First full CH enrichment landed (2026-06-21: 95,629 people / 155,628 roles;
  554 errored → re-tried via `ch_enriched_at`). This becomes the CH **seed file**.
- The monthly `cqc_refresh` cron ([`cqc-bulk-ingest.md`](cqc-bulk-ingest.md)).

## Where things stand (2026-06-21)

Nothing built. `cqc_refresh` overwrites the full CSVs; CH state lives only in the
DB; no change-event files, no projection/replay, no removals.

## Workstreams

### WS1 — Schema: event-file projections + markers

**Status:** Open.

`change_events` (observed_at, effective_date, source, change_type, provider_id FK,
person_id/role_id FK nullable, details JSON) — the queryable materialization of
the files. `applied_event_file` ledger (filename, applied_at). Additive
`Provider.active` (default true), `removed_at`, `ch_enriched_at`,
`ch_filing_watermark`. Additive per [ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md).

**Exit:** tables/columns build; a change_event inserts and queries by provider.

### WS2 — CQC change-event file producer

**Status:** Shipped (2026-06-22).

`cqc_refresh._cmd_refresh` now treats `output.csv`/`Locations.csv` as an
immutable seed: it maps the new bulk into id→row indexes (keyed on
`CQC Location ID` / `Location ID`, projected to the canonical header),
reconstructs the baseline (`_load_seed_index` + `_replay_prior_deltas`), diffs
(`_diff_index`), and writes `data/changes/cqc-YYYY-MM-DD.json` (added/changed
full rows, removed id+name). Seed CSVs are no longer written. `write_csv`
(dead) removed.

**Exit:** ✓ `test_cqc_refresh.py` covers index/projection, diff
(added/changed/removed), apply, and the **seed + replay(delta) == new snapshot**
invariant. (Live end-to-end runs when the next bulk lands / via WS5.)

### WS3 — Apply/replay engine (event files → DB projection)

**Status:** Open.

`apply_events`: **replay** (rebuild — seed import then all files in (source,date)
order) and **apply-latest** (incremental — files not in `applied_event_file`).
Upsert added/changed by key; soft-delete removed; write `change_events` rows.
Idempotent/resumable. Seed bulk-import reuses
[ADR 0005](../adr/0005-two-stage-csv-ingest.md) importers.

**Exit:** rebuild-from-scratch and incremental-apply produce identical DB state;
re-apply is a no-op; a removed provider is soft-deleted.

### WS4 — Companies House change-event file producer

**Status:** Open. (Extends `enrich_people.py`.)

Monthly: per provider with a CH number, one `filing-history` call; if the latest
officer/PSC-category filing is newer than `ch_filing_watermark`, re-fetch
officers+PSC, diff against current roles (from prior CH files / DB), emit
`role_appointed|ended|changed` events to `data/changes/companies-house-YYYY-MM-DD.json`;
update watermark + `ch_enriched_at`. The first file is the seed (current 155k
roles as `*_added`). Never-checked/errored providers always polled.

**Exit:** a provider that filed an officer change is re-polled and emits the right
events; an unchanged provider is skipped after the cheap check.

### WS5 — Simplify the refresh workflow

**Status:** Open.

`cqc-refresh.yml` commits the small new event file (+ watermark/state) instead of
force-pushing 40 MB CSVs; PR body = change summary. Add the CH producer to the
schedule (or its own workflow).

**Exit:** a dispatch run opens a PR adding only an event file.

### WS6 — (Deferred) consumers

**Status:** Deferred. Outreach `Task` from `change_events` (Phase 4); CH
**Streaming API** as a third file producer (deploy phase). Built ready.

## Phase exit criteria

- [ ] WS1 — change_events + markers in schema.
- [ ] WS2 — CQC emits event files; seed not overwritten.
- [ ] WS3 — replay rebuilds and apply-latest updates the DB identically; removals soft-deleted.
- [ ] WS4 — CH filing-history poll emits role events for changed companies only.
- [ ] WS5 — workflow commits event files.
- [ ] ADR 0015 Proposed → Accepted.

## References

- [ADR 0015 — Change-event files / DB projection](../adr/0015-data-freshness-strategy.md)
- [`companies-house-enrichment.md`](companies-house-enrichment.md) / [`cqc-bulk-ingest.md`](cqc-bulk-ingest.md)
- [ADR 0005](../adr/0005-two-stage-csv-ingest.md) · [ADR 0007](../adr/0007-csvs-checked-into-repo.md) · [ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md)
