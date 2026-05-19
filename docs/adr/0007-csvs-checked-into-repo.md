# ADR 0007 — CQC source CSVs checked into the repository

**Status:** Accepted (2025-08-28).

**TL;DR.** In the context of needing a reproducible "fresh DB" import for development, demos, and AKS rollouts, facing zero existing object storage infrastructure and CSVs that are public CQC data anyway, we chose to commit `output.csv` (≈30 MB) and `Locations.csv` (≈20 MB) directly to the git repository, accepting bloat in `git clone` and the absence of a refresh-from-source workflow.

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
