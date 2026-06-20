# Plan — Companies House enrichment: seed director `Person` rows

**Status:** Proposed.

<!--
Status lifecycle:
  Proposed → Active → Closed (YYYY-MM-DD)
Update in place; don't stack past states.
-->

## Goal

Implement Phase 2 of [`docs/product-vision.md`](../product-vision.md): for each
`Provider` with a Companies House number, pull its directors from the Companies
House API and create `Person` rows seeded from that data, applying the
source-hierarchy rule from [ADR 0013](../adr/0013-companies-house-source.md).
Manual entry continues to work alongside; Companies House is the bulk seed, not
the only source.

## Prerequisites

- **ADR 0013 Accepted** (currently Proposed — confirm the Companies-House-first
  choice and the conflict-resolution rule before building).
- **`Person` entity exists**: ✓ landed via
  [ADR 0012](../adr/0012-crm-person-interaction-user-model.md) /
  [`crm-phase1.md`](crm-phase1.md) WS1 (2026-06-20). `Person` carries the
  `source`/`confidence`/appointment-date fields this plan's mapper targets, so
  WS1–WS4 are no longer blocked.
- A Companies House API key (free; register at
  <https://developer.company-information.service.gov.uk/>). Stored as a secret,
  never committed.

## Where things stand (2026-06-20)

Groundwork done; the `Person` blocker is now cleared:

- **WS0 — persist the CH number: Shipped.** `Provider.companies_house_number`
  (nullable, indexed) is populated in `enrich_locations.py` from the
  `Locations.csv` column. Verified against a throwaway local Postgres: 25,514 of
  36,982 providers (~69%) carry a CH number after a full round-trip.
- **WS1–WS4: Open** (unblocked 2026-06-20 — `Person` now exists via ADR 0012).
  Not started; next step is the Companies House API client (WS1).

## Workstreams

### WS0 — Persist the Companies House number on `Provider`

**Status:** Shipped (2026-06-20).

The CQC HSCA export already gives us each provider's CH number; it was being
discarded at import. Added `Provider.companies_house_number` (nullable, indexed
`String(20)`) and populated it in the stage-2 importer. Additive-column path per
[ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md) — no Alembic
trigger. This is the join key the rest of the plan needs.

**Exit:** ✓ A round-trip import populates `companies_house_number` for providers
that have one (verified: 25,514 / 36,982).

### WS1 — Companies House API client

**Status:** Open (unblocked 2026-06-20 — `Person` exists).

A thin client over the Companies House public API. Given a company number,
fetch the officers list (`GET /company/{number}/officers`), returning active
directors with name, role, and appointment/resignation dates. Key-gated (free),
no meaningful rate limit, but be polite (backoff on 429). Stdlib `urllib` or
`requests` — match whatever the codebase settles on; keep it dependency-light
like `cqc_refresh.py`.

**Deliverables:** a `companies_house` module with
`fetch_officers(company_number) -> list[Officer]`; the API key read from the
environment, asserted present at startup.

**Exit:** fetches officers for a handful of known company numbers from the live
API; resignation dates correctly distinguish active from past directors.

### WS2 — Map officers to `Person` rows

**Status:** Open (unblocked 2026-06-20 — `Person` exists).

Transform Companies House officers into `Person` rows against the provider,
setting `source = companies_house`, a confidence value, and role + appointment
dates per the Phase-1 `Person` schema. Filter to director-class roles (skip
secretaries / nominee entries unless Phase 1 decides otherwise).

**Deliverables:** a mapper from `Officer` → `Person`, idempotent on
`(provider, person identity)`.

**Exit:** running against a sample of providers creates the expected `Person`
rows with correct source/role/date fields.

### WS3 — Source-hierarchy merge

**Status:** Open (unblocked 2026-06-20 — `Person` exists).

Implement the conflict-resolution rule from [ADR 0013](../adr/0013-companies-house-source.md)
§3: manual overrides all; Companies House authoritative for director identity +
appointment status; LinkedIn only fills the non-director gap. On re-run, a CH
"resignation" marks the role ended rather than deleting the `Person`.

**Deliverables:** merge logic invoked on each enrichment run; never silently
overwrites a `manual`-sourced fact.

**Exit:** a re-run after a simulated director resignation marks the role ended
and does not resurrect it from a stale lower-confidence source.

### WS4 — Enrichment entry point + cadence

**Status:** Open (unblocked 2026-06-20 — `Person` exists).

A command (mirroring the `cqc_refresh` CLI shape) that walks providers with a CH
number and runs WS1→WS3. Decide refresh cadence (likely: piggy-back the monthly
CQC refresh, or a separate scheduled job). Manual entry remains available in the
app UI throughout.

**Deliverables:** `python -m companies_house enrich` (or equivalent);
optionally wired into a scheduled workflow.

**Exit:** an end-to-end run populates director `Person` rows for CH-registered
providers against a local Postgres; coverage roughly matches the ~69% that carry
a CH number.

## Phase exit criteria

When all of these are true, this plan closes:

- [x] WS0 — CH number persisted on `Provider`.
- [ ] WS1–WS4 shipped (unblocked — `Person` exists; not yet started).
- [ ] Director `Person` rows seeded from Companies House for CH-registered
      providers, round-tripping through a local Postgres.
- [ ] The source-hierarchy rule (ADR 0013 §3) is exercised and holds on re-run.
- [ ] ADR 0013 moved from Proposed to Accepted.

## References

- [ADR 0013 — Companies House as first identification source](../adr/0013-companies-house-source.md)
  — the decision this plan implements.
- [`docs/product-vision.md`](../product-vision.md) — Phase 2 in the roadmap.
- [ADR 0005 — Two-stage CSV ingest](../adr/0005-two-stage-csv-ingest.md) — the
  importer WS0 extended.
- [ADR 0012 — CRM data model](../adr/0012-crm-person-interaction-user-model.md)
  + [`crm-phase1.md`](crm-phase1.md) — landed `Person`, the seed target for this plan.
