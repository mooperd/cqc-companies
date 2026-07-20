# Plan — LinkedIn identification via Phantombuster (executing ADR 0016)

**Status:** Active (started 2026-06-29). **The code mechanism is complete —
PWS0–PWS5 all landed (2026-07-03/04), offline/fixture-tested.** What remains is
operational/external: a live run (real keys + credits) and the ADR 0017 legal
sign-off (LIA, privacy notice, ICO). Closes when the first gated live run is
authorised and executed.

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

## Pivot (2026-07-02) — depend on `phantombuster-lib`

Live Phase-3 validation worked but exposed the store-phantom path as high-friction
(Google-Sheet feed, per-phantom session-connect, a badly fuzzy store URL-Finder).
[`mooperd/phantombuster-lib`](https://github.com/mooperd/phantombuster-lib) already
solves acquisition more cleanly (custom UK-HQ resolver phantom keyed on CQC
`brandName` → numeric `companyId` → Search Export → `resultObject`, inline
argument, no feed). Per [ADR 0016 Amendment (2026-07-02)](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-02--acquisition-via-phantombuster-lib)
we **depend on the lib** and retire our stdlib client. New workstreams supersede
the store-phantom ones below (kept as the historical record of what we learned).

### PWS0 — package `phantombuster-lib` (cross-repo)

**Status:** ✅ Done (2026-07-03) —
[PR #2](https://github.com/mooperd/phantombuster-lib/pull/2), rebase-merged.
**Pin PWS1's git dependency to commit `2189f627cb14a811221c22cf7847efc9d67d9fec`.**

What landed:
- `pyproject.toml` (hatchling): ships three top-level packages —
  `phantombuster`, `cqc`, `resolver`. Core dep is `requests`; Flask/SQLAlchemy
  are a `[webapp]` extra (the demo webapp + `examples/` are dev-only and
  **excluded from the wheel**).
- **Resolver consolidated** into one `resolver/` package (`core.py`), replacing
  the duplicate that lived in `webapp/resolver.py` **and** was re-implemented in
  `examples/cqc_to_linkedin.py`. The two had drifted into **two divergent `.js`
  phantoms** — kept the richer webapp one (UK-HQ geo facet + About-page scrape)
  as canonical, shipped as package data; dropped the simpler examples variant.
- Exposes **both** launch paths off the one phantom: `launch_resolution`
  (managed persistent agent, webapp) and new `resolve_ephemeral`
  (one-shot create→run→delete, CLI). Repointed webapp + both examples at
  `import resolver`.

**Verified** in a throwaway venv: `pip install ".[webapp]"` succeeds; from
outside the source tree `from phantombuster import Phantombuster`,
`from cqc import CQC`, `from resolver import ...` all import; the phantom is
present as installed package data; the Flask app factory builds and its resolve
blueprint binds to the promoted package; webapp/examples absent from the wheel.

**Earlier detour (2026-07-02):** [PR #1](https://github.com/mooperd/phantombuster-lib/pull/1)
removed committed live API keys (public repo) — `webapp/clients.py` is now
fail-closed. **The exposed keys are Andrew's and are pending rotation** (still in
git history).

### PWS1 — depend + retire our client

**Status:** ✅ Done (2026-07-03, commit on `main`). Added the pinned git dependency
`phantombuster-lib @ git+…@2189f627` to `requirements.txt`; **deleted
`phantombuster.py` + `test_phantombuster.py`** (the retired stdlib transport). The
API shapes it verified live are preserved in the spike.

What landed beyond the literal delete (the transport was coupled to the ingest
path, so retiring it touched two more places):
- **`linkedin_profiles.py`** (new) holds the transport-free ingest contract that
  used to live in `phantombuster.py`: `ScrapedProfile` + `parse_profile`/
  `parse_profiles` — **the row→identity mapping PWS3 reuses** (incl. the
  live-confirmed `job` field). Kept ours deliberately.
- **`enrich_linkedin.run_identification_phantom`** reworked onto the lib's
  `Phantombuster` (`launch` → `get_container` poll → `get_result` → `parse_profiles`
  → ingest); client injectable for tests; PhantomRun status/credit semantics
  preserved. `test_enrich_linkedin`'s lifecycle test now mocks a fake lib client
  returning raw rows.

**Verified:** `pip install -r requirements.txt` pulls the lib from the pinned
commit; `from phantombuster import Phantombuster` resolves to the lib (our module
is gone); the full offline suite is green without our client.

### PWS2 — resolver: provider → numeric `companyId`

**Status:** ✅ Done (2026-07-03, commit `1e99e8a` on `main`).

What landed (`resolve_company_id.py`, offline/fixture-tested — the live resolver
call is gated):
- additive **`Provider.linkedin_company_id`** (nullable, indexed; ADR 0002).
- **`verify_match`** (pure) — the trust gate: website-domain agreement (hard
  reject on mismatch) → **name similarity** (rejects the fuzzy-search wrong
  company, e.g. `Scarborough Hall → rbrecycling`) → town corroboration.
- **`resolve_provider`** — fetch `brandName`/`brandId`/`website`/town via the lib's
  injected `CQC` client, run the injected resolver, `verify_match`, and store the id
  only if verified; **caches `brandId → companyId`** so siblings under one brand
  don't re-run the paid resolver; skips deregistered providers.
- **`live_resolver` / `resolve_all`** — gated batch driver on the lib's
  `Phantombuster` + ephemeral resolver phantom (needs real keys; not run offline).

**Update (2026-07-07):** the resolver moved to a **no-auth public-page fetch** — the
phantom search was flaky and the id is in the public HTML, no session/credits needed
([ADR 0016 amendment 2026-07-07](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-07--resolver-pws2-is-a-no-auth-public-page-fetch);
spike: [linkedin-acquisition](../spikes/linkedin-acquisition-approach.md)). New
`linkedin_public.py`; `public_resolver()` is the default `Resolver`; `resolve_all`
dropped `pb`; `live_resolver` (phantom) kept as a fallback. Verified live against
Barchester/HC-One/Sanctuary/Care UK; deterministic.

**Verified:** `test_resolve_company_id.py` + `test_linkedin_public.py` — verified match
stored + cached, brand cache hit skips the resolver, bad match rejected, deregistered
skipped, no-match, `verify_match` branches, slug candidates, public-page extraction,
and the all-boilerplate-name fallback. Full offline suite green.

### PWS3 — consume profiles → `Person`/`Role`

**Status:** ✅ Done (2026-07-04, commit `d01b13f`). `enrich_linkedin` gained the
Search Export consumer: `company_people_search_url(company_id)` builds the
`currentCompany=["<id>"]` people search; `run_company_people` wires
`Provider.linkedin_company_id` → Search Export → the existing
`run_identification_phantom` lifecycle (launch→poll→fetch→`parse_profiles`→
`ingest_run`) on the lib client, reusing `sync_profiles` (low-confidence,
linkedin_url dedup, **no merge into DOB-anchored CH directors**).

**Verified:** `test_enrich_linkedin` — Search Export rows → low-confidence
Person/Role for the provider; refuses an unresolved provider; the currentCompany
filter asserts + asserts we inject **no** `sessionCookie` (Phantombuster owns the
LinkedIn session — [ADR 0016 amendment 2026-07-05](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-05--the-linkedin-session-is-phantombusters-not-ours));
existing dedup/no-CH-merge invariants preserved.

### PWS4 — schema + config

**Status:** ✅ Done. `Provider.linkedin_company_id` landed in PWS2;
`.env.example` gains the CQC key (commit `d01b13f`, originally
`CQC_SUBSCRIPTION_KEY`; the resolver now reads the canonical
`CQC_PRIMARY_KEY`/`CQC_SECONDARY_KEY` — 2026-07-20; the
`LINKEDIN_SESSION_COOKIE` added then was removed 2026-07-05 —
[ADR 0016 amendment](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-05--the-linkedin-session-is-phantombusters-not-ours),
Phantombuster owns the LinkedIn session). **`PhantomRun`'s fate: kept as the optional per-scrape audit record**
(ADR 0016 amendment) — still written by `run_identification_phantom`, not on the
critical path.

### PWS5 — GDPR durable erasure (the persistence gate)

**Status:** ✅ Done (2026-07-04, commit `d01b13f`) — implements
[ADR 0017](../adr/0017-gdpr-controller-posture.md) §5, the load-bearing mechanism
that gates real persistence.
- `SuppressedContact` tombstone (hashed identifier + reason + date; never the
  profile) + `Person.acquired_at` (retention anchor).
- `suppression.py`: `is_suppressed`, `suppress`, `erase_person` (delete
  Person+Roles, write tombstones), `purge_stale` (retention, no tombstone).
- The suppression check gates **both** ingest paths before creating a `Person`
  (`find_or_create_person` for CH, `find_or_create_linkedin_person` for LinkedIn).

**Verified:** `test_suppression.py` — an erased person is deleted with a tombstone
and a **re-scrape of their company does not resurrect them**; name-suppression
also blocks the CH path; retention purge removes only stale, relationship-less,
scraped contacts.

**Still gated (outside code):** the first *live* scrape needs real keys **and** the
ADR 0017 legal sign-off (a reviewed LIA, privacy notice, ICO registration).

## Prerequisites for a LIVE run (gates, not built here)

1. **A Phantombuster account + API key**, and the identification phantoms set up
   (Sales Nav Search Export, Company People Scraper, Profile Data). No sandbox
   exists — live runs cost credits.
2. **[ADR 0017](../adr/0017-gdpr-controller-posture.md) — GDPR controller posture
   (Accepted).** The durable-erasure **mechanism has shipped** (PWS5 —
   `suppression.py` + the on-ingest check). What remains is the **legal sign-off**
   it flags — a reviewed LIA, the privacy-notice wording, ICO registration — which
   code cannot certify and which must precede the first live scrape.
3. **Per-user secret populated** — a real `User` with their encrypted
   `phantombuster_api_key`, plus `APP_SECRETS_KEY` configured for at-rest
   encryption. (No LinkedIn cookie — Phantombuster's connected session is the
   identity; [ADR 0016 amendment 2026-07-05](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-05--the-linkedin-session-is-phantombusters-not-ours).)

## Workstreams (historical — superseded by the 2026-07-02 pivot above)

*WS1–WS3 shipped and taught us the API + field names (preserved in the spike);
the acquisition mechanism they built is retired in favour of `phantombuster-lib`.
Kept here as the record of what was learned. `Person`/`Role`/`User`/`secrets_box`
schema from WS1 survives; `phantombuster.py`/`enrich_linkedin` are reworked.*

### WS1 — Schema + secrets (offline)

**Status:** Shipped (2026-06-29).

- `secrets_box.py` — Fernet encrypt/decrypt keyed off `APP_SECRETS_KEY`,
  **fail-closed** (refuse to store a secret if no key is configured). Adds the
  `cryptography` dependency.
- `model.py` (additive, [ADR 0002](../adr/0002-postgres-sqlalchemy-no-migrations.md)):
  - **`User`** (minimal): `id`, `name`, `email`, encrypted
    `phantombuster_api_key` (stored as a `*_enc` ciphertext column; plaintext only
    ever in memory via `secrets_box`). *(WS1 also shipped an encrypted
    `linkedin_session_cookie`, removed 2026-07-05 —
    [ADR 0016 amendment](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-05--the-linkedin-session-is-phantombusters-not-ours);
    Phantombuster owns the session.)*
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
against fixtures with a mocked HTTP layer (no live key). Transport reconciled
against the public v2 API — see [`docs/spikes/phantombuster-api.md`](../spikes/phantombuster-api.md):
the canonical result path is the S3 `result.json` (not `resultObject`), status is
gated on `lastEndStatus`, and the `argument` is JSON-encoded. *Per-phantom result
**field names** remain INFERRED — one live run confirms them (the spike's residual
list).*

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

### WS6 — Durable erasure + retention ([ADR 0017](../adr/0017-gdpr-controller-posture.md))

**Status:** Open — **gates the first live scrape** (real personal data).

The load-bearing GDPR mechanism (ADR 0017 §5): erasure/objection must survive
re-scraping.
- `SuppressedContact` (additive): hashed identifier (`linkedin_url` and/or
  normalised name) + reason + date — a tombstone, **not** the profile.
- An erasure path that deletes the `Person`/`Role` AND writes the tombstone.
- `sync_profiles` (WS3) + the CH enrichers consult `SuppressedContact` **before**
  creating a `Person`, so an erased person is never resurrected.
- `Person` acquisition timestamp + a retention purge job (no `Interaction` / no
  role change within the window; default 24 months — a knob).

**Exit:** an erased person is deleted and a re-scrape of their company does not
re-create them (suppression hit); retention purge removes stale no-relationship
contacts. (Operational/legal tasks — LIA, privacy notice, ICO — tracked outside
this plan.)

## Phase exit criteria

- [x] WS1 — User + Person.linkedin_url + PhantomRun build; secret round-trips encrypted.
- [x] WS2 — Phantombuster client + parsers, fixture-tested. *(client since retired
      for phantombuster-lib — PWS1.)*
- [x] WS3 — profile→Person/Role with linkedin_url dedup + no-CH-merge, tested.
- [x] PWS0 — phantombuster-lib packaged + resolver consolidated.
- [x] PWS1 — depend on the lib; our stdlib client retired.
- [x] PWS2 — provider → verified `Provider.linkedin_company_id`.
- [x] PWS3 — Search Export consumer → `Person`/`Role`.
- [x] PWS4 — schema + config (`.env.example`); `PhantomRun` = optional audit.
- [x] PWS5 — durable erasure + suppression + retention (ADR 0017 §5).
- [x] ADR 0016 Proposed → **Accepted** (mechanism complete + acquisition validated
      live; the full consumer path is fixture-tested pending the gated live run).
- [x] ADR 0017 Proposed → **Accepted** (erasure mechanism shipped; legal sign-off
      remains an external prerequisite before the first live scrape).
- [ ] **Live run** — execute the first gated identification run once keys + the
      ADR 0017 legal sign-off are in place, then close this plan.

## Open follow-ups (post-2026-07-07 live test)

A gated live run (Exp 2, `--limit 10`) returned 10 real people and the resolver
moved to a no-auth public-page fetch ([ADR 0016 amendment 2026-07-07](../adr/0016-linkedin-phantombuster-ingestion.md#amendment-2026-07-07--resolver-pws2-is-a-no-auth-public-page-fetch)).
These items remain, folded here from the resolved
`linkedin-ingestion-followups` handoff:

1. **Scraped-data home — DECIDED.** The distribution question is resolved:
   scraped `Person`/`Role` data lives in the **deployed Postgres**, no change-set
   mechanism ([ADR 0019](../adr/0019-scraped-data-lives-in-deployed-db.md)).

2. **Backups — mechanism BUILT; operator step + real-Storage-Box verification
   remain.** ADR 0019 made the deployed DB authoritative for non-regenerable
   scraped rows, tripping [ADR 0018](../adr/0018-hetzner-single-box-deploy.md)'s
   backup walk-back trigger. Built: `deploy/backup.sh` (encrypted Borg → Hetzner
   Storage Box, `keep-daily=14` — that window is the ADR 0017 §5 erasure-lag bound)
   + daily systemd timer + provision.sh wiring + a restore drill. **Remaining
   (operational):** provision a Storage Box, authorise the box's backup key, set
   `BORG_REPO`, and confirm `backup.sh restore-latest` round-trips on the live box.
   See [`docs/plans/hetzner-deploy.md`](hetzner-deploy.md) and
   [`deploy/README.md`](../../deploy/README.md#backups). Until the operator step is
   done the box remains a single point of loss.

3. **ADR 0017 legal sign-off gates *production* scraping (external, not code).**
   The erasure/suppression mechanism has shipped (`suppression.py`); what remains
   is the **right-sized** floor — ICO registration + a one-page LIA + a one-page
   privacy notice ([ADR 0017](../adr/0017-gdpr-controller-posture.md) amendment
   2026-07-20; DPO not required at this scale). The one gated live run was a
   controlled test; **new** at-scale scraping + real outreach wait on this. **But
   the product demos ahead of sign-off** on ungated data (public CQC + CH +
   non-personal resolver linkage + existing/synthetic people) — see
   [`client-demo.md`](client-demo.md). Keep the "Live run" box open until legal
   clears production.

4. **Resolver: runnable + an off-box operational model (2026-07-20).** Built the
   runnable driver (`resolve_company_id.main`: `--limit`, `--dry-run`,
   `--emit-changeset`) and reconciled the key to `CQC_PRIMARY_KEY`/`SECONDARY`.
   **Key operational finding:** the no-auth public resolver **works from a
   residential IP but is authwalled (HTTP 999) from the Hetzner datacenter box** —
   so it **cannot run on the box** (the acquisition spike's datacenter-IP wall
   applies to the public resolver too). Operational model landed (option C):
   - run the resolver **off-box** (a residential IP) against a local DB copy, with
     `--emit-changeset` writing `data/changes/linkedin-resolver-<date>.json`
     (non-personal company ids → plaintext-in-git is fine, ADR 0015/0019);
   - **replay on the box** via `apply_events` (`apply_linkedin_resolver_file` +
     `apply_pending` glob) — no LinkedIn touched there, so the box IP is fine;
     idempotent (ledger + per-row guard).
   - Also made `resolve_all` **fault-tolerant** — a per-provider CQC 5xx/timeout is
     tallied `error` and the batch continues (a 37k run can't abort on one bad row).
   - Proven end-to-end: 12/40 resolved off-box → change-set → applied on box
     (1→13 resolved). **Still open:** the full ~37k sweep is an ongoing *paced*
     job — even a residential IP throttles after enough rapid fetches, so add a
     fetch delay + run in batches; and the earlier polish items remain:
     `linkedin_public._fetch` gzip/connection-reuse, and a **golden real-page
     fixture** so LinkedIn markup drift fails CI loudly (today the regex fails
     *closed* → silent resolution-rate drop; tests use synthetic HTML only).

5. **`pb_doctor` session-freshness check (small).** The doctor checks the cookie
   **exists**, not that it's **valid** — a stale-but-present cookie passes yet the
   scrape fails with `exit 84`. It could inspect the agent's last container for
   `exit 84` and warn.

## References

- [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md) — the decision.
- [ADR 0014](../adr/0014-person-role-correlation-model.md) — Person/Role + correlation reused here.
- [ADR 0013](../adr/0013-companies-house-source.md) — source hierarchy (LinkedIn never overrides CH).
- [ADR 0012](../adr/0012-crm-person-interaction-user-model.md) — introduced User (built minimally here).
- [`product-vision.md`](../product-vision.md) — Phase 3.
