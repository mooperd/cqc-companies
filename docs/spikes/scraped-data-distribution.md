# Spike — distributing scraped (personal) data reproducibly

**Status:** Open (design). Needs a decision before implementing. Raised 2026-07-06
from the [README reproducibility section](../../README.md#reproducibility--read-this-before-sharing-data).

## Question

The CQC core rebuilds from committed seed CSVs; Companies House is re-derivable from
its live API. But **LinkedIn-scraped people exist only in a `pg_dump`** — they can't
be regenerated (a live, gated, non-deterministic scrape — [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md)).
And scraping is inherently *local*: it needs a machine with a live LinkedIn session
(the [acquisition spike](linkedin-acquisition-approach.md) showed you can't lift the
session elsewhere). So one person scrapes on their machine; how does everyone else
get that data without either re-scraping (impossible for them) or passing whole
database dumps around (clunky, and all-or-nothing)?

The appealing idea: mirror **[ADR 0015](../adr/0015-data-freshness-strategy.md)** —
store scraped data as **dated change-sets applied in order**, the way CQC monthly
deltas already work (`data/changes/cqc-YYYY-MM-DD.json` → replay → reconstruct state).

## The key insight: separate the *mechanism* from the *storage*

ADR 0015 gives two separable things:

1. **A mechanism** — dated, ordered, replayable JSON deltas that reconstruct DB state
   from a baseline. This fits scraped data *perfectly*: a local scrape emits a dated
   change-set of `Person`/`Role` rows; any machine replays it deterministically via
   an `apply_events`-style step. Incremental, diffable, no re-scrape. **Reuse this.**

2. **A storage choice** — the CQC deltas live **committed in git**. This is fine for
   CQC (care providers + public officer facts). It is **not** automatically fine for
   LinkedIn data, and that's the whole difficulty.

The mechanism is not the problem. The storage is.

## Why we can't just commit scraped change-sets to git like the CQC ones

Scraped rows are **personal data** ([ADR 0017](../adr/0017-gdpr-controller-posture.md));
CQC provider rows are not. Two hard blockers:

- **Erasure vs. immutable history.** GDPR's right to erasure requires the data to
  actually cease to exist. Git history is immutable and replicated to every clone —
  you cannot erase a person from a committed change-set. Plaintext PII in git is
  **un-erasable**, which directly violates ADR 0017 §5. (The `SuppressedContact`
  tombstone stops replay from *re-adding* an erased person, but the raw profile still
  sits in git history forever — the tombstone guards the DB, not the repo.)
- **Disclosure.** A shared/public repo carrying real people's names, profile URLs and
  headlines is itself a disclosure, independent of erasure.

So "commit scraped deltas like CQC deltas" (Option A below) is off the table.

## Options

| # | Approach | Reproducible / incremental | Erasable | Cost |
|---|---|---|---|---|
| **A** | Plaintext change-sets in git (exactly like ADR 0015) | ✅ | ❌ **un-erasable** (git history) | trivial — **but rejected: violates ADR 0017** |
| **B** | **Encrypted** change-sets in git (reuse `secrets_box`/Fernet, `APP_SECRETS_KEY`) | ✅ | ⚠️ partial — drop from current set + rotate key; old ciphertext persists under the old key | key distribution + rotation |
| **C** | Change-sets in a **mutable, access-controlled store** (object store / private DB), **not** git | ✅ | ✅ mutable store → actually delete | needs shared infra |
| **D** | Status quo — share `pg_dump`s | ❌ not incremental; opaque blob | ⚠️ deletable but coarse | trivial |

Notes:
- **B** keeps git-only distribution: git holds ciphertext, the apply step decrypts.
  Erasure is imperfect (rotating the key + destroying the old one makes historical
  ciphertext undecryptable, which is a defensible "erasure" — but it re-keys everyone).
- **C** is the cleanest GDPR posture (a mutable store honours deletion natively and
  gates access), at the cost of introducing infrastructure this local-only project
  doesn't yet have. It reopens the same "where does this live" question Phase 6 raises.
- Any option that replays change-sets **must** consult `SuppressedContact` on apply
  (the ingest path already does — [suppression.py](../../suppression.py)), so a replay
  never resurrects an erased contact.

## Recommendation

1. **Adopt the ADR 0015 change-set *mechanism* for scraped data** — a local scrape
   emits a dated `Person`/`Role` change-set; `apply_events` replays it. This is the
   right shape and reuses existing machinery.
2. **Do not store personal-data change-sets as plaintext in git** (rules out A).
   Choose **B (encrypted-in-git)** or **C (out-of-band store)** by how the team
   actually wants to share:
   - git-only distribution is a hard requirement + key management is acceptable → **B**.
   - any shared infra is available / cleaner erasure matters more → **C**.
3. Whatever wins becomes an ADR that **extends ADR 0015** (change-set mechanism for a
   new source) and **amends ADR 0017** (how erasure reaches distributed change-sets).

## Open decisions (for the human)

1. **Distribution channel:** encrypted-in-git (B) or out-of-band store (C)?
2. **Is git-only a hard constraint?** (If yes → B. If no → C is cleaner.)
3. **Erasure bar:** is "rotate-and-destroy-key" acceptable as erasure (B), or do we
   need true deletion (C)?

Until this is decided, the answer to "how do I get the scraped data" stays: **restore
a `pg_dump`** (Option D), as documented in the README.
