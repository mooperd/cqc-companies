# ADR 0017 — GDPR controller posture for scraped contact data

**Status:** Accepted (2026-07-04). *Records the posture + the mechanisms that make
it operable. The load-bearing code piece — durable erasure (§5) — **has shipped**
(PWS5: `suppression.py` + the on-ingest suppression check; see
[the plan](../plans/linkedin-ingestion.md)). The legal sign-off it depends on (a
reviewed Legitimate Interest Assessment, the privacy-notice wording, ICO
registration) is **not** something this ADR or the code can certify — it remains an
external prerequisite that must be done by someone qualified **before the first
live scrape**.*

<!--
Not legal advice. This is the engineering/product decision record for how the
system supports UK GDPR obligations; it points at where a lawyer's judgement is
required rather than substituting for it.
-->

**TL;DR.** The moment we scrape one real LinkedIn profile we are a **UK data
controller** ([product-vision.md](../product-vision.md) §Constraints,
[ADR 0016](0016-linkedin-phantombuster-ingestion.md)). We chose a **legitimate-
interest basis** for holding business-context contact data on decision-makers at
CQC-regulated orgs, made operable by four mechanisms: **data minimisation**
(business-context fields only, no special-category data), **provenance + an
acquisition timestamp** on every record, **time-boxed retention** (purge contacts
with no live relationship), and — the load-bearing one — **durable erasure**: an
objection/erasure deletes the person AND writes a hashed **suppression tombstone**
that blocks re-ingestion, so a later scrape can never resurrect them. Accepting
the operational overhead (a retention job, a suppression check on every ingest,
ICO registration, DSR handling, and a now-urgent app-auth story), we get a
defensible posture where erasure actually sticks.

## Context

The CRM scrapes LinkedIn (ADR 0016) and enriches from Companies House
(ADR 0013/0014) to build `Person`/`Role` records on people we want to contact.
This is personal data of identifiable living individuals → **UK GDPR applies**,
and we are the **controller** the instant the first real profile is stored, even
into a private dev database. product-vision.md already committed the direction
("legitimate-interest basis … explicit retention + erasure mechanism … cannot be
deferred past the first real enrichment run"); ADR 0016 deferred the detail here.

Two facts shape the mechanisms:

- **We collect indirectly** (scraping/Companies House, not from the person), so
  the Article 14 transparency duty and the right to object/erase are the sharp
  edges — and a person who says "delete me" must *stay* deleted despite our
  pipeline re-scraping their company next month.
- **Most of the data is genuinely business-context** (name, employer, job title,
  LinkedIn URL, directorships from a public register). That is what makes a
  legitimate-interest basis plausible — and what we must not exceed by pulling in
  personal/sensitive fields.

## Decision

1. **Lawful basis: legitimate interest** (UK GDPR Art 6(1)(f)) for B2B prospect
   data, documented in a **Legitimate Interest Assessment (LIA)** — purpose
   (relationship-building outreach to decision-makers at CQC orgs), necessity
   (targeted, not bulk marketing), and a balancing test (business-context data
   only, low expectation of harm, easy objection/opt-out). **Not consent** —
   impractical before first contact. *The LIA must be written and reviewed by a
   qualified person; this ADR commits to the basis, not its sufficiency.*

2. **Data minimisation — a closed field list.** We store only business-context
   identity + affiliation already modelled in `Person`/`Role`: name, parsed
   surname/forenames, `linkedin_url`, scraped headline/employer, and the partial
   DOB Companies House publishes for directors (used only for correlation,
   ADR 0014). **No special-category data** (health, beliefs, etc.), no personal
   contact details beyond the professional context, no inferred sensitive
   attributes. Phantoms are configured to fetch only these fields.

3. **Provenance + acquisition timestamp on every record.** Each `Role` already
   carries `source`; we add an **acquisition timestamp** to `Person` (when we
   first stored them) so retention and the Article 14 "where did you get my data"
   response are answerable. `PhantomRun` (ADR 0016) is the per-scrape audit trail.

4. **Time-boxed retention.** Personal data is not kept indefinitely: a periodic
   job purges contacts with **no live business relationship** — no `Interaction`
   and no role change within the retention window. *Proposed default: 24 months,
   reviewed annually* (a knob — see Open knobs). Companies House director data
   (public register) may warrant a longer window than LinkedIn-scraped data.

5. **Durable erasure + objection — the load-bearing mechanism.** A data subject's
   erasure/objection request:
   - **deletes** their `Person` + `Role` personal data, and
   - writes a **`SuppressedContact` tombstone** keyed on a **hash** of the stable
     identifier (the `linkedin_url`, and/or normalised name) with a reason + date
     — **not** the profile itself, so the suppression list is not a backdoor
     personal-data store.
   - **Ingestion checks the suppression list *before* creating any `Person`** and
     skips a match, so a later scrape of the same company never resurrects an
     erased person. Without this, erasure is theatre.

6. **Transparency (Art 14).** A public **privacy notice** describes the
   processing, basis, retention, and rights; because collection is indirect, the
   notice is surfaced to the data subject **at first outreach contact** (the
   outreach message links it and offers a one-step objection). Deferred to the
   outreach phase but owned here.

7. **Data-subject rights supported:** access, rectification, erasure, objection.
   Erasure + objection are automated via §5; access/rectification are handled
   manually at first (low volume), with a documented runbook.

8. **Controller operational obligations** (not code): register with the ICO and
   pay the data-protection fee; keep a Record of Processing Activities (ROPA);
   appoint a contact point for DSRs. Flagged as launch tasks for the live gate.

9. **Security raises app-auth priority.** Holding real personal data makes the
   deferred app authentication ([ADR 0011](0011-defer-authentication.md)) more
   urgent — real data behind no login is its own exposure. Per-user credentials
   are already encrypted at rest (ADR 0016 `secrets_box`).

## Alternatives considered

- **Consent basis** — rejected: you cannot obtain consent before the first
  contact, and prospecting on consent-only doesn't scale; legitimate interest is
  the standard B2B-prospecting basis (subject to the LIA holding up).
- **Hard-delete with no suppression tombstone** — rejected: the pipeline
  re-scrapes companies monthly, so a deleted person reappears at the next run.
  Erasure must be durable to be real.
- **Store the full profile in the suppression list** (to match on re-scrape) —
  rejected: that makes the suppression list itself a personal-data store, the
  opposite of erasure. Hash the identifier; match on the hash.
- **Don't scrape LinkedIn at all; Companies House (public register) + manual
  only** — the walk-back option if the legitimate-interest posture can't be made
  defensible, not the default (it abandons the ~69% of non-director contacts that
  motivate ADR 0016).
- **Defer GDPR until outreach/publishing** — rejected (this ADR exists): the
  controller obligation attaches at the first *scrape into our DB*, not at
  publishing.

## Consequences

- **Positive:** a defensible, minimised, time-boxed posture; erasure that
  actually sticks against re-scraping; a provenance trail for DSR responses; the
  posture is recorded before — not after — real data lands.
- **Cost — a suppression check on every ingest:** `sync_profiles` (ADR 0016) and
  the CH enrichers must consult `SuppressedContact` before creating a `Person`.
  Cheap (indexed hash lookup), but now mandatory on the ingest path.
- **Cost — operational:** a retention job, ICO registration + fee, a ROPA, a
  privacy notice, and a DSR runbook are real recurring work, and app auth becomes
  a prerequisite rather than a deferral.
- **Schema (additive,** [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md)**):**
  `SuppressedContact` (hashed identifier + reason + date); a `Person` acquisition
  timestamp (and an `erased_at`/tombstone link as needed).

## Walk-back options

- **If the LIA doesn't hold up** under review — narrow to consent-after-first-
  contact (only enrich/contact those who opt in), or restrict scraping to
  Companies House public-register directors, which carry a stronger basis.
- **If retention/erasure ops prove too heavy** — shrink scope to CH-only contacts
  (public register, lower risk) until the operational story matures.
- **If the suppression hash causes false suppressions** (two people, same name,
  no URL) — key strictly on `linkedin_url` where present and fall back to a
  manual review queue for name-only matches, rather than over-suppressing.

## Open knobs (decisions to confirm)

- **Retention window** (proposed 24 months of no relationship) and whether CH
  director data gets a longer one.
- **Suppression key** — `linkedin_url` only, or also name+provider (trades
  false-suppression risk against missed re-suppression).
- **ICO registration / DPO** ownership and timing.

## Links

- [ADR 0016](0016-linkedin-phantombuster-ingestion.md) — the LinkedIn scraping
  this governs; its ingest path gains the suppression check.
- [ADR 0014](0014-person-role-correlation-model.md) — `Person`/`Role`, which gain
  the acquisition timestamp + erasure/suppression.
- [ADR 0013](0013-companies-house-source.md) — CH (public-register) data, a
  stronger-basis source than LinkedIn.
- [ADR 0012](0012-crm-person-interaction-user-model.md) — `Interaction` (the
  "live relationship" signal retention keys off) + `User`.
- [ADR 0011](0011-defer-authentication.md) — app auth, made more urgent by holding
  real personal data.
- [`product-vision.md`](../product-vision.md) — §Constraints (legitimate interest)
  and the retention/erasure open question (Phase 3).
- [`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md) — where the
  durable-erasure mechanism becomes a workstream gating the first live scrape.
