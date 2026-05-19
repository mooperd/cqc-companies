# ADR 0001 — Two-entity domain model (Provider → Facility)

**Status:** Accepted (2025-08-28). Amended 2026-05-19 — see Amendment below; the `Contact` model is a CRM placeholder, not a legacy artefact.

## Amendment (2026-05-19)

Original §Decision-3 and §Consequences described the `Contact` class in `model.py:63-83` as a "legacy flat model retained for backward compatibility during migration" — and §Walk-back proposed deleting it. That framing was wrong.

`Contact` is a deliberate **placeholder for future CRM-style interaction tracking** — i.e. recording conversations, outreach, and notes against providers / facilities. It is intentionally retained.

Two notes on the present state:

1. The current field shape of `Contact` (mirror of the old flat Provider+Location row) does **not** yet reflect this intent. A proper CRM `Contact` would carry interaction-specific fields (`contacted_at`, `channel`, `notes`, `outcome`, FK to `Provider` and/or `Facility`, FK to a user). The fields will be reshaped when the CRM feature is scoped.
2. The misleading comment in `model.py:63` (`# Keep Contact for backward compatibility during migration`) should be updated when the feature lands or sooner.

The original §Walk-back item "if `Contact` is confirmed unreferenced, delete the class" no longer applies — `Contact` is reserved, not unused-pending-cleanup.

The original Context, Decision, and Walk-back sections below are retained for historical record. Where they conflict with this amendment, this amendment wins.

**TL;DR.** In the context of representing CQC's regulated care market in the database, facing a published dataset where each provider organisation owns one or more registered locations, we chose a normalised two-entity model (`Provider` 1—N `Facility`) over a flat single-table model, to achieve cheap "provider-level" aggregates (count of facilities, total beds, NHS-vs-private filtering) at the cost of a join on every facility query.

## Context

The CQC publishes two related public datasets:

- A "Provider" CSV (`output.csv`) where each row is one *registered location* and the provider organisation is repeated across all its locations.
- A "Locations" CSV (`Locations.csv`) carrying detail (ratings, beds, registered manager, region) keyed by Location ID.

The naive thing — and what the repo originally did — is one wide `Contact` table per CSV row. That model can't answer "show me providers with more than 5 facilities, excluding NHS bodies" cheaply: every aggregate has to group by provider *name* (which is not unique across CQC's own data in subtle cases) and there's no stable join key for the locations dataset.

CQC's own IDs (`CQC Provider ID`, `CQC Location ID`) are the only stable keys across both datasets and across CQC's own website (`https://www.cqc.org.uk/location/<id>`). The `providers()` route filters and sorts on per-provider aggregates (`sort_by=facility_count`, `exclude_nhs`); without a provider entity these aggregates require a `GROUP BY name` everywhere they appear.

## Decision

1. The persistent domain has two entities: **`Provider`** (one row per CQC-registered provider organisation) and **`Facility`** (one row per CQC-registered location, owned by a Provider via foreign key).
2. CQC's IDs (`cqc_provider_id`, `cqc_location_id`) are stored and indexed on both entities. They are the join keys against the second CSV (see [ADR 0005](0005-two-stage-csv-ingest.md)) and against `cqc.org.uk` URLs.
3. The legacy flat `Contact` model is retained in `model.py` ("for backward compatibility during migration") and not used by any live code path. It will be removed once nothing references it.
4. The Provider entity carries the *org-level* contact fields (website, email, phone, address). Facility carries the *location-level* fields, plus the rating and beds enrichment fields from the second CSV.

## Alternatives considered

- **Keep the flat `Contact` table.** Rejected: per-provider aggregates require `GROUP BY name`, which is unsafe (provider names collide and change). The `providers()` page would have to fall back to text matching on the provider name.
- **EAV / single-table with `kind` column.** Rejected: SQLAlchemy ergonomics fall off a cliff (every query needs a `kind=` filter); no real upside for a two-class domain.
- **Three-or-more entities (Provider / Location / Service / Rating).** Rejected: the CQC export already collapses these; introducing more entities is speculative and adds joins for queries the UI doesn't issue.

## Consequences

- Every facility-level query joins to Provider (most do already, for the provider name). Cost is borne; both FKs are indexed.
- `providers()` route's facility-count sort needs an explicit subquery (see `app.py:160-191`) to avoid double-counting from filter joins. This is in the code rather than the ORM defaulting because SQLAlchemy's `distinct()` doesn't compose with `ORDER BY count(*)` cleanly.
- The legacy `Contact` table is dead code but still loaded by `model.py`. `db.create_all()` will create the table on a fresh database — a minor footgun and a smell flag for cleanup.
- No Alembic migrations yet, so renaming or dropping fields is non-trivial — see [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md).

## Walk-back options

- **If a third top-level CQC entity (e.g. inspection reports) needs first-class storage** — add it as a sibling of `Facility`, not by widening `Facility`.
- **If the `Contact` table is confirmed unreferenced** — delete the class and add an Alembic migration to drop the table. Until then it's harmless dead weight.

## Links

- `model.py:5-83` — Provider / Facility / Contact definitions.
- [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) — why removing `Contact` is harder than it looks.
- [ADR 0005](0005-two-stage-csv-ingest.md) — how the two CSVs are joined.
