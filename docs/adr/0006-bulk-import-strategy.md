# ADR 0006 — In-memory caches + 5,000-row bulk inserts for import

**Status:** Accepted (2025-08-28).

**TL;DR.** In the context of importing ≈50k facility rows from `output.csv` (and a similar order from `Locations.csv`), facing single-row INSERTs taking tens of minutes against a local Postgres, we chose a two-pass approach inside each importer — first an in-memory dict cache of existing rows keyed by the relevant CQC ID, then `session.add_all(...)` in batches of 5,000 — to achieve sub-minute imports at the cost of holding the full provider list in memory and occasional "lost work" if a batch fails mid-run.

## Context

Both importer scripts (`import_records.py`, `enrich_locations.py`) need to do "find or create" by a string ID on every row. The naive shape — `session.query(Provider).filter_by(...).first()` per row, then `session.commit()` per row — is N round-trips to Postgres plus N transactions. On a local Postgres, this was taking long enough to be annoying interactively (minutes) and a non-starter on cold-start in a deployed container.

Dataset size sets the upper bound on memory: roughly 30k providers × small dict entry ≈ tens of MB. Trivially fits in the container.

## Decision

1. **Pre-load the lookup set once.** Both importers issue a single `query(...).all()` and build a `{key → object}` dict before processing rows (`import_records.py:28-36`, `enrich_locations.py:28-43`).
2. **Two-pass ingest inside `import_records.py`.** Pass 1a walks the CSV to collect the set of *new* providers; pass 1b creates them in bulk via `session.add_all`; pass 1c walks the CSV again to create facilities (`import_records.py:107-199`). This avoids inter-row dependencies during facility creation.
3. **Batch size = 5,000 facilities per commit.** `commit_batch(...)` is called every 5,000 rows in `import_records.py:160-178`. Progress is logged with rate and ETA.
4. **Lookup uses CQC IDs where present.** Providers are keyed on the provider *name* in pass 1 (the `output.csv` join key) and on `cqc_provider_id` in pass 2 (the `Locations.csv` join key); facilities are keyed on `cqc_location_id` in pass 2.
5. **Failures abort the importer.** A commit failure logs the row number and `sys.exit(1)` (`import_records.py:103-105`); the partially-committed batches are not rolled back. The recovery path is "fix the input, drop the DB, re-run".

## Alternatives considered

- **Per-row INSERT + commit.** Rejected: 10–100× slower; not viable interactively.
- **COPY FROM (Postgres bulk-load).** Rejected for now: needs CSV-to-COPY transformation and bypasses the ORM (would have to recreate the "find or create" semantics in SQL). Reconsider if dataset crosses 1M rows.
- **`bulk_insert_mappings` / `bulk_save_objects`.** Considered; `add_all` is plenty fast at this size and preserves SQLAlchemy session semantics (cascades, defaults, the `facilities` relationship). Worth revisiting if a single batch >50k rows is ever needed.
- **Threaded / async import.** Rejected: single-writer Postgres makes parallelism pointless here.

## Consequences

- A full import runs in roughly a minute on a developer laptop and fits comfortably inside the AKS pod's resource envelope.
- The whole provider list lives in RAM during a run (≤100 MB). Comfortable headroom.
- Mid-run failures leave the database in a partially-imported state. Recovery is documented to be a full re-import, which is feasible only because the importers are idempotent against a clean DB. **The first time this becomes false** (e.g. a non-idempotent enrichment step), this decision must walk back.
- Progress logs land on stdout — useful for `kubectl logs`, not durable.
- The 5,000-row batch size is a magic number. Tuned by feel; no benchmarking captured. If commit latency or memory ever matters, this needs a measurement, not a knob-twist.

## Walk-back options

- **If dataset > ~500k rows** — switch to `COPY FROM` with a staging table + upsert. Drop the in-memory dict (it's gone past comfort).
- **If imports start being run against a populated production DB** (rather than drop-and-reimport) — add per-batch savepoints and a `--resume-from-row N` flag so a failure doesn't require restarting from row 1.
- **If RAM pressure shows up under `kubectl top pod`** — process the CSV in chunks rather than holding the cache for the full run.

## Links

- `import_records.py:28-36` — `load_all_providers`.
- `import_records.py:107-199` — `process_csv_file` two-pass.
- `import_records.py:160-178` — batch commit.
- `enrich_locations.py:28-43`, `:45-60` — facility / provider caches for pass 2.
- [ADR 0005](0005-two-stage-csv-ingest.md) — why there are two importers.
