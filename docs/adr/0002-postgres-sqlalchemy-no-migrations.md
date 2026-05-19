# ADR 0002 — PostgreSQL + Flask-SQLAlchemy; `db.create_all()` for schema

**Status:** Accepted (2025-08-28).

**TL;DR.** In the context of a small Flask app whose schema is driven by two well-defined CSV exports, facing a startup-stage codebase where Alembic overhead would be premature, we chose PostgreSQL via Flask-SQLAlchemy with `db.create_all()` as the only schema-management mechanism, accepting that any non-additive schema change requires either a hand-written SQL migration or a database drop-and-reimport.

## Context

The app needs a real relational store because the queries are all SQL-shaped: joins, group-by, ilike searches across normalised columns. The dataset is bounded (≈50k facilities, ≈30k providers) and grows slowly — the entire dataset can be re-imported from CSV in minutes.

Three things make migrations feel optional in this phase:

1. The CSV importers (`import_records.py`, `enrich_locations.py`) are idempotent against a clean DB — full re-import is a recovery path, not a recovery disaster.
2. The schema in `model.py` is small and stable enough that field additions outnumber renames or drops.
3. The single deployment target (one AKS namespace, one Postgres pod) means there is no fleet to migrate; the canonical schema is "whatever `db.create_all()` writes from the latest `model.py`".

That said, "no migrations" is a phase, not a virtue. The moment a deployed column needs a rename or a type change, this decision walks back.

## Decision

1. The runtime database is PostgreSQL 15 (in-cluster: see [ADR 0009](0009-in-cluster-postgres.md)).
2. The ORM is SQLAlchemy via Flask-SQLAlchemy. The schema is the union of the `db.Model` classes in `model.py`.
3. Schema is provisioned via `db.create_all()` at import-time entry points (`app.py:612-614`, `import_records.py:225`, `enrich_locations.py:281`). No Alembic, no `alembic.ini`, no `migrations/` directory.
4. The connection URL is read from `DATABASE_URL` (default `postgresql://darwinist:darwinist@localhost:5432/darwinist`). The same env var is used by the importer scripts and the web app.

## Alternatives considered

- **Alembic from day one.** Rejected for now: there's no schema-evolution traffic yet, and the field-addition pattern (most columns added are nullable strings from new CSV columns) wouldn't have been gated by it anyway.
- **SQLite for local dev, Postgres in prod.** Rejected: `ilike`, the `case(...)` aggregate, and the indexes behave differently enough between the two to make local testing misleading.
- **Stored procedures / views for the aggregates.** Rejected: SQLAlchemy expressions are perfectly adequate for the size of the dataset and keep the logic in Python where it's reviewable.

## Consequences

- Adding a nullable column is free: edit `model.py`, restart the app, `db.create_all()` is a no-op for existing columns and adds the new one on next run. **Actually, this is wrong** — `db.create_all()` *does not* add columns to existing tables; it only `CREATE TABLE IF NOT EXISTS`. New columns appear only on a fresh database. This is a real footgun and the strongest argument for adding Alembic before the next column lands in prod.
- Any rename, drop, or type-change requires manual SQL or a drop-and-reimport. The reimport is currently feasible (`import_records.py` + `enrich_locations.py` ≈ minutes), but that ceiling shrinks as the dataset grows.
- The `Contact` table from [ADR 0001](0001-provider-facility-domain-model.md) cannot be dropped cleanly without out-of-band SQL.
- The dev/prod credentials in `.env` and `k8s/postgres.yaml` are identical (`darwinist:darwinist`). Convenient for now; should be split before the first non-toy user.

## Walk-back options

- **If a column needs to be renamed/dropped on a non-empty production database** — introduce Alembic, generate an initial migration from current state, then write the rename migration. Two-step, but unavoidable.
- **If the dataset grows past the "drop-and-reimport-in-an-hour" threshold** — same as above; Alembic before that point.
- **If a second deployment target appears (a staging cluster, another tenant)** — Alembic first; the cost of forgetting to keep two schemas in sync is much higher than the cost of running migrations.

## Links

- `model.py` — schema.
- `app.py:612-614` — `create_tables()` helper.
- `import_records.py:225`, `enrich_locations.py:281` — importer-side `create_all`.
- [ADR 0001](0001-provider-facility-domain-model.md) — the `Contact` table whose removal this decision blocks.
- [ADR 0009](0009-in-cluster-postgres.md) — where the database actually runs.
