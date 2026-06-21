# ADR 0015 — Change-event files as the source of truth; the database as a projection

**Status:** Proposed.

<!--
Supersedes the regenerate-and-overwrite refresh (ADR 0007 Amendment / cqc-bulk-
ingest plan); reframes ADR 0005 ingest as "replay event files into a projection".
Move to Accepted once the event-file pipeline rebuilds + incrementally applies a DB.
-->

**TL;DR.** In the context of keeping CQC and Companies House data current and
giving the CRM an event stream to act on, we chose **event sourcing with git as
the event store**: each monthly refresh writes a **git-committed, per-source,
timestamped change-event file**, and the **database is a derived projection** —
replay all files to rebuild it, apply the latest to update it. CQC files come
from a monthly bulk snapshot-diff; Companies House files come from a monthly
filing-history check that re-polls only changed companies. Accepting replay and
per-company poll costs, we get one canonical, reviewable, replayable history that
both refreshes the DB and triggers outreach.

## Context

Two acquisition realities and one product need:

- **CQC** publishes a full monthly bulk snapshot. Today `cqc_refresh` rewrites the
  40 MB CSVs in full (verified: unsorted → noisy git diff), forces a full
  re-import, never deletes (removed providers orphan), emits no change signal.
- **Companies House** officers/PSC have **no bulk snapshot** — per-company API
  only. Its real change feed is the Streaming API (deferred to the deploy phase).
- **The product** ([`product-vision.md`](../product-vision.md)) is event-driven:
  outreach should fire when something changes at a target provider.

A prior draft split this into git delta files (CQC) *and* a DB `ChangeEvent`
table (CH) — two representations for one idea, and unclear which was canonical.
This ADR resolves that: **the files are canonical; the DB is rebuildable from
them.**

## Decision

1. **Canonical store = git-committed, per-source, timestamped change-event files**
   under `data/changes/`:
   - `cqc-YYYY-MM-DD.json`, `companies-house-YYYY-MM-DD.json`.
   - Each is an ordered list of change events: `change_type`
     (`provider_added|removed|updated`, `location_added|removed|updated`,
     `role_appointed|ended|changed`), the entity key(s) (Provider/Location ID;
     person identity + role for CH), `effective_date` (source date of the change),
     and `details` (the new row, or field-level deltas). `added`/`changed` carry
     full payloads so apply needs nothing else.

2. **The database is a projection** of seed + event files, never a source of
   truth. Two operations:
   - **Rebuild:** import the seed, then replay every event file in
     (source, date) order → providers / facilities / person / role, plus a
     `change_events` table (a queryable materialization of the files, for
     outreach).
   - **Apply (incremental):** apply not-yet-applied files (tracked in an
     `applied_event_file` ledger) — idempotent and resumable.
   The CQC **seed** stays the committed `output.csv`/`Locations.csv`
   ([ADR 0007](0007-csvs-checked-into-repo.md)); the cron no longer rewrites
   them. The CH **seed** is simply the first `companies-house-*` file (all roles
   as `*_added` — larger one-time, small monthly thereafter).

3. **CQC acquisition — monthly bulk snapshot-diff.** Reconstruct the baseline in
   memory (seed + replay prior CQC files), diff the freshly mapped bulk by
   `Provider ID` / `Location ID`, write the new `cqc-DATE.json`. Order-independent
   (keyed by ID), small, reviewable.

4. **CH acquisition — monthly filing-history check, re-poll only the changed.**
   For each provider with a CH number, one cheap `filing-history` call; if its
   latest officer/PSC-category filing is newer than the stored watermark
   (`Provider.ch_filing_watermark`), re-fetch officers+PSC, diff against current
   roles (reconstructed from prior CH files), and emit `role_*` events into
   `companies-house-DATE.json`. Far fewer heavy calls than re-polling everyone.
   `Provider.ch_enriched_at` tracks coverage; never-checked / previously-errored
   providers (the ~554) are always polled.

5. **Removals = soft-delete.** `Provider.active` (default true) + `removed_at`; a
   `provider_removed` event sets `active=false` (retain `Person`/`Role`; no data
   loss). Re-appearance reactivates. Queries/UI filter on `active`.

6. **CI simplifies.** The refresh workflow commits the small new event file (+
   watermark/state) instead of force-pushing regenerated 40 MB CSVs; the PR body
   is the human change summary.

7. **Deferred consumers.** Outreach `Task` creation from `change_events` (Phase
   4) and the CH **Streaming API** as a third event-file producer (deploy phase).
   The log is built ready for them.

8. **Compaction.** Event files accumulate; periodically re-base (snapshot current
   state as a fresh seed, archive old files) — manual, outside the cron.

## Alternatives considered

- **DB `ChangeEvent` table as the source of truth** (prior draft) — rejected:
  makes the DB authoritative and rebuild/DR harder; files-canonical gives a
  reviewable, replayable history and a trivially rebuildable DB.
- **Monthly full CH re-poll** (no filing-history check) — rejected: ~7–11h /
  ~51k calls/month even when little changed; the filing-history check re-polls
  only the changed subset.
- **Keep overwriting the CQC CSV** — rejected: the 40 MB rewrite/force-push and
  noisy diff are what motivated this.
- **Profile-etag watermark instead of filing-history** — viable but over-triggers
  (etag changes on any filing, e.g. accounts); filing-history scoped to
  officer/PSC categories is precise. Etag is the fallback if filing-history
  parsing proves fiddly.
- **CH Streaming API now** — deferred (no host pre-deploy); same file model later.

## Consequences

- **Positive:** one canonical, reviewable, replayable history; DB rebuildable or
  incrementally updatable from the same files; tiny order-independent commits;
  removals handled; CH heavy calls cut to the changed subset; the outreach
  trigger substrate exists from day one.
- **Cost — replay + ledgers:** acquisition reconstructs state from seed + files
  each run; DB needs `applied_event_file` + the `change_events` projection (grows
  — archive/partition later).
- **Cost — CH cheap-call floor:** ~25k filing-history calls/month (rate-limited
  to a few hours) even when nothing changed; the alternative is the Streaming API
  (deploy phase).
- **Schema (additive,** [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md)**):**
  `change_events`, `applied_event_file`; `Provider.active`, `removed_at`,
  `ch_enriched_at`, `ch_filing_watermark`.
- **Supersedes** the regenerate/overwrite refresh; **reframes** ingest as
  seed + event-file replay.

## Walk-back options

- **If replay/files sprawl** — re-base more often (§8), or add an `id→hash`
  baseline manifest for CQC; file format unchanged.
- **If a projection drifts from the files** — a full rebuild (seed + replay) is
  the reconciliation oracle.
- **If the CH cheap-call floor bites** — adopt the Streaming API once a host
  exists; it becomes a third event-file producer with no model change.

## Links

- [ADR 0007](0007-csvs-checked-into-repo.md) — CQC seed CSVs (amended: seed, not rewritten).
- [ADR 0005](0005-two-stage-csv-ingest.md) — ingest (reframed: seed + event-file replay).
- [ADR 0013](0013-companies-house-source.md) / [ADR 0014](0014-person-role-correlation-model.md) — CH enrichment that emits role events.
- [`product-vision.md`](../product-vision.md) — event-driven outreach; Phase-4 `Task` consumes `change_events`.
- [`docs/plans/data-freshness.md`](../plans/data-freshness.md) — implementation plan.
