# ADR 0014 — Person/Role model with cross-source identity correlation

**Status:** Accepted (2026-06-23).

<!--
Supersedes the flat `Person` introduced in ADR 0012 (only throwaway data
exists, so no migration). Move to Accepted once the schema + correlation are
implemented and verified against live Companies House data.
-->

**TL;DR.** In the context of seeding decision-makers from Companies House — which
exposes the same human through *two* endpoints (officers and persons with
significant control) under different name formats — we chose to split the flat
`Person` (ADR 0012) into **`Person` (the human) ↔ `Role` (one per source-fact)**,
correlating records into a single global `Person` by **DOB (month+year) + surname
+ first forename + nationality**, accepting a small false-merge risk (flagged via
a match-confidence) in exchange for one contact record per human across sources
and companies.

## Context

[ADR 0013](0013-companies-house-source.md) makes Companies House the first
identification source. CH actually exposes people two ways:

- **`/officers`** — directors/secretaries (`officer_role`, `appointed_on`,
  `resigned_on`, partial `date_of_birth`).
- **`/persons-with-significant-control`** (PSC) — beneficial owners/controllers
  (`kind`, `natures_of_control`, `notified_on`, `ceased_on`, partial
  `date_of_birth`).

WS1/WS2 (shipped) only ingested officers, into the flat `Person` from
[ADR 0012](0012-crm-person-interaction-user-model.md) (`name`, `role`, `source`,
dates, FK provider). Two problems surfaced:

1. **The same human appears in both endpoints, and as a director of multiple
   companies** — e.g. *Asam Khan* is a director **and** a 25-50% PSC of U&I Care
   (CH 07347897). A flat one-row-per-fact `Person` can't represent "one human,
   several roles", so it can't correlate.
2. **Name strings differ across endpoints**: the officer record is
   `"KHAN, Asam Tazeem"` (surname-first, middle name) while the PSC record is
   `"Mr Asam Khan"` (titled, no middle name). Exact-string matching fails; the
   reliable anchor is the **partial DOB (month+year)** CH gives for individuals
   in *both* endpoints, plus surname + first forename.

The product (CRM targeting decision-makers, [`product-vision.md`](../product-vision.md))
wants one contact record per human, ideally linked across the multiple providers
a person controls — high-value targets are people who direct/own several care
organisations.

## Decision

1. **Split `Person` into `Person` + `Role`.** `Person` is the human; `Role` is one
   per source-fact tying a person to a provider. One `Person` has many `Role`s.

   - **`Person`** (the human, global — not provider-scoped):
     `name` (display), `surname`, `forenames`, `normalized_name`, `dob_year`,
     `dob_month`, `nationality`, `match_confidence` (`high` | `low`).
   - **`Role`** (a person's affiliation to a provider, one per source-fact):
     `person_id` FK, `provider_id` FK, `role_type`
     (`director` | `psc` | `manual` | …), `source`
     (`companies_house:officers` | `companies_house:psc` | `manual` |
     `phantombuster:<phantom>`), `confidence`, `start_date`
     (appointed_on/notified_on), `end_date` (resigned_on/ceased_on),
     `control_nature` (PSC natures summary; null for officers).

2. **Global identity correlation.** On ingest, find-or-create a `Person` by the
   correlation key, then attach/update a `Role`. A `Person` spans providers, so a
   human directing several care orgs is one record with many `Role`s.

3. **Correlation key = DOB + surname + first forename, nationality must not
   conflict.** Specifically two records correlate when `dob_year` and `dob_month`
   match, normalized `surname` matches, and normalized first forename matches;
   nationality, if present on both, must agree. Middle names are ignored (the
   officer/PSC discrepancy above). Records lacking a DOB are **not** auto-merged
   (create distinct, `match_confidence='low'`). A name+DOB match where one side
   lacks nationality is still a match but may be flagged.

4. **Name parsing is source-specific, identity is source-agnostic.** Officer
   names parse as `"SURNAME, Forenames"`; PSC names parse as `"[Title] Forenames
   Surname"`. Each ingester normalizes into (`surname`, `forenames`) before
   correlation, so the stored `Person` identity doesn't depend on which endpoint
   first created it.

5. **Individuals only.** Corporate and legal-entity officers/PSCs (e.g. holding
   companies like *RSS GLOBAL LIMITED*) are **not** `Person` rows — `Person` is a
   table of contactable humans. Ownership-structure between companies is out of
   scope here (a later, separate concern if wanted).

6. **The [ADR 0013](0013-companies-house-source.md) §3 source-hierarchy now
   operates at the `Role` level.** Each `Role` carries its own `source` +
   `confidence`; manual roles win over Companies House for the same
   person+provider; CH is authoritative for director/PSC facts. Cross-source
   reconciliation remains WS3.

## Alternatives considered

- **Keep the flat `Person`, two rows per dual-role human (namespaced source)** —
  the prior decision, rejected here: it can't correlate (the whole point), and
  produces duplicate-looking contacts a CRM has to de-dupe anyway.
- **Per-provider Person scope** — rejected: loses the high-value signal of a
  person who controls multiple providers; the global key (DOB+name) makes
  cross-company linking cheap.
- **Exact normalized-name correlation** — rejected: real CH data proves the name
  string differs across endpoints (titles, middle names, surname-first vs
  forename-first). DOB is the stable anchor.
- **Correlate by full name only (no DOB)** — rejected: both too weak (common
  names collide) and too strict (name formats differ); DOB resolves both.

## Consequences

- **Positive:** one contact record per human, linked across CH's two endpoints
  *and* across the providers they control — exactly the CRM's targeting need.
- **Positive:** `Role` is the natural home for `source`/`confidence`/dates, so the
  ADR 0013 merge rule and future sources (LinkedIn, manual) compose cleanly.
- **Cost — false merges:** two different people sharing surname + first forename +
  birth month/year + nationality would merge. Rare; mitigated by `match_confidence`
  and recoverable (a split is a data fix, not a schema change).
- **Cost — false splits:** a person with no DOB, or a materially different name,
  stays separate. Acceptable; better than wrong merges.
- **Operational:** replaces the flat `person` table (ADR 0012) — additive via
  `create_all()` on the throwaway data, no migration. WS1/WS2/WS4 enrich code is
  reworked to ingest officers **and** PSC into `Person`+`Role`.

## Walk-back options

- **If false merges prove common** in real data — tighten the key (require
  nationality, add full-DOB where available via the authenticated PSC endpoint,
  or require middle-name agreement) and re-correlate; `match_confidence` already
  marks the suspect rows.
- **If global scope is too aggressive** — fall back to per-provider correlation
  (a `Role`-local Person) without schema change; only the find-or-create lookup
  narrows.

## Links

- [ADR 0012](0012-crm-person-interaction-user-model.md) — introduced the flat
  `Person` this reshapes (amended there).
- [ADR 0013](0013-companies-house-source.md) — CH as source; amended to add the
  PSC endpoint + individuals-only; its §3 hierarchy now applies per `Role`.
- [`docs/plans/companies-house-enrichment.md`](../plans/companies-house-enrichment.md)
  — WS2/WS4 reworked to populate `Person`+`Role` from officers + PSC.
- [`docs/plans/crm-phase1.md`](../plans/crm-phase1.md) — the Person reshape folds in here.
- [`product-vision.md`](../product-vision.md) — §Data shape (Person).
