# Product vision — CRM + outreach automation for CQC-regulated orgs

> **What this is.** Captures the actual product the project is becoming, so subsequent ADRs and plans have a north star to point at. Durable; updated when the strategic direction shifts (which is rarer than plan-update churn).

## What this project actually is

The repo started as a Flask directory of CQC-registered care providers + a statistics dashboard. Useful, but the real product is something larger: **a relationship-building CRM with outreach automation, targeting decision-makers at CQC-regulated organisations.** The directory features (ingest, search, statistics) are *substrate*, not the product — they're how we look up an organisation before reaching out to someone there.

The operational target is **5 touchpoints per identified decision-maker**. Everything in the data model and the automation layer answers some flavour of *"who needs touchpoint N+1, by when, via which channel, with which draft"*.

## Goals

The three load-bearing goals, as agreed:

**A. Build a DB of CQC-registered organisations and their facilities.** Largely in place: the `Provider` + `Facility` schema exists, the importers ingest CQC's bulk-download CSVs. PR #9 (in flight at the time of writing) lands the monthly cron that keeps the data fresh — once it merges, this row becomes "done".

**B. Mini CRM for tracking contact and touchpoints within those organisations.** Not built yet. The `Contact` model in `model.py` was reserved for this (see [ADR 0001](adr/0001-provider-facility-domain-model.md) Amendment 2026-05-19), but the actual CRM needs a different shape than `Contact` carries today — see "Data shape" below.

**C. Automated outreach to those decision-makers, with async human input when needed.** Not built. The "async human input" pattern is centered on a **`Task` table**: every pending human decision (approve an outbound draft, decide the next move on a reply, re-auth a stale LinkedIn cookie, "no response after touch 5 — keep going or stop?") is a database row. The web UI is the authoritative decision surface; WhatsApp and email are notification channels that *push* pending tasks to the right human and *reflect* their decision back into the row. This generalises beyond outreach approval — any work-item that the automation can't resolve alone surfaces as a Task.

## Constraints and strategic decisions

These are the design choices that scope the next 6–10 ADRs:

- **Per-user accounts.** Each team-member doing outreach has their own LinkedIn session (stored against a `User` row in our DB) and acts under their own identity. Outreach goes *from* their own LinkedIn account, not a shared automation account. This implies a `User` entity (which the user-stated goals didn't name explicitly but is a direct consequence of the "per-user" decision) and a real secrets-management story.

- **LinkedIn access via [Phantombuster](https://phantombuster.com/),** for *both* identification and contact. Two phantom risk classes worth separating in the data model and the runtime:
  - *Identification phantoms* (scrape) — Sales Nav Search Export, Company People Scraper, Profile Data. Lower risk; feed `Person` rows.
  - *Action phantoms* (write) — Auto-Connect, Message Sender, InMail Sender. Higher risk; record as `Interaction` rows of channel = `linkedin`.

- **Companies House as the first identification source** (provisional — to be confirmed in ADR 0013). Reasoning: the HSCA bulk-download already gives us each provider's Companies House number, the Companies House API is free with no meaningful rate limit, and it returns legally-named directors with appointment dates. LinkedIn-via-Phantombuster fills the gap for non-director influencers but is paid and rate-limited.

- **Approval surface = a `Task` table with notification channels layered on top.** Every pending decision is a database row (state, assignee, payload, FK to the related Interaction or phantom-run); WhatsApp / email / web UI are *views* over that table, not separate state stores. The web UI is the authoritative decision surface; channels push and reflect. Phase 4 starts with **email-only push** to lock in the channel-agnostic abstraction; WhatsApp via a Business API transport (Twilio is the likely choice) swaps in at Phase 6 once a public webhook endpoint exists.

- **GDPR posture: legitimate-interest basis** for storing personal data on B2B contacts, with explicit retention + erasure mechanism. We become a UK data controller the moment we scrape one LinkedIn profile; cannot be deferred past the first real enrichment run (Phase 3).

## Data shape (sketch — individual entities locked in by the ADRs noted in the phasing table)

```
Provider (organisation)             ← exists
  └─ Facility (registered location) ← exists
  └─ Person                          ← NEW (introduced in ADR 0011)
       (name, role, source: companies_house | phantombuster:<phantom> | manual,
        confidence, FK Provider)
  └─ Interaction                     ← NEW (introduced in ADR 0011)
       (when, channel, direction (out|in), summary, outcome,
        FK Person, FK User)
  └─ User (us)                       ← NEW (introduced in ADR 0011)
       (auth identity + per-user secrets:
        linkedin_session_cookie, phantombuster_api_key, whatsapp_phone_number)
  └─ phantom-run runtime model       ← TBD (Phase 3, ADR 0014)
       (likely a persisted entity tracking kind, inputs, status,
        outputs, credits spent — exact shape decided in 0014)
  └─ Task                            ← NEW (introduced in ADR 0017)
       (kind: approve_outreach | review_reply | reauth_linkedin | …,
        state: pending | approved | rejected | expired | snoozed,
        payload (JSON), assignee FK User, created_at, decided_at,
        FK to the relevant Interaction or phantom-run)
```

The exact field shapes (encryption posture for the per-user secrets, indexes, FK on-delete behaviour, …) are owned by the introducing ADRs. This sketch exists to make the conversation legible.

**On the `Contact` placeholder.** [`docs/plans/initial-debt-and-questions.md`](plans/initial-debt-and-questions.md) WS6 already reserved ADR 0011 for reshaping the `Contact` model into something CRM-shaped. **The vision here upgrades that scope**: instead of reshaping one table, the CRM tier introduces three (`Person`, `Interaction`, `User`) and the existing `Contact` class is deleted. Phase 1 below restates this as the explicit Phase 1 deliverable; WS6 will fold into the Phase 1 plan when that's written.

## Phased roadmap

We build the smallest meaningful slice first and add capability without invalidating what came before. Each phase ends with a working slice — deployable in principle, though no live deploy target currently exists (see cloud-infra note below).

| Phase | Goal | ADRs introduced | Rough size |
|---|---|---|---|
| **0. Foundation** | Local dev works end-to-end. CI runs integration tests against fixture data in a Postgres service container. PR #9 (CSV cron) merges. | — *(code only)* | ~1 session |
| **1. Smallest CRM loop** | Log in, see a Provider, list known People (manually entered), log one Interaction against a Person. Folds WS6 from `initial-debt-and-questions.md` into this phase. | 0011 (Person + Interaction + User), 0012 (app auth) | ~2 sessions |
| **2. Companies House enrichment** | Auto-populate `Person` rows for legally-registered directors of each provider. Manual entry still works alongside; conflict-resolution rules established when sources disagree. | 0013 (Companies House integration + source hierarchy) | ~1 session |
| **3. Phantombuster identification** | Scrape LinkedIn for non-director influencers. Phantom-run runtime model. GDPR controller posture in place. **No outreach yet.** | 0014 (Phantombuster runtime + credit accounting), 0015 (GDPR posture), 0016 (LinkedIn account hygiene) | ~3-4 sessions (three ADRs + first real scrape + GDPR work) |
| **4. First outreach channel** | Email approval-loop + email send. Locks in the channel-adapter + approval-loop abstractions. **The outreach-channel abstraction must be channel-agnostic from day one** — designed so Phase 5's LinkedIn-DM channel is an addition, not a rewrite. ADR 0017 also introduces the `Task` entity (see Data shape) — Tasks are the source of truth for any pending human decision; email/WhatsApp/web are notification views. | 0017 (Task entity + approval-loop state machine), 0018 (outreach channels — channel-agnostic) | ~2 sessions |
| **5. LinkedIn DMs as second channel** | Per-user action phantoms send real outreach. Touchpoint counter visible per Person. Triggers when touchpoint=5 (decided in ADR 0011 — see Open Questions). | extensions of 0014, 0016, 0018 | ~1 session |
| **6. WhatsApp swap** | Replace email approval with WhatsApp via the chosen transport (Twilio is the likely candidate, decided in this phase's ADR extension). Adds a public webhook receiver — reopens the deployment question. | extension of 0017; new ADR for public-endpoint hosting | ~1 session, plus WhatsApp Business onboarding wait |

The right time to **decide on cloud infrastructure** is Phase 6 — when WhatsApp's inbound webhooks force the issue. Phases 0–5 run locally + in CI without a public endpoint, so the question stays parked.

## Cloud infra has been removed (2026-05-20)

The previous AKS deploy story is gone: `.github/workflows/main.yml` and `k8s/*.yaml` were deleted, and [ADR 0008](adr/0008-aks-envsubst-deploy.md) (AKS deploy) and [ADR 0009](adr/0009-in-cluster-postgres.md) (in-cluster Postgres) are marked Withdrawn. They are kept (not deleted) so the historical reasoning survives — when Phase 6 picks the next deploy target, the successor ADR will reference them.

**Postgres still exists for local dev and CI**, but now via Docker (compose locally, service container in CI). That's not a "deploy story", it's the test bench.

## What this project is NOT

Stating the negative space so scope-creep is easier to spot:

- **Not a transactional CRM.** No deal pipelines, opportunities, quotes. The primitive is the touchpoint, not the deal. If a deal-tracking layer ever becomes needed, it sits *on top of* the touchpoint primitive, not in place of.
- **Not a CQC directory as a consumer product.** The directory UI exists to support our outreach, not as a destination site for external users.
- **Not a marketing-automation system.** No broadcast campaigns, no mass-email blasts, no segmented drip sequences against thousands of contacts. One-to-one outreach only, with per-touchpoint review.
- **Not an inbound CRM.** The expected traffic shape is *we initiate*, *they reply*. Inbound webhooks (from WhatsApp, from LinkedIn replies) feed back into Interactions but the system isn't designed around fielding cold inbound.
- **Not a generic task tracker.** The `Task` entity is specifically for pending decisions *the automation can't make on its own* (approve a draft, decide next action on a reply, re-auth a credential, etc.). It's not a Trello replacement; team todos, project planning, and ad-hoc work-tracking live elsewhere.

## Open questions to answer along the way

These are flagged here so they're not forgotten as we move phase-by-phase. Each gets resolved in the ADR for its phase.

- **App auth mechanism** — Google OAuth (per the aspirational `google_login.py`) vs. magic-link email vs. single hardcoded admin. Decided in ADR 0012 (Phase 1).
- **Touchpoint definition + stop condition** — what counts as a touchpoint (sent message? delivered? replied? unanswered DM still ticks the counter?) and what happens at touchpoint 5 (stop, escalate to a human-only follow-up, mark dormant?). Decided in ADR 0011 (Phase 1) and refined in Phase 4.
- **Team-internal privacy** — can User A see User B's Interactions with a Person? Default-open (everyone sees everything) is simplest; default-private has implications for the Interaction schema and the app's authz layer. Decided in ADR 0011 + ADR 0012 (Phase 1).
- **Conflict resolution between identification sources** — what wins when Companies House says someone is no longer a director but LinkedIn still shows the role? Decided in ADR 0013 (Phase 2).
- **Phantombuster cost budgeting** — per-user credit quotas, hard caps, what happens when a phantom run would exceed quota. Decided in ADR 0014 (Phase 3).
- **Retention / erasure mechanics** — how a target's "delete me" request flows through `Person` + `Interaction` + cached scraped data. Decided in ADR 0015 (Phase 3).
- **LinkedIn account warming policy** — how aggressive per-user phantoms can be before LinkedIn restricts the account. Decided in ADR 0016 (Phase 3).
- **Approval-loop UX details** — exact WhatsApp template formats, what's quick-yes/no vs. what's "click here for the full draft", how multi-user approval works if one person drafts and another approves. Decided in ADR 0017 (Phase 4) and refined in Phase 6.
- **Task assignment semantics** — does each Task get assigned to a specific User on creation (round-robin? based on which User the related Interaction belongs to?), or does it land in a team pool that anyone can claim? What's the snooze / re-assign UX? Decided in ADR 0017 (Phase 4).
- **Task TTL and expiry** — do Tasks expire if not acted on? What's the default time-to-live, and what happens at expiry (auto-reject the underlying action, auto-escalate, just notify)? Decided in ADR 0017 (Phase 4).
- **Task fanout policy** — when a Task is created, which channels notify (all of WhatsApp + email + web, or one based on user preference, or escalating sequence)? Decided in ADR 0017 (Phase 4) and refined alongside the WhatsApp swap.

## When to reconsider this whole direction

The ADR tradition has walk-back triggers; the vision doc should too. The whole direction is worth re-litigating if any of these become true:

- **Phantombuster gets banned or shuts down.** Forces a pivot to a different LinkedIn integration (browser-extension-based tools, paid scraping services with different ToS posture) or — if LinkedIn becomes hostile to all scraping — to channels other than LinkedIn entirely.
- **LinkedIn's automation-detection escalates** such that even modest per-user phantom rates trigger restrictions. Probably means moving LinkedIn from "outreach channel" to "research-only read".
- **The 5-touchpoint heuristic turns out wrong** after Phase 5 produces real data — too low (people aren't responding by touch 5) or too high (we're annoying recipients). The data model accommodates a different number; the strategic question is whether to use a single number at all vs. per-segment heuristics.
- **CQC changes the bulk-download terms** such that automated refresh is disallowed. Already partly mitigated by the API contingency path (see [spike](spikes/cqc-source-selection.md)), but a "we can't use CQC data at all" outcome would shift the targeting source to a different industry directory.

## References

- [ADR 0001 — Provider/Facility model](adr/0001-provider-facility-domain-model.md) (with Amendment 2026-05-19 explaining the Contact placeholder)
- [ADR 0005 — Two-stage CSV ingest](adr/0005-two-stage-csv-ingest.md)
- [ADR 0007 — CSVs in repo + bulk-download refresh](adr/0007-csvs-checked-into-repo.md) (PR #9 lands the in-flight Amendment 2026-05-19 covering Phase 0's cron)
- [Plan — Initial debt and questions](plans/initial-debt-and-questions.md) (WS5 = auth path, WS6 = Contact reshape; both fold into Phase 1)
- [Spike — CQC source selection](spikes/cqc-source-selection.md) (why we use bulk downloads, not the API)
- PR #9 will introduce `docs/plans/cqc-bulk-ingest.md` (Phase 0's data-freshness piece) when it merges.
