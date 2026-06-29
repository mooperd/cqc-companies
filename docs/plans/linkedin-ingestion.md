# Plan — LinkedIn identification via Phantombuster (executing ADR 0016)

**Status:** Active (started 2026-06-29).

<!-- Status lifecycle: Proposed → Active → Closed (YYYY-MM-DD) -->

## Goal

Implement [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md): scrape
LinkedIn for the non-director decision-makers Companies House cannot see, into
low-confidence `Person` + `Role(source=phantombuster:<phantom>)` rows, via a
persisted `PhantomRun` runtime run under a per-user LinkedIn session +
Phantombuster key.

This plan builds the **offline, fixture-tested mechanism** — the client, parsers,
mapping/correlation, and schema — exactly as the Companies House client was built
and unit-tested before a live key existed. The **live scrape stays gated** behind
three prerequisites (below) and is not part of this slice.

## Prerequisites for a LIVE run (gates, not built here)

1. **A Phantombuster account + API key**, and the identification phantoms set up
   (Sales Nav Search Export, Company People Scraper, Profile Data). No sandbox
   exists — live runs cost credits.
2. **[ADR 0017](../adr/0017) — GDPR controller posture.** ADR 0016 §Consequences:
   this *must land before the first live identification run*. The first scrape
   makes us a UK data controller; retention/erasure fields on `Person` are shaped
   there.
3. **Per-user secrets populated** — a real `User` with their encrypted
   `linkedin_session_cookie` + `phantombuster_api_key`, plus `APP_SECRETS_KEY`
   configured for at-rest encryption.

## Workstreams

### WS1 — Schema + secrets (offline)

**Status:** Shipped (2026-06-29).

- `secrets_box.py` — Fernet encrypt/decrypt keyed off `APP_SECRETS_KEY`,
  **fail-closed** (refuse to store a secret if no key is configured). Adds the
  `cryptography` dependency.
- `model.py` (additive, [ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md)):
  - **`User`** (minimal): `id`, `name`, `email`, encrypted
    `linkedin_session_cookie` + `phantombuster_api_key` (stored as `*_enc`
    ciphertext columns; plaintext only ever in memory via `secrets_box`).
  - **`Person.linkedin_url`** — nullable, indexed; the LinkedIn identity + dedup key.
  - **`PhantomRun`** — `id`, `phantom`, `user_id` FK, `provider_id` FK nullable,
    `input` JSON, `status` (queued|launched|running|finished|failed),
    `launched_at`, `finished_at`, `output_ref`, `credits_spent`, `error`.

**Exit:** tables/columns build via `create_all`; a User round-trips an encrypted
secret; setting a secret with no `APP_SECRETS_KEY` raises.

### WS2 — Phantombuster API client (offline)

**Status:** Shipped (2026-06-29).

`phantombuster.py`, stdlib-only (urllib + json), mirroring `companies_house.py`:
- Transport: `launch_agent` (POST), `fetch_container` (poll status),
  `fetch_output` (result), with retry/backoff on transient codes; auth via
  `X-Phantombuster-Key-1`.
- Parsers: a phantom's result payload → `ScrapedProfile`
  (name, linkedin_url, headline, company, location), tolerant of field-name
  variation across phantoms.
- CLI for manual checks once a key exists.

**Exit:** `test_phantombuster.py` covers request construction + result parsing
against fixtures with a mocked HTTP layer (no live key). *Field names are
representative and flagged for validation against a live run (ADR 0016).*

### WS3 — Ingestion: profile → Person/Role (offline)

**Status:** Shipped (2026-06-29).

`enrich_linkedin.py`:
- `sync_profiles(session, provider, profiles, phantom, observed_at)` — find-or-
  create `Person` by **exact `linkedin_url`** first, else the no-DOB name path
  ([ADR 0014](../adr/0014-person-role-correlation-model.md)); attach a `Role`
  (`source=phantombuster:<phantom>`, `confidence=low`).
- **No auto-merge into DOB-anchored CH people** — the no-DOB path only matches
  among `dob_year IS NULL`, so a CH director (DOB set) is never absorbed; the
  duplicate is tolerated + flagged `match_confidence='low'` (ADR 0016 §5).
- `ingest_run(session, run)` — given a finished `PhantomRun` + its fetched
  output, parse → `sync_profiles`, update run status/credits.
- A gated live driver (`launch→poll→fetch→ingest`) that needs a key (won't run
  offline, mirrors `enrich_people.enrich_all`).

**Exit:** `test_enrich_linkedin.py` — linkedin_url dedup (re-scrape = same
Person), no-merge-into-CH-director, low-confidence + `phantombuster:<phantom>`
source, idempotent re-sync.

### WS4 — Live scrape (gated, deferred)

**Status:** Blocked on the three prerequisites above. The driver exists (WS3) but
is not run until an account + ADR 0017 + per-user secrets are in place.

### WS5 — Merge-review Tasks (deferred)

**Status:** Deferred to Phase 4. A LinkedIn↔CH provider-scoped name match becomes
a `merge_person` review Task once the Task entity (ADR 0019) exists. Until then
duplicates are tolerated and flagged.

## Phase exit criteria

- [x] WS1 — User + Person.linkedin_url + PhantomRun build; secret round-trips encrypted.
- [x] WS2 — Phantombuster client + parsers, fixture-tested.
- [x] WS3 — profile→Person/Role with linkedin_url dedup + no-CH-merge, tested.
- [x] CI smoke imports the new modules + asserts schema.
- [ ] ADR 0016 Proposed → Accepted (after a live run proves the mechanism — WS4).

## References

- [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md) — the decision.
- [ADR 0014](../adr/0014-person-role-correlation-model.md) — Person/Role + correlation reused here.
- [ADR 0013](../adr/0013-companies-house-source.md) — source hierarchy (LinkedIn never overrides CH).
- [ADR 0012](../adr/0012-crm-person-interaction-user-model.md) — introduced User (built minimally here).
- [`product-vision.md`](../product-vision.md) — Phase 3.
