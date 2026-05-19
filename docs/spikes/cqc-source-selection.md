# Spike — Can the CQC syndication API replace the bulk CSV exports as our data source?

**Status:** Open. Time-box: ≤ 1 day of live-probe work; abort and write up a "hybrid" outcome if Q2 (rate limit) blows past the GH-Actions 6-hour ceiling for a full backfill.

<!--
At resolution, update Status to one of:
  Resolved (YYYY-MM-DD) — YES; <one-line outcome>
  Resolved (YYYY-MM-DD) — NO; <one-line outcome>
  Resolved (YYYY-MM-DD) — PARTIAL; <one-line outcome>
And fill in the Outcome section below.
-->

## Question

**Does the CQC public syndication API (`https://api.service.cqc.org.uk/public/v1/`) expose enough of the data we currently store to replace the two bulk CSV exports — `output.csv` (providers) and `Locations.csv` (locations) — as our primary ingest source?**

If yes, this spike unlocks an ADR + plan to automate ingest via API (cron / GitHub Actions), eliminating the manual CSV refresh tracked under [ADR 0007](../adr/0007-csvs-checked-into-repo.md).

## Why this is in question

The repo's current source-of-truth is two large CSVs committed to the repo ([ADR 0007](../adr/0007-csvs-checked-into-repo.md)) and refreshed by hand. The walk-back option there names this exact pivot: *"If CSV refreshes become frequent, move them to object storage; have the importer fetch by URL with a checksum check."* This spike answers the precondition for that walk-back — **before** we touch infrastructure, can we even get the data over the wire?

The CQC syndication API is the obvious source. It's documented, free, and CQC's own preferred channel for programmatic consumers. But: it requires registration, it's rate-limited, the list endpoints are sparse (IDs only — full sync is N+1 detail calls), and at least *some* fields the current schema stores are CSV-only enrichments rather than API-surfaced.

## Approach

### Q1. Field coverage — does the API expose the columns we depend on?

**Done — desk-research via oracle agent on the CQC developer portal + openans-mirrored JSON schemas + MuleSoft examples + Birdie's open-source Singer tap + the `cqcr` R package.** Findings in §Findings below.

### Q2. Rate limit and quota — does a full backfill (≈125k detail calls) fit in a GitHub Actions job?

**Not yet started.** Requires a registered API key. Concrete sub-steps:

- Register at <https://api-portal.service.cqc.org.uk/signup>.
- Call `GET /locations?perPage=1000&page=1` once; capture `Retry-After`, `RateLimit-*` response headers, confirm `perPage=1000` is honoured.
- Make 60 rapid-fire calls in a one-minute window; observe whether throttling kicks in.
- Read the developer-portal "Products" page (gated behind login) for the documented per-minute limit on the Syndication product.

### Q3. Live field-diff — turn the oracle's "UNKNOWN" verdicts into definitive YES/NO

**Not yet started.** Requires a registered API key. Pick 10 known locations from the current `Locations.csv`, GET each via `/locations/{id}`, diff the JSON against our SQLAlchemy field list. Specifically nail down:

- `uprn` — confirmed present per CQC's own examples on the new endpoint, but absent from the openans-mirrored legacy schema.
- `dormant` — likely derivable from `registrationStatus`, but not a direct field.
- `inspectionDirectorate` — believed to map to our `primary_inspection_category`, exact equivalence unconfirmed.
- `email_address` — CQC's data page says emails are not in the dataset. **Confirmed in our context as a non-blocker** — see §Pre-spike findings.

## Decision matrix

| Outcome | Means | Action |
|---|---|---|
| API covers everything we use, rate limit accommodates a full backfill in ≤ 6h | **Switch fully** | New ADR ("CQC API as primary ingest source") supersedes [ADR 0007](../adr/0007-csvs-checked-into-repo.md). Plan workstreams: API client, persistence of `lastSync` timestamps, incremental via `/changes`, GH Actions cron, PR-against-CSV workflow if we still want the CSVs as backup. |
| API covers everything we use, but rate limit means full backfill > 6h | **Switch with cron split** | Same as above, but the backfill happens in chunks (resumable via persisted `page` cursor) or runs locally / on a long-running runner. Incremental sync stays in GH Actions. |
| API misses a *display-only* field (e.g. `service_users_supported`) | **Switch with hybrid** | API for the core ingest; supplementary CSV (the "Care directory with filters" download) for the gap column, refreshed on a longer cadence. Plan calls out the hybrid explicitly. |
| API misses a *load-bearing* field used in filters/queries | **Don't switch; revisit** | Spike resolves NO. ADR 0007 stays accepted. We may revisit if/when CQC adds the field. |

## Pre-spike findings

(Filled in as the spike runs. Terse — these are evidence, not narrative. The oracle agent's full report is the primary source; this section captures only what's load-bearing for the decision.)

- **2026-05-19 — Oracle desk research:** API has `/providers`, `/locations`, `/changes/{provider,location}` endpoints. Auth via `Ocp-Apim-Subscription-Key` header (Azure APIM). List endpoints are sparse (IDs + name + postcode only); detail requires N+1 calls. Documented rate limit on the legacy endpoint was 2000 req/min with a `partnerCode`; the new APIM-fronted endpoint's exact limit is *docs-silent* and gated behind the portal login. No documented daily quota.
- **2026-05-19 — Field coverage from openans schemas:** ~70% of our columns are present (mostly under different names — `cqc_location_id` → `locationId`, `phone_number` → `mainPhoneNumber`, etc.). ~20% are nested (5 sub-ratings under `currentRatings.overall.keyQuestionRatings[]`, `service_types` under `gacServiceTypes[].name`, `specialisms_services` under `specialisms[].name`). ~10% are absent or derived:
  - **Absent:** `email_address` (CQC explicitly excludes), `service_users_supported` (CSV-only).
  - **Derivable:** `url` (build from `https://www.cqc.org.uk/location/{locationId}`), `care_home_size_band` (compute from `numberOfBeds`), `location_length_service_band` (compute from `registrationDate`), `dormant` (likely from `registrationStatus`).
- **2026-05-19 — Local code probe:** `service_users_supported` is **display-only** in templates (`templates/index.html:193`, `templates/providers.html:285`) — no filter, no query. Acceptable temporary regression OR fill from the supplementary CSV. `email_address` is referenced in templates but **set to `''` by every importer** (`import_records.py:91`, `enrich_locations.py:68,100`) — net zero loss from the API gap.
- **Implication so far:** Both originally-flagged "gap" fields collapse to non-blockers under our actual usage. The only remaining open question is **rate-limit feasibility for the full backfill** (Q2).

## Outcome

(To be filled in at resolution, after Q2 and Q3 run against a live key.)

## References

- Oracle agent report — full field-by-field mapping and source links (in this PR's review thread; not committed to the repo because it's transient research, not load-bearing).
- [ADR 0007 — CSVs checked into the repo](../adr/0007-csvs-checked-into-repo.md) — the walk-back this spike unlocks.
- [ADR 0005 — Two-stage CSV ingest](../adr/0005-two-stage-csv-ingest.md) — the import pipeline this spike's outcome reshapes.
- [Plan — Initial debt and questions](../plans/initial-debt-and-questions.md) — peer plan; this spike, if it resolves YES, becomes a successor plan rather than a workstream on that one.
- [CQC developer portal](https://api-portal.service.cqc.org.uk/) — registration + product docs.
- [CQC — Using CQC data](https://www.cqc.org.uk/about-us/transparency/using-cqc-data) — official statement on data scope (incl. "no email addresses").
- [openans-mirrored JSON schemas](https://openans.github.io/cqc-syndication-api/) — the most complete public spec (legacy endpoint, but ≈95% accurate for the new one).
- [Birdie tap-cqc-org-uk](https://github.com/birdiecare/tap-cqc-org-uk) — real-world Singer tap consumer; a reference for handling the N+1 pattern in practice.
