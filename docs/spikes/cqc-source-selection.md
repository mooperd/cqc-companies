# Spike — Which CQC data source should we automate ingest against?

**Status:** Resolved (2026-05-19) — **bulk monthly downloads.** Three predictable URLs at `cqc.org.uk/sites/default/files/<YYYY>-<MM>/...` cover everything the schema needs, no API key required. The Azure-fronted syndication API stays in the contingency drawer.

## Question

**What's the right data source to automate ingest against — the CQC public syndication API or the bulk monthly downloads CQC publishes — given we currently maintain two CSVs (`output.csv`, `Locations.csv`) by hand?**

Answer: **bulk monthly downloads**, with the API kept available as a backup path if CQC ever stops publishing the bulk files.

## Why this was in question

The repo's current source-of-truth is two large CSVs committed to the repo ([ADR 0007](../adr/0007-csvs-checked-into-repo.md)) and refreshed by hand. The walk-back option there names this exact pivot: *"If CSV refreshes become frequent, move them to object storage; have the importer fetch by URL with a checksum check."*

Three plausible automation sources existed:

1. **The Azure-fronted syndication API** (`api.service.cqc.org.uk/public/v1/`) — programmatic, free key, rate-limited.
2. **The published monthly bulk downloads** — a CSV and two `.ods` files at predictable URLs under `cqc.org.uk/sites/default/files/<YYYY>-<MM>/`.
3. **Scraping** the `/search/all` UI page (was the initial framing) — fragile, ruled out almost immediately.

The middle option turned out to be the easiest *and* the most complete.

## Findings

(Terse evidence, ordered chronologically. The oracle agent's full field-by-field comparison against the API schemas is in this PR's review thread, kept for the API contingency path.)

### 2026-05-19 — API path investigated first

- API exists at `api.service.cqc.org.uk/public/v1/`; endpoints `/providers`, `/locations`, `/changes/{provider,location}`. Auth via `Ocp-Apim-Subscription-Key` header (Azure APIM).
- List endpoints are sparse (IDs + name + postcode only) — full sync is N+1 detail calls, ≈125k requests for an initial backfill against the current ~88k locations + ~33k providers.
- Documented rate limit (legacy endpoint) was 2000/min with a `partnerCode`; the new APIM-fronted endpoint's exact limit is gated behind the portal login.
- Field coverage from openans-mirrored schemas: ~70% direct (renamed), ~20% nested (5 sub-ratings under `currentRatings.overall.keyQuestionRatings[]`, `service_types` under `gacServiceTypes[].name`), ~10% absent or derived.
- `service_users_supported` is *not* in the API. `email_address` is explicitly excluded by CQC.
- Local code probe: `service_users_supported` is display-only in templates; `email_address` is set to `''` by every importer and the source CSVs have no email column. Both gaps collapse to non-blockers under actual usage.

### 2026-05-19 — Bulk monthly downloads found

CQC publishes three monthly files at predictable URLs under `https://www.cqc.org.uk/sites/default/files/<YYYY>-<MM>/`:

| File | URL pattern | Size | Cadence | Equivalent to |
|---|---|---|---|---|
| `<DD>_<Month>_<YYYY>_CQC_directory.csv` | e.g. `2026-05/13_May_2026_CQC_directory.csv` | ~19 MB CSV | ~monthly | current `output.csv` (modernised, simpler) |
| `<DD>_<Month>_<YYYY>_HSCA_Active_Locations.ods` | e.g. `2026-05/05_May_2026_HSCA_Active_Locations.ods` | ~24 MB ODS | ~monthly | current `Locations.csv` plus a lot more |
| `<DD>_<Month>_<YYYY>_Latest_ratings.ods` | e.g. `2026-05/05_May_2026_Latest_ratings.ods` | ~26 MB ODS | ~monthly | the 5 sub-ratings (Safe/Effective/Caring/Responsive/Well-led) in long-format |

The CSV directory file has 15 columns (renamed and address-collapsed vs. our current `output.csv`):

| New CSV column | Maps to our current schema |
|---|---|
| `Name` | `Facility.name` |
| `Also known as` | `Facility.also_known_as` |
| `Address` (single, quoted multi-line string) | requires split → `address_1`, `address_2`, `town_city`, `county` |
| `Postcode` | `Facility.postcode` |
| `Phone number` | `Facility.phone_number` |
| `Service's website (if available)` | `Facility.website` |
| `Service types` | `Facility.service_types` |
| `Date of latest check` | `Facility.report_publication_date` |
| `Specialisms/services` | `Facility.specialisms_services` |
| `Provider name` | `Provider.name` |
| `Local authority` | `Facility.local_authority` |
| `Region` | `Facility.region` |
| `Location URL` | `Facility.url` |
| `CQC Location ID (for office use only)` | `Facility.cqc_location_id` |
| `CQC Provider ID (for office use only)` | `Provider.cqc_provider_id` |

HSCA Active Locations (.ods, 122 cols across `HSCA_Active_Locations` + `Dual_Registration_Locations` sheets) covers everything in our `Locations.csv` and more:

- All location identity, address, manager, beds, dormant flag — direct columns.
- Latest **overall** rating + publication date (cols 14-15). Sub-ratings (Safe / Effective / etc.) are **not** in this file — they live in `Latest_ratings.ods`.
- Service types as **one-hot Y/N columns 76-108** (rather than the current comma-separated string).
- Service user bands (the `service_users_supported` equivalent) as **one-hot Y/N columns 109-120**.
- Regulated activities as one-hot Y/N columns 62-75.
- Brand info, Companies House number, lat/lon, NHS region, CCG, parliamentary constituency — all present.

Latest ratings (.ods, 28 cols on `Locations` sheet, 21 on `Providers` sheet): one row per (location/provider, service-population-group, **domain**) where `Domain` ∈ {Safe, Effective, Caring, Responsive, Well-led}. To populate our current scalar sub-rating columns, we pivot wide on `Domain`.

### Net field coverage

Combining the three bulk files, **every column the current schema needs is recoverable.** The two original "gap" fields the API analysis flagged are both present in HSCA:

- `service_users_supported`: HSCA cols 109-120 → flatten Y/N to comma-separated string (matching the current shape).
- `email_address`: still absent everywhere (CQC doesn't publish emails). Continues to be set to `''` — no regression.

## Decision matrix

| Outcome | Action |
|---|---|
| **Bulk URLs cover all needed columns** ← *what landed* | **Switch to bulk-download ingest.** New ADR ("Bulk monthly downloads as ingest source") amends [ADR 0007](../adr/0007-csvs-checked-into-repo.md) per its own walk-back clause. Importers updated to read the new column shape (CSV + ODS). API stays documented as contingency. |
| Bulk URLs miss a load-bearing column | Switch with hybrid — bulk for the bulk, API or scraping for the gap. |
| CQC stops publishing the bulk downloads | Fall back to the API path. Keys already provisioned in `.env` (`CQC_PRIMARY_KEY` / `CQC_SECONDARY_KEY`). |

## Outcome

**Bulk monthly downloads chosen.** Three predictable URLs, no auth, no rate limit, more complete than the API. The next steps belong in a successor plan and ADR, not this spike:

1. **ADR** amending [ADR 0007](../adr/0007-csvs-checked-into-repo.md): the walk-back triggers (frequent refresh, automation desire) are now active; the new ingest source is the bulk monthly downloads.
2. **Plan** (`docs/plans/cqc-bulk-ingest.md`) scoping: URL discovery (scrape `cqc.org.uk/about-us/transparency/using-cqc-data` for the current month's links), download + checksum, ODS-to-DataFrame loader using a streaming parser (NOT the default odfpy + pandas path — see "Lesson learned" below), schema mapping for the three files, GH Actions cron, PR-against-CSV workflow.
3. **API kept as contingency.** Keys present at `.env:CQC_PRIMARY_KEY` / `CQC_SECONDARY_KEY`. The auth flow is documented in CQC's developer portal at <https://api-portal.service.cqc.org.uk/> — we don't redistribute it locally because the source document we had wasn't clearly licensed.

### Lesson learned (worth preserving for the plan)

Reading the 24-26 MB `.ods` files via `odfpy + pandas.read_excel(engine='odf')` consumed **>3 GB of RAM** during parsing and didn't complete within a minute. The fix: stream the embedded `content.xml` directly with `ElementTree.iterparse`, since `.ods` is just a ZIP archive containing OpenDocument XML. A 30-line streaming parser produces the same columns in < 1 second using a few MB of RAM. Reference implementation lived at `/tmp/probe-ods-headers.py` during this spike; the plan should commit a productionised version under the importer code.

## References

- [ADR 0007 — CSVs checked into the repo](../adr/0007-csvs-checked-into-repo.md) — the walk-back this spike's outcome activates.
- [ADR 0005 — Two-stage CSV ingest](../adr/0005-two-stage-csv-ingest.md) — the import pipeline reshaped by the outcome.
- [Plan — Initial debt and questions](../plans/initial-debt-and-questions.md) — peer plan; the bulk-ingest follow-up is a successor plan, not a workstream here.
- [CQC — Using CQC data](https://www.cqc.org.uk/about-us/transparency/using-cqc-data) — official index of the bulk files (also the page we'd scrape for URL discovery).
- [CQC developer portal](https://api-portal.service.cqc.org.uk/) — API contingency path.
- [openans-mirrored JSON schemas](https://openans.github.io/cqc-syndication-api/) — preserved for the API contingency path; the most complete public API spec.
- [Birdie tap-cqc-org-uk](https://github.com/birdiecare/tap-cqc-org-uk) — real-world Singer tap consumer of the API, for the contingency reference.
