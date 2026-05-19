# ADR 0007 — CQC source CSVs checked into the repository

**Status:** Accepted (2025-08-28). Amended 2026-05-19 — see Amendment below; refresh is no longer manual.

**TL;DR.** In the context of needing a reproducible "fresh DB" import for development, demos, and AKS rollouts, facing zero existing object storage infrastructure and CSVs that are public CQC data anyway, we chose to commit `output.csv` (≈30 MB) and `Locations.csv` (≈20 MB) directly to the git repository, accepting bloat in `git clone` and the absence of a refresh-from-source workflow.

## Amendment (2026-05-19)

The original §Decision still holds — the CQC data CSVs remain committed to the repo as the single source of truth that the importers consume. **What this amendment changes is the *refresh mechanism***: the original §Consequences described refresh as "a manual download + commit, which keeps human eyes on schema drift but slows down updates." That's no longer the design. Refresh is automated by a scheduled GitHub Actions job that polls CQC's published bulk URLs and opens a PR when the data changes; human eyes still see schema drift, but at PR-review time rather than at download time.

This amendment supersedes the original §Walk-back item *"If CSV refreshes become frequent — move them to object storage"* for the refresh-frequency trigger only. The repo-size trigger ("If the repo crosses ~500 MB") and the contributor-portability trigger ("`*.csv binary`" — already done) remain in force unchanged.

### Source (resolved 2026-05-19 — see [`docs/spikes/cqc-source-selection.md`](../spikes/cqc-source-selection.md))

The authoritative source is CQC's monthly bulk-download files published under `https://www.cqc.org.uk/sites/default/files/<YYYY>-<MM>/`:

| File | Replaces | Notes |
|---|---|---|
| `<DD>_<Month>_<YYYY>_CQC_directory.csv` (~19 MB) | `output.csv` | 15 cols; address fields collapsed into one quoted string |
| `<DD>_<Month>_<YYYY>_HSCA_Active_Locations.ods` (~24 MB) | `Locations.csv` + more | 122 cols; service types and user bands as one-hot Y/N columns |
| `<DD>_<Month>_<YYYY>_Latest_ratings.ods` (~26 MB) | the 5 sub-ratings | long format — one row per (location, Domain) where Domain ∈ {Safe, Effective, Caring, Responsive, Well-led}; needs pivot |

The day-of-month varies per file (e.g. May 2026: 13th for directory, 5th for HSCA + ratings), so the cron job discovers current URLs by scraping the CQC data index page (`https://www.cqc.org.uk/about-us/transparency/using-cqc-data`) rather than templating dates.

The Azure-fronted syndication API (`api.service.cqc.org.uk/public/v1/`) is **not** used. It's kept documented as a contingency path — auth flow at CQC's developer portal (<https://api-portal.service.cqc.org.uk/>), keys provisioned in each developer's local `.env` — for the day CQC stops publishing the bulk files.

### Refresh mechanism

1. **Schedule.** GitHub Actions cron on the upstream `mooperd/cqc-companies` repo. Daily, off-peak UTC. Daily polling matches the cheapest-detection model (see §Trigger below); CQC publishes monthly, so most days the job exits in seconds without doing any work.
2. **Trigger.** On each run, `HEAD` the three current download URLs (discovered from the CQC data index page) and compare `ETag` + `Last-Modified` against a state file committed to the repo (path TBD — see plan). If all three are unchanged since the last run, exit with no further action.
3. **Action when changed.** Download the changed file(s) → parse (CSV reader for the directory; streaming `ElementTree.iterparse` over `content.xml` for the `.ods` files — *not* `odfpy + pandas.read_excel`, which consumes >3 GB of RAM on the 26 MB ratings file; see the spike's "Lesson learned") → map to our schema's shape (one-hot Y/N → comma-separated string for service types and user bands; long-to-wide pivot for the 5 sub-ratings) → regenerate the committed CSVs in their current column shape.
4. **PR creation.** If the regenerated CSVs differ from what's on `main`, the job opens a PR against `main` with the refreshed CSVs and a brief generated body summarising row-count deltas. The existing PR-checks workflow (`smoke` + `actionlint`) runs against it.
5. **Auth.** The workflow uses the runner-injected `GITHUB_TOKEN` with explicitly-declared `contents: write` + `pull-requests: write` permissions. **No CQC API key is required for the bulk files** — they're public, unauthenticated downloads. The CQC key path stays gated behind the contingency.

### New walk-back triggers

The walk-back options in the original ADR remain in force. This amendment adds two more, tied to the new refresh mechanism:

- **If CQC stops publishing the bulk files** — discovery scrape returns nothing, or `HEAD` returns 404 for >2 consecutive runs. Fall back to the API; keys are already provisioned in `.env`. Open a successor ADR ("API as primary ingest source") rather than amending again.
- **If the regenerated CSVs diverge from current shape (CQC changes columns)** — the PR will reveal the diff. Block the merge; either update the importers to accept the new shape (separate PR) or freeze on the last good month until the change is understood. The cron job MUST NOT silently merge.

The original Context, Decision, Alternatives considered, Consequences, Walk-back, and Links sections below are retained for the historical record. Where they conflict with this amendment, this amendment wins.

## Context

The two CSVs are published by CQC at a stable public URL; they are not secret, and they are the single source of truth that the importer scripts ([ADR 0005](0005-two-stage-csv-ingest.md)) consume. For local development and for the deployed container's first import, the import step needs a copy of the CSV somewhere reachable.

The alternatives all add infrastructure: blob storage (S3, Azure Blob), a fetch-on-startup step (requires CQC's URL to be stable), or a separate "data" repo. None of these were already in place; the project is small enough that the bloat from committing a 50 MB pair of files is comparable to a few months of code churn.

There is also a pair of "head" copies (`Locations-head.csv`, `output-head.csv`) — first ~500 rows of each — used for fast local iteration.

## Decision

1. The full CSVs (`output.csv`, `Locations.csv`) and their `*-head.csv` previews are committed to `main`.
2. They live at the repo root; the importer scripts default to reading from there.
3. No Git LFS, no submodule, no external fetch step.
4. A `.gitattributes` line (`* text=auto`) means git treats the CSVs as text and does line-ending normalisation on commit. This is fine *only* because the CSVs originated on macOS LF — Windows commits would corrupt them.

## Alternatives considered

- **Fetch from CQC URL on container start / on importer invocation.** Rejected: CQC's URL structure has shifted historically; a stable in-repo copy is a reliable input.
- **Object storage (S3 / Azure Blob).** Rejected: requires bucket + credentials + rotation; disproportionate to the project's scope today.
- **Git LFS.** Considered. Rejected because LFS introduces a new failure mode (fetch on clone), adds storage cost, and the CSVs are not currently large enough to need it.
- **`.gitignore` the CSVs, document a "go fetch these" step.** Rejected: turns "clone and run" into "clone, find the URL, hope the URL still works, run".

## Consequences

- `git clone` is ~50 MB heavier than it would otherwise be. Acceptable on broadband; mildly annoying on flaky links.
- Every CSV refresh is a binary-ish commit that bloats history. Over many years this matters; over the current cadence (rare refreshes) it does not.
- There is no automation for "pull latest CQC export". Refresh is a manual download + commit, which keeps human eyes on schema drift but slows down updates.
- The container image already includes the CSVs because the Dockerfile does `COPY . .` — the AKS pod can run the importer without any external fetch. This is part of why this decision works today.
- `* text=auto` in `.gitattributes` means anyone committing from Windows could mangle the CSVs. If contributors broaden, switch this to a per-pattern rule that marks `*.csv` as binary.

## Walk-back options

- **If CSV refreshes become frequent** — move them to object storage; have the importer fetch by URL with a checksum check. Keep `*-head.csv` in repo for fast local dev.
- **If the repo crosses ~500 MB** — same as above, urgently.
- **If a second contributor joins from Windows** — set `*.csv binary` in `.gitattributes` before they touch them.

## Links

- `Locations.csv`, `output.csv` — full CSVs at repo root.
- `Locations-head.csv`, `output-head.csv` — sampled previews for fast iteration.
- `.gitattributes:2` — line-ending normalisation, today.
- `Dockerfile:8` — `COPY . .` pulls CSVs into the image.
- [ADR 0005](0005-two-stage-csv-ingest.md) — what consumes these files.
