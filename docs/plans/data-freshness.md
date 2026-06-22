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

### WS3 — Apply engine (event files → DB projection)

**Status:** Shipped (2026-06-22). WS3a (Facility.active), WS3b (shared
`cqc_mapping`; importers keyed on `cqc_provider_id`), WS3c (`apply_events`).

`apply_events.apply_pending` applies cqc-*.json files not in the
`applied_event_file` ledger, in date order (idempotent/resumable). Per file:
upsert Provider+Facility by id via `cqc_mapping`, enrich from locations rows,
soft-delete `removed` facilities, derive `Provider.active = has any active
facility`, and write a `ChangeEvent` per change. Seed still bulk-imported once
([ADR 0005](../adr/0005-two-stage-csv-ingest.md), now sharing `cqc_mapping`).

**Exit:** ✓ `test_apply_events.py` — add/change/remove, enrich, soft-delete,
provider deactivation, ChangeEvents, and ledger idempotency, on SQLite.
*(Full `--rebuild` mode — seed import then replay all — deferred; apply_pending
+ the existing seed importers cover it manually.)*

### WS4 — Companies House change-event file producer

**Status:** Shipped (2026-06-22). (Extends `enrich_people.py` + `companies_house.py`.)

`companies_house.fetch_filing_history` + `latest_relevant_filing_date` (officer/PSC
categories only) drive a cheap gate: `enrich_all` does one filing-history call per
provider, and only when `_should_repoll` says so (never-enriched/errored, or a
newer officer/PSC filing than `ch_filing_watermark`) re-fetches officers+PSC.
`sync_provider` diffs against current roles and emits
`role_appointed|ended|changed` events **and** `ChangeEvent` rows (produce + apply
inline — it writes the projection as it polls), accumulated into
`data/changes/companies-house-YYYY-MM-DD.json`; each polled provider's watermark +
`ch_enriched_at` advance. `--seed` dumps the current 155k roles as the first
file (file-only; roles already exist from the live run).

A standalone `apply_ch_file` (companies-house-*.json → DB, with `applied_event_file`
ledger entries) for a from-scratch CH rebuild is **deferred** alongside the CQC
`--rebuild` path (WS3) — `apply_events.apply_pending` still globs `cqc-*.json` only.

**Exit:** ✓ `test_enrich_people.py` / `test_companies_house.py` cover the
filing-history parse/paginate/404, the `_should_repoll` gate, appointed/ended/
changed/no-op events, the seed dump, and a full `enrich_all` integration
(skip-unchanged + re-poll-changed + file write). A **live** API run (real key +
real filings) is still unproven, same caveat as the CQC producer.

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
- [x] WS4 — CH filing-history poll emits role events for changed companies only.
- [ ] WS5 — workflow commits event files.
- [ ] ADR 0015 Proposed → Accepted.

## References

- [ADR 0015 — Change-event files / DB projection](../adr/0015-data-freshness-strategy.md)
- [`companies-house-enrichment.md`](companies-house-enrichment.md) / [`cqc-bulk-ingest.md`](cqc-bulk-ingest.md)
- [ADR 0005](../adr/0005-two-stage-csv-ingest.md) · [ADR 0007](../adr/0007-csvs-checked-into-repo.md) · [ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md)
