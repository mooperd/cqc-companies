# Plan — Phase 1: the smallest CRM loop

**Status:** Active.

<!--
Status lifecycle:
  Proposed → Active → Closed (YYYY-MM-DD)
Update in place; don't stack past states.
-->

## Goal

Deliver Phase 1 of [`docs/product-vision.md`](../product-vision.md): log in, see
a `Provider`, list known `Person` rows for it, and log one `Interaction` against
a `Person`. This is the smallest end-to-end CRM slice. Implements the data model
from [ADR 0012](../adr/0012-crm-person-interaction-user-model.md) and folds in
WS6 (the `Contact` reshape) from
[`initial-debt-and-questions.md`](initial-debt-and-questions.md).

## Prerequisites

- [ADR 0012](../adr/0012-crm-person-interaction-user-model.md) Accepted — CRM
  data model (Person / Interaction / User), Contact deleted.
- App-auth decision (ADR 0011 deferred it) — needed before `User` and any
  login. Not yet written; this is the gating ADR for WS3/WS4.

## Where things stand (2026-06-20)

- **WS1 — Person entity: Shipped, then reshaped (ADR 0014).** Flat `Person`
  shipped + `Contact` deleted (2026-06-20). Now being reshaped into `Person` ↔
  `Role` (one human, many roles) to support Companies House correlation — see
  [ADR 0014](../adr/0014-person-role-correlation-model.md) and
  [`companies-house-enrichment.md`](companies-house-enrichment.md). Schema rework
  pending.
- **WS2–WS5: Open / blocked.** Interaction, User+auth, and the UI loop not
  started. WS3 (User) is blocked on the auth ADR.

## Workstreams

### WS1 — `Person` entity + delete `Contact`

**Status:** Shipped (2026-06-20); **reshaped into `Person` ↔ `Role`** by
[ADR 0014](../adr/0014-person-role-correlation-model.md) (rework pending).

Flat `Person` added to `model.py` (`name`, `role`, `source`, `confidence`,
`appointment_date`/`resignation_date`, FK `Provider`) and `Contact` deleted.
**Reshape (ADR 0014):** split into a global `Person` (the correlated human:
name, surname/forenames, dob_year/month, nationality, match_confidence) and a
`Role` (per source-fact: role_type, source, confidence, start/end dates,
control_nature, FK person + provider). Enables one human with many roles, the
prerequisite the Companies House enrichment needs. Additive via
`db.create_all()` — no Alembic ([ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md)).

**Exit:** ✓ `person` table builds; a Person inserts and round-trips with its
Provider backref; `contact` table no longer exists.

### WS2 — `Interaction` entity

**Status:** Open.

A touchpoint against a `Person`: `when`, `channel` (email | phone | linkedin |
in-person | …), `direction` (out | in), `summary`, `outcome`, FK `Person`, FK
`User`. Field shapes confirmed here (ADR 0012 deferred them). The `User` FK can
land nullable until WS3 exists, or WS2 can follow WS3.

**Deliverables:** `Interaction` model; `Person.interactions` backref.

**Exit:** an Interaction can be created against a Person and queried back.

### WS3 — `User` entity + app auth

**Status:** Blocked on the app-auth ADR.

`User` `(auth identity + per-user secrets: phantombuster_api_key,
whatsapp_phone_number)` — the LinkedIn session is Phantombuster's, not stored
here ([ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md)). Needs the auth-mechanism
decision deferred in [ADR 0011](../adr/0011-defer-authentication.md) (Google
OAuth vs magic-link vs single admin — product-vision Open Questions). Write that
ADR first; it also owns the secrets-encryption posture.

**Deliverables:** the auth ADR; `User` model; login/logout; secrets storage.

**Exit:** a user can log in; Interactions attribute to the logged-in user.

### WS4 — CRM UI loop

**Status:** Open (depends on WS2, and WS3 for attribution).

The smallest UI: on a Provider page, list its `Person` rows (manually entered +
enrichment-seeded), add a Person, and log an Interaction against one. Reuses the
existing server-rendered Flask + Jinja approach
([ADR 0003](../adr/0003-server-rendered-flask-jinja.md)).

**Deliverables:** Provider-detail view listing people; add-person + log-interaction
forms/routes.

**Exit:** the end-to-end loop in the Goal works against a local Postgres.

## Phase exit criteria

When all of these are true, this plan closes:

- [x] WS1 — `Person` shipped, `Contact` deleted.
- [ ] WS2 — `Interaction` shipped.
- [ ] WS3 — `User` + app auth shipped (gated on the auth ADR).
- [ ] WS4 — the log-in → see-Provider → list-People → log-Interaction loop works.

## References

- [ADR 0012 — CRM data model (Person / Interaction / User)](../adr/0012-crm-person-interaction-user-model.md)
- [ADR 0011 — Defer authentication](../adr/0011-defer-authentication.md) — the auth ADR (WS3) picks up from here.
- [ADR 0013 — Companies House enrichment](../adr/0013-companies-house-source.md) — Phase 2; consumes `Person` (WS1).
- [`docs/plans/companies-house-enrichment.md`](companies-house-enrichment.md) — Phase 2 plan, unblocked by WS1.
- [`docs/plans/initial-debt-and-questions.md`](initial-debt-and-questions.md) — WS6 (Contact reshape) folds into WS1 here.
- [`docs/product-vision.md`](../product-vision.md) — Phase 1 in the roadmap.
