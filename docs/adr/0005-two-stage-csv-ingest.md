# ADR 0005 — Two-stage CSV ingest keyed on CQC IDs

**Status:** Accepted (2025-08-28).

**TL;DR.** In the context of CQC publishing two complementary CSVs — the "providers" extract (`output.csv`) and the "locations" extract (`Locations.csv`) — each with overlapping but non-identical columns, we chose to ingest them in two passes (`import_records.py` then `enrich_locations.py`), joining on CQC's own Provider ID and Location ID, to achieve clean separation between "what we get on import" and "what gets enriched onto existing rows" at the cost of carrying two import scripts that must agree on the schema.

## Context

CQC publishes two CSV exports that together describe the regulated care market:

- **`output.csv`** — one row per facility, with the *provider's* contact details, the location's address, and the basic categorisation (specialisms, service types, region, local authority).
- **`Locations.csv`** — one row per location, with the *location's* enrichment data: registered manager, UPRN, telephone, web address, bed count, latest overall rating + sub-ratings, care-home size band, primary inspection category, location start/end dates, etc.

The two CSVs share `CQC Location ID` as the only stable join key (provider names and location names are not unique). Some locations appear in `Locations.csv` that aren't in `output.csv` (and vice versa), so the enrichment pass must be able to *create* missing rows, not just update existing ones.

Doing both passes inside a single importer was considered but rejected: the two CSVs ship on different cadences (providers monthly, locations more frequently), and conflating them makes it harder to re-run only the enrichment when a new locations export arrives.

## Decision

1. **Pass 1 — `import_records.py output.csv`**: ingest providers and facilities, keyed on `CQC Provider ID` (provider) and `CQC Location ID` (facility). Creates providers and facilities from scratch on a clean DB.
2. **Pass 2 — `enrich_locations.py Locations.csv`**: load existing facilities into an in-memory `{location_id → facility}` map; for each row, update the matching facility's enrichment columns. Missing providers and facilities are created from location data (`enrich_locations.py:62-104`).
3. The schema in `model.py` is shaped so that *provider-set* fields and *location-set* fields are clearly partitioned — comments in `model.py:40-59` mark "Location enrichment fields".
4. Both scripts call `db.metadata.create_all(engine)` (`import_records.py:225`, `enrich_locations.py:281`) — they are responsible for creating their own schema on first run, see [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md).

## Alternatives considered

- **Single combined importer.** Rejected: couples the two CSVs' release cadences and forces a full re-import for an enrichment-only refresh.
- **Skip Pass 2 entirely, accept missing enrichment fields.** Rejected: the rating, beds, and registered-manager fields are precisely what makes the directory interesting; without them, the `/statistics` page is empty.
- **External ETL tool (dbt, Airflow, etc.).** Rejected: heavyweight relative to the two-script approach; introduces its own deployment surface.
- **Join the two CSVs in Pandas before insert.** Rejected: the in-memory join is fine but the row-level rules (create-if-missing, partial update, idempotency) are clearer in straight Python than in a Pandas merge.

## Consequences

- The two CSVs can be refreshed independently. Most updates will be Pass-2-only.
- The schema in `model.py` is a superset of both CSVs' columns. Adding a column requires updating only one place but, given [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md), the new column only lands on a fresh DB.
- Pass 2 creates providers and facilities from location data when they are missing, with empty contact fields (`enrich_locations.py:62-104`). This means a Pass-2-only run on a fresh database produces a partially-populated dataset — not a recommended use, but not broken either.
- The CSVs are committed alongside the code — see [ADR 0007](0007-csvs-checked-into-repo.md).
- There's a third script-flavoured concern not yet addressed: detecting *deletions*. Neither importer removes records that disappear from a fresh CSV. If a provider deregisters, their rows persist until manual cleanup. Walk-back option below.

## Walk-back options

- **If CQC publishes a "deregistered locations" feed** — add a Pass-3 that soft-deletes (sets a flag) or hard-deletes facilities missing from a fresh extract.
- **If column drift between the CSVs and `model.py` becomes painful** — generate `model.py`'s columns from the CSV headers (with a manual override map), or invert the dependency and write a CSV-to-DB diff tool.
- **If the schemas of the two CSVs diverge significantly across versions** — version the importer scripts (`import_records_v1.py`, `_v2.py`) and pick by header detection.

## Links

- `import_records.py` — Pass 1.
- `enrich_locations.py` — Pass 2.
- `model.py:40-59` — "Location enrichment fields" comment.
- [ADR 0001](0001-provider-facility-domain-model.md) — why CQC IDs are the join keys.
- [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) — why both scripts call `create_all` themselves.
- [ADR 0006](0006-bulk-import-strategy.md) — performance approach inside the scripts.
- [ADR 0007](0007-csvs-checked-into-repo.md) — where the input CSVs live.
