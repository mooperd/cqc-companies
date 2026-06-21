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

## Where things stand (2026-06-21) — reshaped by ADR 0014

**Reopened.** WS1/WS2/WS4 shipped against the flat `Person` (ADR 0012) and a
full run was *started then stopped at ~1,250 providers* when two gaps surfaced:
(a) we only ingested officers, not persons with significant control (PSC), and
(b) the flat `Person` can't represent one human with multiple roles or correlate
the same human across CH's two endpoints and across companies.
[ADR 0014](../adr/0014-person-role-correlation-model.md) reshapes `Person` into
**`Person` ↔ `Role`** with global DOB+name correlation, and
[ADR 0013](../adr/0013-companies-house-source.md) (amended) adds the PSC source,
individuals only. The ~10k flat rows from the stopped run will be dropped.

- **WS0 — persist the CH number on Provider: Shipped, unaffected.** 25,514 of
  36,982 providers (~69%) carry a CH number.
- **WS1 — API client: officers shipped + live-verified; PSC to add.**
  `companies_house.py` `fetch_officers` works (Medacs: 37 officers). **Rework:**
  add `fetch_psc` (`/persons-with-significant-control`) + a `PSC` dataclass +
  `psc` CLI subcommand. Both endpoints expose partial DOB for correlation.
- **WS2 — map → Person+Role: reopened (rework).** Was `enrich_directors.py`
  flat-Person upsert. **Rework:** parse officer/PSC names → (surname, forenames);
  find-or-create global `Person` by DOB+surname+first-forename; attach `Role`
  (role_type, namespaced source, dates, control_nature); individuals only. Rename
  `enrich_directors.py` → `enrich_people.py`.
- **WS4 — provider-walk CLI: reopened (rework).** Fetch **both** endpoints per
  provider, correlate + sync `Person`+`Role`. ~2 API calls/provider → full run
  ~7h. `--skip-enriched` + batched commits carry over.
- **WS3: Open.** Source precedence now per `Role` (ADR 0013 §3 / ADR 0014 §6);
  still lower value until manual/LinkedIn sources exist.

New prerequisite: the `Person`+`Role` schema (ADR 0014) — tracked in
[`crm-phase1.md`](crm-phase1.md) WS1.

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

**Status:** Officers shipped + live-verified (2026-06-21); **reopened** to add the
PSC endpoint (ADR 0013 amendment / ADR 0014).

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

### WS2 — Map officers + PSC to `Person` + `Role`

**Status:** Flat-Person mapper shipped (2026-06-21); **reopened** for the
`Person`↔`Role` reshape + correlation + PSC (ADR 0014). `enrich_directors.py` →
`enrich_people.py`.

`enrich_directors.py` (parallel to `enrich_locations.py`). `is_director_role`
keeps director-class roles (`director`, `corporate-director`, nominee-director)
and drops secretaries/nominee-secretaries. `dedupe_by_identity` collapses repeat
appointments of one person (preferring an active appointment, then the latest
`appointed_on`). `sync_provider_directors(session, provider_id, officers)`
upserts director `Person` rows with `source='companies_house'`,
`confidence='high'` (ADR 0013 §3), and the role + appointment/resignation dates.

Idempotent on (provider, case-folded name): a re-run updates the existing
CH-sourced row rather than duplicating. It only ever touches
`source='companies_house'` rows — manual/LinkedIn rows are left alone (the full
cross-source precedence is WS3).

**Deliverables:** ✓ `enrich_directors.sync_provider_directors(...)` +
`test_enrich_directors.py` (role filter, dedupe, create/skip, idempotency,
manual-rows-untouched), against in-memory SQLite. Wired into the CI smoke check.

**Exit:** ✓ Offline tests pass. ✓ End-to-end with live WS1 data (Medacs
02518546): 37 officers → 30 director `Person` rows (3 active), 5 secretaries
skipped, 2 duplicate appointments deduped; re-run idempotent (0 created / 30
updated).

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

**Status:** Flat-Person CLI shipped (2026-06-21); **reopened** to fetch both
endpoints and sync `Person`+`Role` (ADR 0014). Scheduled cadence still deferred.

`python enrich_directors.py [--limit N] [--sleep S] [--dry-run] [--skip-enriched]`
(the CLI lives
in `enrich_directors.py` alongside the WS2 mapper, mirroring
`enrich_locations.py`). `providers_with_ch_number` selects providers carrying a
CH number; `enrich_all` walks them, calling `fetch_officers` (WS1) +
`sync_provider_directors` (WS2) per provider, committing every 50, pacing
requests (`--sleep`, default 0.5s) under the ~600-req/5-min limit with the
client's 429 backoff as the safety net. A 404 skips that provider (counted
`not_found`); a bad key aborts. `--dry-run` rolls back. `--skip-enriched`
excludes providers that already have CH-sourced people, so an interrupted run
resumes rather than restarting (batched commits every 50 make completed work
durable). Measured throughput: ~0.62s/provider, so a full ~25.5k-provider walk
is ~4.4h with ~220k director rows.

**Deliverables:** ✓ `enrich_directors.py` CLI (`enrich_all`,
`providers_with_ch_number`, `build_parser`/`main`); `providers_with_ch_number`
filter test; wired into the CI smoke check.
**Not done:** scheduled workflow / cadence — deferred (a full ~25k-provider walk
is ~3.5h at the rate limit; decide piggy-back-monthly vs separate job when we
run it for real).

**Exit:** ✓ End-to-end against a throwaway local Postgres seeded with real
providers (Medacs / Alphonsus / U&I + one without a CH number): 3 enriched (the
no-CH one skipped), 37 director rows, all `source=companies_house`; re-run
idempotent (0 created / 37 updated); `--dry-run` persists nothing.

## Phase exit criteria

When all of these are true, this plan closes:

- [x] WS0 — CH number persisted on `Provider`.
- [~] WS1 — officers client shipped + live-verified; **PSC endpoint still to add**
      (ADR 0014 rework).
- [~] WS2 — flat-Person mapper shipped; **reopened** for `Person`+`Role` +
      correlation + PSC (ADR 0014).
- [~] WS4 — flat-Person CLI shipped; **reopened** to fetch both endpoints and
      sync `Person`+`Role`. Scheduled cadence deferred.
- [ ] WS3 — cross-source precedence (per `Role`; deferred until manual/LinkedIn
      sources exist).
- [ ] `Person`+`Role` rows seeded from officers **and** PSC for CH-registered
      providers, correlated by DOB+name, round-tripping through a local Postgres.
- [ ] ADR 0014 (Person/Role) and ADR 0013 moved from Proposed to Accepted.

## References

- [ADR 0013 — Companies House as first identification source](../adr/0013-companies-house-source.md)
  — the decision this plan implements.
- [`docs/product-vision.md`](../product-vision.md) — Phase 2 in the roadmap.
- [ADR 0005 — Two-stage CSV ingest](../adr/0005-two-stage-csv-ingest.md) — the
  importer WS0 extended.
- [ADR 0012 — CRM data model](../adr/0012-crm-person-interaction-user-model.md)
  + [`crm-phase1.md`](crm-phase1.md) — landed `Person`, the seed target for this plan.
