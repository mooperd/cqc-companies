# Plan — client demo ahead of GDPR sign-off

**Status:** Active (started 2026-07-20).

Goal: put a **working, walkthrough-able CRM** in front of the client **before** the
client undertakes the (right-sized) GDPR steps — so they see value first, then file
the ICO registration + LIA + privacy notice ([ADR 0017](../adr/0017-gdpr-controller-posture.md)
amendment 2026-07-20). The demo runs entirely on **ungated data**; the legal gate
only blocks *new at-scale scraping* and *real outreach*, neither of which a demo
needs.

## The gate line (what the demo may and may not do)

| Demo may use (ungated) | Gated — post-sign-off only |
|---|---|
| Public **CQC** providers/facilities | **New** at-scale LinkedIn people scrapes |
| **Companies House** directors (public register, ~95k already in the box) | Real **outreach** to scraped individuals |
| **Resolver → LinkedIn company ids** (non-personal, [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md) PWS2) | |
| **Existing** (Exp 2 sample) or **synthetic** `Person`/`Role` rows | |

The pitch closes with "live LinkedIn people-enrichment switches on once you're ICO-registered."

## Environment

Deployed at the Hetzner box (`crm.darwinist.io`, behind Caddy basic-auth —
[ADR 0018](../adr/0018-hetzner-single-box-deploy.md)); DB already carries the
restored enriched dump (37k providers, ~95k people, ~156k roles). The resolver runs
**off-box** (residential IP; the box is LinkedIn-authwalled) and its company-id
change-sets replay onto the box ([linkedin-ingestion plan](linkedin-ingestion.md) §4).

## Workstreams

### DS1 — Scale the resolver (ungated, non-personal)
Broaden `Provider.linkedin_company_id` coverage across a good demo set via the
off-box resolve → change-set → apply pipeline. Needs light **pacing** (LinkedIn
throttles rapid fetches even from a residential IP). **Exit:** a visibly non-trivial
fraction of demo providers linked to their LinkedIn company id, applied on the box.

### DS2 — Provider detail UI audit
Confirm `/provider/<id>` presents the enriched decision-makers (CH directors +
roles, LinkedIn linkage) well for a live walkthrough; pick **2–3 strong demo
providers** (a resolved chain with CH directors). **Exit:** a named shortlist of
demo URLs that look good behind basic-auth.

### DS3 — (optional) Synthetic LinkedIn people seed
If the people-layer panel looks thin on real data, seed a **synthetic** LinkedIn
`Person`/`Role` set for a provider or two so the "LinkedIn decision-makers" view is
populated **without new scraping**. **Exit:** a demo provider whose people panel is
full, clearly labelled demo/synthetic.

## Progress (2026-07-20)

- **DS1 done** — resolver gained `--sleep` (pacing) + `--richest-first` (resolve
  decision-maker-rich providers first). Ran off-box against the local DB, emitted
  `data/changes/linkedin-resolver-2026-07-20.json` (102 resolutions), applied on
  the box → **103 providers resolved**. (A residential-IP DNS blip mid-run threw
  ~56/200 `error`s per batch; fault-tolerance kept the batch alive and those stay
  retryable. The full ~37k sweep is still an ongoing paced job.)
- **DS2 done** — demo shortlist (resolved + CH-director-rich, LinkedIn company link
  now clickable in `/provider/<id>`): **Marie Curie** (`/provider/3063`, 109
  directors), **Leonard Cheshire Disability** (`/provider/1533`, 146), **Royal
  Mencap Society** (`/provider/3371`, 106), **St John Ambulance** (`/provider/35558`,
  107), **Combat Stress**, **Sense**. All render cleanly.
- **Wrinkle noted:** `apply_pending` keys the ledger on *filename*, so a same-day
  re-emit of an already-applied file is skipped — clear its `applied_event_file`
  row to re-apply (the per-row guard keeps it idempotent). Normal operation emits a
  *new* dated file each day, so this only bites same-day dev iteration. A
  content-hash ledger would remove the wrinkle if it ever matters.

## Exit criteria

- [x] DS1 — resolver coverage broadened (103 resolved) + applied on the box.
- [x] DS2 — demo providers chosen; UI presents enrichment cleanly (clickable LinkedIn link).
- [~] DS3 — **decided: a real pre-production scrape** (client needs to see real
      LinkedIn people to approve this stage; [ADR 0017](../adr/0017-gdpr-controller-posture.md)
      amended 2026-07-20 with the pre-production carve-out + guardrails). Wired to
      run **on the box** (ADR 0019 — personal data authoritative there; Phantombuster
      owns the LinkedIn session, so the box IP is irrelevant), auth via the env PB
      key (injected client, no per-user encryption). **Blocked:** first run failed
      `exit 84` = **stale LinkedIn session** (the #1 gotcha) — and `pb_doctor` gave a
      **false green** (it checks the cookie *exists*, not *valid* — open follow-up
      #4, now evidenced). Clean failure: 0 people, no credits. **Unblock:** reconnect
      the LinkedIn session in Phantombuster (agent cookie **fingerprint must change**),
      then re-run `scrape_demo.py <provider_id>` on the box.
- [ ] A rehearsed walkthrough at `crm.darwinist.io` the client can be shown.

## References

- [ADR 0017](../adr/0017-gdpr-controller-posture.md) — the gate + the 2026-07-20 right-sizing/demo amendment.
- [ADR 0016](0016-linkedin-phantombuster-ingestion.md) — resolver (PWS2, non-personal) + people scrape (gated).
- [ADR 0018](../adr/0018-hetzner-single-box-deploy.md) / [ADR 0019](../adr/0019-scraped-data-lives-in-deployed-db.md) — the box + data home.
- [`linkedin-ingestion.md`](linkedin-ingestion.md) §4 — the off-box resolver operational model.
