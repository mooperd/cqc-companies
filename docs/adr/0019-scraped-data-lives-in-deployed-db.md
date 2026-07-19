# ADR 0019 — Scraped LinkedIn Person/Role data lives in the deployed Postgres, not a distributed change-set

**Status:** Accepted (2026-07-19). Resolves
[`docs/spikes/scraped-data-distribution.md`](../spikes/scraped-data-distribution.md).

**TL;DR.** In the context of "how does everyone get the LinkedIn-scraped
`Person`/`Role` data when only one machine can scrape it" ([the distribution
spike](../spikes/scraped-data-distribution.md)), and now that a single hosted
Postgres exists ([ADR 0018](0018-hetzner-single-box-deploy.md)), we chose to
make **the deployed database the authoritative home** for scraped personal data
and **not build** the ADR-0015-style change-set emit/apply mechanism the spike
framed. This is the spike's **Option C** (a mutable, access-controlled store)
realised by infrastructure we already have. The cost: the deployed DB now holds
**non-regenerable** state, so **automated backups become required** (this trips
[ADR 0018](0018-hetzner-single-box-deploy.md)'s own walk-back trigger) — that is
the one obligation this decision creates and it is the immediate next task, not
solved here.

## Context

The [distribution spike](../spikes/scraped-data-distribution.md) posed a real
problem: CQC provider/facility data rebuilds from committed seed CSVs and CH data
re-derives from the live API, but **LinkedIn-scraped people exist only in a
`pg_dump`** — a live, gated, non-deterministic scrape ([ADR 0016](0016-linkedin-phantombuster-ingestion.md))
that runs only on a machine with a live LinkedIn session ([the acquisition
spike](../spikes/linkedin-acquisition-approach.md)). So one person scrapes; how
does everyone else get that data without re-scraping (they can't) or passing
whole DB dumps around (clunky, all-or-nothing)?

The spike's appealing answer was to mirror [ADR 0015](0015-data-freshness-strategy.md):
emit scraped rows as **dated, ordered, replayable change-sets**. It separated the
ADR 0015 *mechanism* (good — reuse it) from the *storage* (the hard part), and
ruled out plaintext-in-git (**Option A**) because git history is immutable and
replicated, so PII in git is **un-erasable** — a direct conflict with
[ADR 0017](0017-gdpr-controller-posture.md) §5. That left **B** (encrypted
change-sets in git) or **C** (change-sets in a mutable, access-controlled store),
and the spike parked the choice as a human decision — noting C "introduces
infrastructure this local-only project doesn't yet have."

**That premise changed.** [ADR 0018](0018-hetzner-single-box-deploy.md) (2026-07-18)
stood up a single hosted Postgres, and `provision.sh` already restores the
enriched dump onto it. The out-of-band, access-controlled store the spike's
Option C wanted now exists. And once there is one shared database, the
distribution *problem itself* dissolves — everyone reads the same DB; the scrape
writes `Person`/`Role` straight into it (`enrich_linkedin` already writes to
whatever `DATABASE_URL` points at). There is nothing to distribute.

## Decision

1. **The deployed Postgres is the authoritative home for scraped LinkedIn
   `Person`/`Role` data.** The scrape writes into it directly; every consumer
   reads it. There is no separate distribution artefact for this data.

2. **We do not build the ADR-0015 change-set mechanism for scraped data.** No
   dated `Person`/`Role` change-set emit, no `apply_events`-style replay for the
   LinkedIn source, no encrypted-in-git store, no key distribution. The spike
   framed all of this; a shared DB makes it unnecessary. ADR 0015's change-set
   model continues to govern **CQC and Companies House** data (which is
   rebuildable from source and belongs in git); it does **not** extend to the
   scraped source.

3. **This is the spike's Option C, lighter.** A mutable, access-controlled store
   honours erasure natively — `erase_person` actually DELETEs the rows and
   `SuppressedContact` still blocks re-ingestion ([ADR 0017](0017-gdpr-controller-posture.md)
   §5, `suppression.py`). The deployed box is that store; we add no new
   infrastructure beyond ADR 0018.

4. **Automated backups become required — this is the load-bearing consequence.**
   Making the deployed DB *authoritative* for non-regenerable data trips
   [ADR 0018](0018-hetzner-single-box-deploy.md)'s explicit walk-back trigger
   ("if the DB starts holding non-rebuildable state … add real backups … since
   'just rebuild it' stops being a recovery path"). "Regenerate the scrape" is
   **not** a recovery path: it is non-deterministic, gated, and may be unrunnable
   without a fresh session. So the deployed DB is a single point of data loss for
   the scraped rows, and a backup (Hetzner snapshots or `pg_dump` → object
   storage, with a stated retention window) is now mandatory. **This ADR requires
   it; a follow-up implements it** (see [the plan](../plans/linkedin-ingestion.md)).

5. **Local development keeps the dump-restore path.** A developer running locally
   either points `DATABASE_URL` at the deployed DB or restores a shared `pg_dump`
   (the README path). Local machines are not authoritative and need no scraped
   data to run the CQC/CH layers.

## Alternatives considered

- **B — encrypted change-sets in git** (reuse `secrets_box`/Fernet) — rejected
  now that a shared DB exists: it solves a distribution problem we no longer have,
  and buys a worse erasure story (rotate-and-destroy-key, with old ciphertext
  persisting in history) plus permanent key-distribution/rotation overhead. It
  was the right answer *only* under the spike's original "git is the only shared
  channel" constraint, which ADR 0018 removed.
- **A — plaintext change-sets in git** — rejected by the spike and still rejected:
  un-erasable PII in immutable, replicated git history violates
  [ADR 0017](0017-gdpr-controller-posture.md) §5.
- **D — keep sharing `pg_dump`s as the primary channel** — rejected as the
  *authoritative* model: opaque, all-or-nothing, not incremental, and every copy
  is another uncontrolled personal-data store to track for erasure. It survives
  only as the **local-dev convenience** (§5), not as the source of truth.
- **Build the change-set mechanism anyway, targeting the DB** — rejected as
  premature machinery: a single shared DB needs no replay layer to distribute
  rows to itself. Revisit only if a second authoritative store or an offline-first
  consumer appears (Walk-back).

## Consequences

- **Positive — the distribution problem is gone and the GDPR posture improves.**
  No mechanism to build; erasure works natively against a live store (strictly
  better than git-immutability, which was the whole reason Option A failed). The
  scrape's existing write path needs no change — point `DATABASE_URL` at the box.
- **Cost — backups are now mandatory, not deferred.** The deployed DB is a single
  point of loss for non-regenerable data. Until the backup lands, that window is
  open and is called out as the immediate next task. This **amends
  [ADR 0018](0018-hetzner-single-box-deploy.md)**: its "no automated backup yet …
  deferred" Consequence and its walk-back trigger are now *tripped* — backups move
  from deferred to required.
- **Cost — a backup-retention erasure lag.** This **amends
  [ADR 0017](0017-gdpr-controller-posture.md)** §5: erasure now DELETEs from the
  live store immediately, but a person erased today still sits in backups taken
  before the erasure until those backups age out. That lag must be bounded by a
  **stated backup-retention window** (part of the backup follow-up) and noted in
  the DSR runbook. This is far softer than git's forever-immutability — backups
  rotate; history does not — but it is not zero and must be documented.
- **Operational — the box holds real personal data behind basic-auth only.**
  Reinforces [ADR 0017](0017-gdpr-controller-posture.md) §9 / the
  [ADR 0011](0011-defer-authentication.md) walk-back: real app auth grows more
  urgent as the deployed DB becomes the authoritative personal-data store.
- **No schema change.** `Person`/`Role`/`SuppressedContact` are unchanged; this is
  a decision about *where the data lives and how it's recovered*, not its shape.

## Walk-back options

- **If a second authoritative store or an offline-first consumer appears** (e.g. a
  team member who must hold scraped data locally and sync) — the distribution
  problem returns, and the spike's change-set mechanism (Option B or C-with-replay)
  is the design to revive. This ADR is scoped to "one shared DB is the home."
- **If backups prove insufficient for the erasure bar** (regulator or DSR review
  finds the retention-window lag unacceptable) — shorten the backup-retention
  window, or move to a managed Postgres with point-in-time controls
  ([ADR 0018](0018-hetzner-single-box-deploy.md) already names managed PG as the
  walk-back when the DB holds non-rebuildable state).
- **If the box is lost before the backup follow-up lands** — the scraped rows are
  gone and must be re-scraped from scratch (gated, non-deterministic). This is the
  open-window risk that makes the backup task urgent, not optional.

## Links

- [`docs/spikes/scraped-data-distribution.md`](../spikes/scraped-data-distribution.md)
  — the spike this resolves (marked Resolved, pointing here).
- [ADR 0018](0018-hetzner-single-box-deploy.md) — the deployed box that is now the
  home; **amended** (backups required, walk-back trigger tripped).
- [ADR 0017](0017-gdpr-controller-posture.md) — GDPR posture; **amended** (erasure
  reaches the live store natively; backup-retention window is the erasure lag).
- [ADR 0015](0015-data-freshness-strategy.md) — the change-set mechanism we
  deliberately do **not** extend to the scraped source (still governs CQC/CH).
- [ADR 0016](0016-linkedin-phantombuster-ingestion.md) — why the scrape is
  non-deterministic and gated (so "regenerate" is not a recovery path).
- [`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md) — where the
  required backup follow-up is tracked.
