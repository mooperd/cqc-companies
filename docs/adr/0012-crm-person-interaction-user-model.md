# ADR 0012 — CRM data model: Person, Interaction, User (replacing Contact)

**Status:** Accepted (2026-06-20). `Person` implemented; `Interaction` + `User` decided here but deferred — see Decision §4. **Amended 2026-06-21:** the flat `Person` is reshaped into `Person` ↔ `Role` by [ADR 0014](0014-person-role-correlation-model.md) — see Amendment below.

## Amendment (2026-06-21) — Person split into Person + Role

The flat `Person` decided here (one row per person, carrying `role`/`source`/
dates against a provider) proved unable to represent one human holding multiple
roles — which Companies House requires, since the same person appears as both a
director and a person with significant control, and as a director of multiple
providers. [ADR 0014](0014-person-role-correlation-model.md) supersedes the
`Person` shape: `Person` becomes the correlated human and a new `Role` entity
carries each source-fact (role_type, source, confidence, dates). The
`Interaction` and `User` decisions here are unchanged; `Interaction` will FK
`Person` as before.

**TL;DR.** In the context of turning the CQC directory into the relationship CRM
described in [`docs/product-vision.md`](../product-vision.md), facing a `Contact`
placeholder whose flat shape mirrored the old Provider+Location row, we chose to
model the CRM tier as **three entities — `Person`, `Interaction`, `User`** — and
delete `Contact`, implementing `Person` first because it's the only piece the
Companies House enrichment ([ADR 0013](0013-companies-house-source.md)) is
blocked on, accepting that `Interaction` and `User` land later.

## Context

The product is becoming a relationship CRM targeting decision-makers at
CQC-regulated organisations (product-vision). The `Contact` model in `model.py`
was reserved for this ([ADR 0001](0001-provider-facility-domain-model.md)
Amendment 2026-05-19), but its field shape was a copy of the flat
Provider+Location row (address, postcode, ratings, …) — wrong for tracking
*people and interactions*. It was imported in `app.py` but never used.

The vision's data-shape sketch (product-vision §"Data shape") calls for three
new entities hanging off `Provider`:

- **`Person`** — a decision-maker `(name, role, source, confidence, FK Provider)`.
- **`Interaction`** — a touchpoint `(when, channel, direction, summary, outcome, FK Person, FK User)`.
- **`User`** — us `(auth identity + per-user secrets)`.

[ADR 0013](0013-companies-house-source.md) (Companies House enrichment) is
blocked on `Person` existing — there is nothing to seed without it. Auth (the
`User` driver) was deliberately deferred in
[ADR 0011](0011-defer-authentication.md). So `Person` can and should land first,
independently of the other two.

## Decision

1. **Model the CRM tier as `Person`, `Interaction`, `User`** — three focused
   entities, not one reshaped flat table. **Delete `Contact`.**

2. **`Person` (implemented now).** A decision-maker at a provider:
   - `name` (not null, indexed), `role`.
   - `source` (not null) — provenance: `companies_house | phantombuster:<phantom> | manual`.
   - `confidence` — `high | medium | low`.
   - `appointment_date`, `resignation_date` — real `db.Date` (not the
     `String(50)` used for CSV-derived dates), because these arrive as ISO dates
     from the Companies House API and the [ADR 0013](0013-companies-house-source.md)
     §3 merge rule queries role-currency (`resignation_date IS NULL` = active).
   - `provider_id` — FK to `Provider` (not null, indexed); `Provider.people`
     backref mirrors `Provider.facilities`.

3. **Provenance over a generic "contact".** `Person` carries `source` +
   `confidence` from the start so the multi-source identification story
   (Companies House, then LinkedIn, then manual) composes under the ADR 0013
   precedence rule. This is the load-bearing difference from the old `Contact`.

4. **`Interaction` and `User` are decided here but deferred.** Their existence
   and rough shape are committed (above); their exact field shapes — encryption
   posture for per-user secrets, `Interaction` channel enum, on-delete
   behaviour, the touchpoint counter — are confirmed when implemented, in the
   later phases that need them ([`docs/plans/crm-phase1.md`](../plans/crm-phase1.md)).
   `User` in particular pulls in the app-auth decision deferred by ADR 0011, so
   it does not land with `Person`.

## Alternatives considered

- **Reshape `Contact` in place** (the original WS6 scope) — rejected: the vision
  upgraded one-table-reshape into a three-entity tier; reshaping `Contact` would
  have produced an awkward Person/Interaction hybrid. A clean delete + purpose-built
  entities is simpler than bending the placeholder.
- **Build all three (Person + Interaction + User) now** — rejected for this step:
  `User` requires the deferred auth decision (ADR 0011) and a secrets story;
  `Interaction` needs `User` for its "who recorded it" FK. Only `Person` is on
  the critical path (it unblocks ADR 0013). Bundling them would stall the
  unblock behind auth.
- **Store appointment dates as `String(50)`** (matching the CSV-derived date
  fields) — rejected: `Person` dates come from the CH JSON API, not CSV, and
  role-currency is a real query. `db.Date` is the right depth here.

## Consequences

- **Positive:** the Companies House enrichment ([ADR 0013](0013-companies-house-source.md))
  is unblocked — `Person` now exists as a seed target with the provenance fields
  its merge rule needs.
- **Positive:** `Contact` (dead, misleading) is gone; `model.py` now reflects
  the actual product direction.
- **Cost / partial state:** the CRM loop is incomplete — `Interaction` and
  `User` don't exist yet, so there's no way to *log* a touchpoint or attribute
  one to a user. `Person` rows can be created (manually or by future enrichment)
  but not yet acted on. Tracked in `crm-phase1.md`.
- **Operational:** adds one table (`person`) via `db.create_all()` — additive,
  no Alembic trigger ([ADR 0002](0002-postgres-sqlalchemy-no-migrations.md)).
  Dropping `Contact` removes an unused (never-populated) table; no data loss.

## Walk-back options

- **If the three-entity split proves wrong** once real interactions are logged
  (e.g. `Person` and `Interaction` want merging, or `Person` needs to attach to
  `Facility` not just `Provider`) — amend this ADR and migrate; the FK is the
  only hard coupling and it's nullable-addable in reverse.
- **If `confidence` as `high|medium|low` is too coarse** for the merge rule once
  multiple sources are live — widen to a numeric score; it's a single column.

## Links

- [`docs/product-vision.md`](../product-vision.md) — §"Data shape" and Phase 1;
  this ADR implements the `Person` portion.
- [ADR 0001](0001-provider-facility-domain-model.md) — the Provider/Facility
  model `Person` hangs off; its Amendment reserved this work.
- [ADR 0011](0011-defer-authentication.md) — why `User` (and auth) is deferred.
- [ADR 0013](0013-companies-house-source.md) — Companies House enrichment, which
  `Person` unblocks; the source/confidence fields exist for its §3 merge rule.
- [`docs/plans/crm-phase1.md`](../plans/crm-phase1.md) — remaining Phase-1 work
  (Interaction, User, auth, the CRM UI loop).
- [`docs/plans/companies-house-enrichment.md`](../plans/companies-house-enrichment.md)
  — Phase 2, now unblocked.
