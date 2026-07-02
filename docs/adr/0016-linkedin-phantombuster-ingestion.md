# ADR 0016 — LinkedIn identification via Phantombuster (the phantom-run runtime)

**Status:** Proposed. *Drafted ahead of Phase 3.* **Amended 2026-07-02:** live
Phase-3 validation + the discovery of the more-advanced
[`mooperd/phantombuster-lib`](https://github.com/mooperd/phantombuster-lib)
pivot the *acquisition mechanism* — see the Amendment below. The core decision
(LinkedIn identification via Phantombuster → low-confidence `Person`/`Role`)
stands; the store-phantom + spreadsheet-feed implementation is superseded.

<!--
Scope: identification (scrape → Person/Role) ONLY. Action phantoms (Auto-Connect,
Message Sender) are outreach — Phase 5, owned by the channel ADR (0020). GDPR
controller posture is ADR 0017; account-hygiene/warming is ADR 0018.
-->

**TL;DR.** In the context of seeding the ~69% of providers whose decision-makers
are **not** Companies House directors ([ADR 0013](0013-companies-house-source.md)),
we chose **Phantombuster identification phantoms** as the LinkedIn ingestion
mechanism, modelled as a persisted **`PhantomRun`** runtime entity (async
launch→poll→fetch, run under a per-user LinkedIn session + Phantombuster key,
credit-metered). Scraped profiles become **low-confidence `Person` rows** keyed
on a new `Person.linkedin_url`, with `Role.source = phantombuster:<phantom>`
([ADR 0014](0014-person-role-correlation-model.md)). Because LinkedIn gives **no
DOB**, these never auto-merge into DOB-anchored CH people — a name+provider match
surfaces as a review Task (Phase 4). Accepting paid, rate-limited, async scraping
and a duplicate-until-reviewed cost, we fill the gap Companies House structurally
cannot see.

## Amendment (2026-07-02) — acquisition via `phantombuster-lib`

Phase-3 was validated live: the store **LinkedIn Company Employees Export**
(`spreadsheetUrl` → employees) works end-to-end, but the path is high-friction —
a public **Google-Sheet feed** (no API upload; Drive *file* URLs are rejected),
per-phantom UI session-connect, and a store **Company URL Finder** whose
name→URL match is badly fuzzy (`Scarborough Hall → rbrecycling`). In parallel,
[`mooperd/phantombuster-lib`](https://github.com/mooperd/phantombuster-lib)
independently solved the same problem more cleanly. We pivot the *mechanism* to
depend on it.

**What changes:**

1. **Acquisition = `phantombuster-lib`** (git dependency). A **custom Puppeteer
   resolver phantom** searches LinkedIn companies **with the UK-HQ geo facet**
   (`companyHqGeo` = United Kingdom) keyed on the **CQC `brandName`** (the trading
   brand LinkedIn actually lists, not the legal name), and scrapes the About page
   for the numeric **`companyId`** + industry/HQ/website — verification built into
   the search, not bolted on. Then LinkedIn **Search Export**
   (`currentCompany=["<companyId>"]`) yields the people; results come from
   `containers/fetch-result-object`.
2. **No spreadsheet/Drive feed, no UI session-connect.** Custom phantoms take
   input inline via the `argument` JSON; the `li_at` session cookie is injected
   via the argument (from a per-user secret or a borrowed identity).
3. **`cqc-companies` becomes the consumer.** The lib *acquires*; we correlate its
   result rows into `Person`/`Role` (ADR 0014) with the source hierarchy
   (ADR 0013) and suppression/erasure (ADR 0017). The row→identity mapping
   (name / `job` / `profileUrl`) carries over from our `parse_profile`.
4. **Schema:** add **`Provider.linkedin_company_id`** (the numeric id the lib
   resolves); **`PhantomRun` demoted to optional audit** (the lib is stateless —
   the container id is the job handle); keep `User`/`secrets_box` for the per-user
   `li_at`. Adopt the lib's **`cqc` Syndication client** for
   `brandName`/`brandId`/`companiesHouseNumber` (the resolver's search + verify
   signals) — our bulk-CSV `Provider` doesn't store `brandName`.
5. **Superseded and retired:** our stdlib `phantombuster.py`, the store Company
   Employees Export + `spreadsheetUrl` path, and the store URL-Finder. The API
   shapes + field names we verified are preserved in
   [`docs/spikes/phantombuster-api.md`](../spikes/phantombuster-api.md).

**Why:** eliminates the feed friction, solves trading-name (via `brandName`) and
geography (via the UK-HQ facet) verification natively, and avoids maintaining a
duplicate acquisition stack. The `PhantomRun`/store-phantom design in the
original Decision below is the historical record; §1–§5 here override its
*mechanism*. The implementation plan is
[`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md).

## Context

[ADR 0013](0013-companies-house-source.md) makes Companies House the first
identification source and already fixes the **source hierarchy** (manual >
Companies House > LinkedIn; LinkedIn is authoritative only for people CH cannot
see, and never overrides a CH director fact). [ADR 0014](0014-person-role-correlation-model.md)
fixes the **identity model** (`Person` ↔ `Role`, correlation anchored on the
partial DOB CH gives). What neither covers is *how LinkedIn data actually gets
in* — the Phase 3 piece the [product vision](../product-vision.md) calls the
"phantom-run runtime model (TBD)".

Three facts shape the decision:

- **CH only sees legally-registered directors/PSCs** — roughly 31% of providers'
  influential people. The rest (operations directors, registered managers,
  heads of care who aren't statutory officers) are reachable only via LinkedIn.
- **There is no usable first-party LinkedIn data API** for this. The realistic
  options are browser-automation scrapers; the vision already committed to
  [Phantombuster](https://phantombuster.com/) for *both* identification and
  later outreach, run under **each team-member's own LinkedIn session**
  ([ADR 0012](0012-crm-person-interaction-user-model.md) `User`).
- **Phantombuster runs are asynchronous and metered.** You launch a phantom
  (container), it runs for seconds-to-minutes, then you fetch its output; each
  run spends account credits. This is unlike the synchronous, free Companies
  House client — it needs a runtime model, not just a function call.

LinkedIn profiles carry **name, headline/title, current company, profile URL —
but no date of birth.** That single gap is the crux: ADR 0014's correlation key
is DOB-anchored, so LinkedIn data cannot merge into CH people the same way the
officer and PSC endpoints merge into each other.

## Decision

1. **Phantombuster identification phantoms are the LinkedIn ingestion mechanism.**
   Three are in scope as *identification* (read-only scrape) phantoms, feeding
   `Person`/`Role`:
   - **Sales Navigator Search Export** — bulk discovery of people by
     company/role filters.
   - **Company People Scraper** — everyone listed at a target provider's
     LinkedIn company page.
   - **Profile Data** — enrich one profile (headline, current role, location).

   *Action* phantoms (Auto-Connect, Message Sender, InMail) are explicitly **out
   of scope** — they are outreach, owned by the Phase 5 channel ADR (0020) and
   recorded as `Interaction`s, never as identification.

2. **A persisted `PhantomRun` entity models the async runtime.** Additive per
   [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md):
   `id`, `phantom` (which identification phantom), `user_id` FK (whose LinkedIn
   session + Phantombuster key it ran under), `provider_id` FK nullable (the
   target org, for company-scoped phantoms), `input` (JSON — the launch args),
   `status` (`queued | launched | running | finished | failed`), `launched_at`,
   `finished_at`, `output_ref` (where the fetched result is stored),
   `credits_spent`, `error`. The lifecycle is **launch → store container id →
   poll → on finish fetch output → map to `Person`/`Role`**; a `PhantomRun` is
   the durable record of one scrape, so a crash resumes from `status` and reruns
   are idempotent on (phantom, user, provider, input).

3. **Output maps to `Person` + `Role` via ADR 0014's model, source-namespaced.**
   Each scraped individual find-or-creates a `Person`; the affiliation is a
   `Role` with `source = phantombuster:<phantom>` (e.g.
   `phantombuster:company-people-scraper`), `confidence = low`, `role_type`
   carrying the scraped seniority/title bucket (exact taxonomy an implementation
   detail). Corporate/company results are discarded — `Person` is contactable
   humans only (ADR 0014 §5).

   *Unlike CQC/CH ([ADR 0015](0015-data-freshness-strategy.md)), LinkedIn
   ingestion writes `Person`/`Role` **directly**, with no change-event file. The
   asymmetry is deliberate: a re-scrape overwrites the current profile rather than
   accumulating an external diff, so there is nothing to event-source — the
   `PhantomRun` row is the audit trail (what ran, when, against whom, at what
   credit cost).*

4. **`Person.linkedin_url` (new, additive) is the LinkedIn identity + dedup key.**
   A nullable, indexed `String`. LinkedIn-sourced people correlate **by exact
   `linkedin_url`** when present (the same profile re-scraped is the same
   `Person`), falling back to ADR 0014's no-DOB path (exact normalized name among
   DOB-less people) when a run yields a name but no stable URL.

5. **No auto-merge into DOB-anchored CH people; matches become review Tasks.**
   Since LinkedIn carries no DOB, a scraped person who is *also* a CH director
   stays a **separate, low-confidence `Person`** (consistent with ADR 0013 §3:
   "a separate, lower-confidence observation flagged for review"). A
   **provider-scoped name match** between a LinkedIn `Person` and a CH `Person`
   is *suggested*, not applied — surfaced as a `merge_person` review Task once
   the Task system exists (Phase 4, ADR 0019). Until then
   the duplicate is tolerated and flagged by `match_confidence='low'`.

6. **Credit accounting + per-user quota gate every launch** (resolves the
   "Phantombuster cost budgeting" open question, product-vision Phase 3). Each
   `User` has a credit quota; a `PhantomRun` records `credits_spent`; a launch
   that would exceed the user's remaining quota is **blocked before it runs** and
   surfaced as a Task rather than silently overspending. Hard per-user caps, not
   a shared pool, so one user can't exhaust the team's credits.

7. **Deferred, by design, to their own ADRs:** GDPR controller posture (we become
   a UK data controller on the first scrape) → ADR 0017; LinkedIn
   account-hygiene / warming rate limits → ADR 0018; outreach action phantoms +
   channel abstraction → ADR 0019/0020. This ADR is *acquisition mechanism only*.

## Alternatives considered

- **A direct/official LinkedIn API** — rejected: none exposes the people-search
  and profile data this needs; the compliant API surface is for ad/marketing
  partners, not contact discovery.
- **A different scraping tool** (browser-extension scrapers, Bright Data,
  PhantomBuster competitors) — not rejected on merit so much as deferred: the
  vision committed to Phantombuster for identification *and* outreach so one
  integration serves both phases. If Phantombuster is banned or priced out, the
  `PhantomRun` entity is deliberately phantom-agnostic enough to repoint (see
  walk-back).
- **Force LinkedIn people into the CH `Person` by name+provider (auto-merge)** —
  rejected: name+company is a weak key (common names, people who moved orgs);
  auto-merging weak LinkedIn signal into authoritative CH identities would create
  false-confident contacts. Review-Task-gated merge keeps identity honest.
- **Add a synthetic/lower-precision DOB to fit ADR 0014's key** — rejected:
  fabricating a correlation anchor corrupts the very signal 0014 relies on. The
  honest answer is "no DOB → low confidence → don't auto-merge".
- **Scrape into a separate `LinkedInProfile` table, not `Person`/`Role`** —
  rejected: the CRM wants *one* contact model; ADR 0014's `Role.source`
  namespacing already accommodates LinkedIn cleanly, so a parallel table would
  just need reconciling back.

## Consequences

- **Positive:** the non-director majority of decision-makers becomes reachable;
  identification and (later) outreach share one Phantombuster integration and one
  per-user credential model; `PhantomRun` gives a durable, resumable, auditable
  record of every scrape and its credit cost.
- **Positive:** composes cleanly with shipped work — `Role.source` already
  reserves `phantombuster:<phantom>` (ADR 0014); only two additive bits of schema
  (`Person.linkedin_url`, `PhantomRun`) are new.
- **Cost — duplicate people until reviewed:** a LinkedIn-and-CH person is two
  `Person` rows until a merge Task resolves them. Accepted: honest low-confidence
  duplication beats false-confident merges, and the Task system (Phase 4) is the
  intended resolver.
- **Cost — async + metered + fragile:** scrapes are slow, cost credits, and break
  when LinkedIn changes its DOM or rate-limits. The runtime must treat failure as
  normal (status + retry, not abort), and credit quotas must gate launches.
- **Cost — legal exposure:** the first real scrape makes us a data controller.
  This ADR does **not** discharge that — [ADR 0017](0017-gdpr-controller-posture.md)
  must land before the first
  live identification run, not after.

## Walk-back options

- **If Phantombuster is banned / shuts down / is priced out** — `PhantomRun` is
  modelled around *kind/input/output/credits*, not Phantombuster specifics, so a
  different scraper becomes a new `phantom` value + adapter; `Person`/`Role` and
  `linkedin_url` are unaffected. If LinkedIn turns hostile to *all* scraping,
  demote LinkedIn from identification source to manual-only and lean on
  Companies House + manual entry (the vision's stated trigger to re-litigate).
- **If low-confidence duplicates pile up** before the Task system exists — add a
  one-off heuristic merge pass (name + provider + same town), still writing
  `match_confidence` so a wrong merge is a recoverable data fix, not a schema
  change (mirrors ADR 0014's walk-back).
- **If per-run scraping is too slow/costly at ~37k providers** — scope
  identification to a prioritised provider subset (by size/rating/region) rather
  than the whole directory; the `provider_id` on `PhantomRun` already makes
  targeted runs first-class.

## Links

- [ADR 0013](0013-companies-house-source.md) — source hierarchy (LinkedIn is the
  second source, never overrides CH director facts); this ADR is its §1
  "second source" made concrete.
- [ADR 0014](0014-person-role-correlation-model.md) — `Person`/`Role` +
  correlation; `Role.source = phantombuster:<phantom>` and the no-DOB
  low-confidence path are reused here.
- [ADR 0012](0012-crm-person-interaction-user-model.md) — `User` holds the
  per-user `linkedin_session_cookie` + `phantombuster_api_key` a `PhantomRun`
  runs under.
- [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) — additive schema path
  for `Person.linkedin_url` + `PhantomRun`.
- [`product-vision.md`](../product-vision.md) — Phase 3; identification vs action
  phantoms; the credit-budgeting and account-warming open questions (the latter
  deferred to ADR 0018).
- **Deferred siblings:** [ADR 0017](0017-gdpr-controller-posture.md) (GDPR controller posture), ADR 0018 (LinkedIn
  account hygiene / warming), ADR 0019 (Task entity — the merge/review surface),
  ADR 0020 (outreach channels — action phantoms).
