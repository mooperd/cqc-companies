# Architecture Decision Records

Numbered records of non-obvious design choices, kept forever, amended in place when scope expands, never deleted.

For the full convention, see [Claude Project Discipline — ADRs](https://robtaylor.github.io/claude-project-discipline/adr.html).

These first ten ADRs were reverse-engineered from the codebase as of `2fe0977` (2025-09-01). They describe choices that the code makes self-evident but whose *why* would otherwise be lost. New ADRs from here are written when the decision is made, not after.

## Index

| ADR  | Title | Status |
|------|-------|--------|
| 0001 | [Two-entity domain model (Provider → Facility)](0001-provider-facility-domain-model.md) | Accepted 2025-08-28 |
| 0002 | [PostgreSQL + Flask-SQLAlchemy; `db.create_all()` for schema](0002-postgres-sqlalchemy-no-migrations.md) | Accepted 2025-08-28 |
| 0003 | [Server-rendered Flask + Jinja, no front-end framework](0003-server-rendered-flask-jinja.md) | Accepted 2025-08-28 |
| 0004 | [Matplotlib server-side charts, base64-embedded in HTML](0004-matplotlib-base64-charts.md) | Accepted 2025-09-01 |
| 0005 | [Two-stage CSV ingest keyed on CQC IDs](0005-two-stage-csv-ingest.md) | Accepted 2025-08-28 |
| 0006 | [In-memory caches + 5,000-row bulk inserts for import](0006-bulk-import-strategy.md) | Accepted 2025-08-28 |
| 0007 | [CQC source CSVs checked into the repository](0007-csvs-checked-into-repo.md) | Accepted 2025-08-28 |
| 0008 | [AKS deploy via envsubst-templated manifests](0008-aks-envsubst-deploy.md) | Accepted 2025-08-29 |
| 0009 | [In-cluster PostgreSQL on a `managed-csi` PVC](0009-in-cluster-postgres.md) | Accepted 2025-08-29 |
| 0010 | [Hardcoded specialism / service-type filter taxonomy](0010-hardcoded-filter-taxonomy.md) | Accepted 2025-08-29 |

## Format

```markdown
# ADR NNNN — Short title

**Status:** Accepted (YYYY-MM-DD).

## Context
What was the situation? What constraints forced a decision?

## Decision
What we chose, with enough specificity to act on.

## Consequences
What this buys us, what it costs us, what would trigger a walk-back.

## Walk-back options
(Optional, recommended.) Conditions to revisit.

## Links
Cross-references.
```

When scope expands, amend in place rather than rewriting:

```markdown
**Status:** Accepted (2025-08-28). Scope expanded 2025-12-01 — see Decision §3.
```

When superseded, update Status on the old ADR and add a new ADR that references it. Never delete the old one.

## Template

Copy `0000-template.md` to `NNNN-<short-name>.md` for the next available number.
