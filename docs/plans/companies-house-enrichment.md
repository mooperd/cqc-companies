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
- **WS1 — API client: Shipped + live-verified** (2026-06-21).
  `companies_house.py` (`fetch_officers`) — stdlib-only, paginating, with the
  active/resigned distinction; offline tests pass. Verified against the **live**
  CH API on three real companies (e.g. 02518546 Medacs: 37 officers, 3 active /
  34 resigned; `--active-only` returns exactly the 3). A `COMPANIES_HOUSE_ENV`
  switch selects live/test key + matching base.
- **WS2–WS4: Open** (unblocked — `Person` exists via ADR 0012). Next step is
  WS2 (map officers → `Person` rows).

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

**Status:** Shipped + live-verified (2026-06-21).

`companies_house.py` — a stdlib-only client (matching `cqc_refresh.py`; no new
`requirements.txt` dep). `fetch_officers(company_number, api_key=None,
active_only=False) -> list[Officer]` follows pagination, parses each officer's
name / role / `appointed_on` / `resigned_on` (real dates), and exposes
`Officer.is_active` (`resigned_on is None`). HTTP Basic auth with the key as
username.

Live and test keys can both live in `.env.local`; a single
`COMPANIES_HOUSE_ENV` (`live` default | `test`) switch selects the key
(`COMPANIES_HOUSE_LIVE_KEY` / `COMPANIES_HOUSE_TEST_KEY`, with a generic
`COMPANIES_HOUSE_API_KEY` fallback) **and** the matching base URL — derived from
the env so they can't mismatch (`resolve_env` / `resolve_api_key`). Backs off on
429 (honours `Retry-After`); 401 → fail-loud, 404 → `CompaniesHouseError`. CLI:
`python -m companies_house officers <number> [--active-only]` (logs the active
env). Wired into the CI import smoke check.

Role filtering (directors vs secretaries) is deliberately left to WS2 — this
returns every officer so the active/resigned distinction stays visible.

**Deliverables:** ✓ `companies_house.fetch_officers(...) -> list[Officer]`;
env-switched key resolution asserted at startup; `test_companies_house.py`
covering parsing, pagination, active/resigned, the env→key/base switch, and the
missing-key error.

**Exit:** ✓ Offline tests pass. ✓ Live-verified (2026-06-21) on three real
companies via `COMPANIES_HOUSE_ENV=live` — e.g. 02518546 (Medacs): 37 officers,
3 active / 34 resigned with correct dates, and `--active-only` returns exactly
the 3 active. The active/resigned distinction holds on real data.

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
- [x] WS1 — Companies House API client shipped and **live-verified**
      (2026-06-21) on real companies.
- [ ] WS2–WS4 shipped (`Person` exists; not yet started).
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
