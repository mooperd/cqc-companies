# ADR 0013 — Companies House as the first director-identification source

**Status:** Accepted (2026-06-23). **Amended 2026-06-21:** scope widened to two
CH people sources (officers **and** persons with significant control),
individuals only — see Amendment below.

## Amendment (2026-06-21) — add the PSC endpoint; individuals only

WS1 originally used only `/officers`. Companies House also exposes
`/persons-with-significant-control` (PSC) — beneficial owners/controllers, often
the real decision-makers and frequently *not* directors (e.g. the individual
owners of U&I Care). Both endpoints now feed identification, as two namespaced
sources: `companies_house:officers` and `companies_house:psc`.

Two refinements, both detailed in [ADR 0014](0014-person-role-correlation-model.md):

- **Individuals only.** Corporate/legal-entity officers and PSCs (holding
  companies, nominee entities) are not contactable people and are excluded from
  `Person`.
- **The §3 source-hierarchy now applies per `Role`**, not per flat Person — each
  source-fact is a `Role` on a correlated `Person`. Manual still overrides CH;
  CH is authoritative for the director/PSC facts it reports.

<!--
Status lifecycle:
  Proposed → Accepted (YYYY-MM-DD) → [Superseded by ADR MMMM (YYYY-MM-DD) | Withdrawn (YYYY-MM-DD)]
This ADR is Proposed, not Accepted: it depends on the `Person` entity that
Phase 1 introduces (not yet built), and the product vision flagged the
Companies-House-first choice as "provisional — to be confirmed in ADR 0013".
Move to Accepted once Phase 1 lands `Person` and the source-hierarchy rule
below is confirmed against real enrichment data.
-->

**TL;DR.** In the context of populating decision-maker `Person` rows for each
CQC provider, facing a choice of where to source identities first, we chose
**Companies House** as the primary source — it's free, already half-ingested
(every provider's CH number rides in the HSCA export), and returns legally-named
directors with appointment dates — accepting that it only covers *registered
directors* (≈69% of providers) and must be supplemented later by LinkedIn for
non-director influencers and by manual entry.

## Context

[`docs/product-vision.md`](../product-vision.md) Phase 2 ("Companies House
enrichment") needs to auto-populate `Person` rows for the directors of each
`Provider`. Three candidate first-sources existed: Companies House, LinkedIn
(via Phantombuster), and manual entry.

The deciding constraint is that **we already ingest the Companies House number
for free**. The CQC HSCA bulk export carries a `Provider Companies House Number`
column; it flows through `cqc_refresh.py` into `Locations.csv` and is populated
for roughly 69% of providers (25,514 of 36,982 in the June 2026 data —
sole traders, NHS bodies, and partnerships have none). Until now that column was
discarded at import time. The Companies House public API is free, key-gated but
without a meaningful rate limit, and returns directors with names, roles, and
appointment/resignation dates — exactly the fields a `Person` row wants.

LinkedIn-via-Phantombuster (Phase 3, ADR 0014) is paid, rate-limited, and
carries GDPR/account-hygiene risk; it is the right tool for *non-director*
influencers but the wrong one to lead with. Manual entry always works but
doesn't scale to ~37k providers.

## Decision

1. **Companies House is the primary (first) identification source.** Director
   `Person` rows are seeded from the Companies House API, keyed on each
   provider's stored `companies_house_number`.

2. **Persist the CH number now, ahead of the full enrichment.** *(Done — shipped
   alongside this ADR.)* `Provider.companies_house_number` is a nullable,
   indexed `String(20)` populated in `enrich_locations.py` from the
   `Locations.csv` column. This is the additive-column path under
   [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) (nullable add, no
   Alembic trigger fired). It stops us throwing away data we already have so the
   enrichment phase starts with the join key in place.

3. **Source-hierarchy / conflict-resolution rule** (resolves the open question
   at product-vision line 104). Each `Person` carries a `source`
   (`companies_house | phantombuster:<phantom> | manual`) and a `confidence`.
   When sources disagree:
   - **Manual overrides everything.** A human-entered fact wins and is never
     silently overwritten by an automated source.
   - **Companies House is authoritative for director identity and
     appointment/resignation status.** If CH says a person is no longer a
     director, the role is marked ended on the CH-sourced `Person`; a LinkedIn
     source still claiming the role does *not* resurrect it — it becomes a
     separate, lower-confidence `Person`/role observation flagged for review.
   - **LinkedIn (Phantombuster) is authoritative only for people Companies House
     cannot see** (non-director influencers). It never overrides a CH director
     fact.

4. **Scope boundary.** This ADR covers *where identities come from and which
   source wins*. The Companies House API client implementation, caching,
   refresh cadence, and the `Person` schema field shapes are owned by the Phase
   1 `Person` ADR (data model) and the Phase 2 implementation plan
   ([`docs/plans/companies-house-enrichment.md`](../plans/companies-house-enrichment.md)).

## Alternatives considered

- **LinkedIn (Phantombuster) first** — rejected: paid, rate-limited, GDPR
  controller exposure from the first scrape, and account-restriction risk. It
  also can't be keyed off data we already hold. It is the *second* source, for
  the gap CH can't fill.
- **Manual entry first** — rejected: doesn't scale to ~37k providers; correct
  only as the always-available override, not the bulk seed.
- **Do nothing / defer the whole question** — rejected: cheap value is being
  left on the table. We already ingest the CH number and were discarding it;
  persisting it now (Decision §2) is near-zero-cost and unblocks Phase 2.

## Consequences

- **Positive:** the bulk of director identities come from a free, structured,
  legally-authoritative source. The join key (`companies_house_number`) is
  captured immediately, so Phase 2 is a smaller jump.
- **Positive:** a clear precedence rule means later sources (LinkedIn, manual)
  compose without ambiguity about what wins.
- **Cost / coverage gap:** Companies House covers only registered companies —
  ~31% of providers have no CH number and get *no* director seed from this
  source; they depend entirely on Phantombuster (Phase 3) or manual entry.
- **Cost:** CH director data lags reality (filings are periodic), so a freshly
  appointed or departed director may be stale until the next CH refresh — the
  precedence rule accepts this in exchange for authoritativeness on what it does
  report.
- **Operational:** introduces an outbound dependency on the Companies House API
  (free key required) when Phase 2 implements; no runtime impact until then.
  The `companies_house_number` column adds one nullable field + index to
  `Provider` (already shipped).

## Walk-back options

- **If Companies House coverage proves too thin** (the ~69% turns out to skew
  away from the providers we actually target) — demote CH from "primary" to
  "one of two parallel seeds" and lead with Phantombuster for the target
  segment, keeping the precedence rule unchanged.
- **If the conflict rule produces bad merges in practice** (Phase 2 real data
  shows CH "director ended" facts wrongly suppressing still-valid roles) —
  revisit §3 to make CH authoritative for *identity* but not for *current-role*
  status, deferring role-currency to a freshness-weighted merge.
- **If the Companies House API terms change** to restrict automated director
  pulls — fall back to manual entry for directors and lean on Phantombuster,
  reopening this ADR.

## Links

- [`docs/product-vision.md`](../product-vision.md) — Phase 2; the
  Companies-House-first choice (line 31) and the conflict-resolution open
  question (line 104) this ADR resolves.
- [`docs/plans/companies-house-enrichment.md`](../plans/companies-house-enrichment.md)
  — the implementation plan for Phase 2.
- [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) — the additive-column
  procedure used to add `companies_house_number`.
- [ADR 0005](0005-two-stage-csv-ingest.md) — the importer that now persists the
  CH number (stage 2, `enrich_locations.py`).
- [Spike — CQC source selection](../spikes/cqc-source-selection.md) — confirms
  the CH number is present in the bulk download.
- **Dependency:** the Phase 1 `Person` / `Interaction` / `User` ADR (not yet
  written; the vision's table still lists stale numbers since ADR 0011 was
  consumed by *Defer authentication*). This ADR's seeding target is that
  `Person` entity.
