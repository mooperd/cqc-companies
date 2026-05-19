# Plan — Automate CSV refresh from CQC bulk monthly downloads

**Status:** Active.

## Goal

Implement the refresh mechanism committed to in [ADR 0007 Amendment (2026-05-19)](../adr/0007-csvs-checked-into-repo.md): a scheduled GitHub Actions job that polls CQC's three bulk URLs, detects publication via ETag/Last-Modified, regenerates the committed CSVs in their current shape when something changes, and opens a PR for human review.

## Prerequisites

- ADR 0007 amendment accepted ([commit `fc110e7`](../adr/0007-csvs-checked-into-repo.md#amendment-2026-05-19)).
- Spike `cqc-source-selection.md` Resolved — three named URLs, no auth, schema mapping understood.
- PR-checks workflow from PR #7 merged (provides `smoke` + `actionlint` against the new workflow file).

## Where things stand (2026-05-19)

WS1–WS7 are the natural implementation phases. None started yet beyond the ADR amendment.

## Workstreams

### WS1 — URL discovery (S)

**Status:** Open.

Scrape `https://www.cqc.org.uk/about-us/transparency/using-cqc-data` for the three current month's URLs. Return a `{kind: url}` dict for `{directory_csv, hsca_ods, ratings_ods}`. Pure HTTP + regex/BeautifulSoup; no auth.

**Deliverables:** `cqc_refresh.discover_urls() -> dict[str, str]`. Verified against the live page from this dev session.

**Exit:** Discovery returns the three known May 2026 URLs.

### WS2 — Streaming ODS parser (M)

**Status:** Open.

Productionised version of `/tmp/probe-ods-headers.py` from the spike. Reads all rows from a named sheet inside an `.ods` (zip-of-OpenDocument-XML) using `ElementTree.iterparse` on the embedded `content.xml`. Yields dicts keyed by header. Must correctly handle `number-columns-repeated` (sparse-cell encoding) and `<text:p>` paragraph-wrapped values.

**Critical constraint** (from the spike): the alternative — `odfpy + pandas.read_excel(engine='odf')` — consumes >3 GB of RAM on the 26 MB ratings file. The streaming parser keeps memory in single-digit MB.

**Deliverables:** `cqc_refresh.stream_ods(path, sheet) -> Iterator[dict[str, str]]`. Memory profile under 50 MB on the largest file.

**Exit:** Reads all three .ods files end-to-end, row counts match what CQC publishes (~88k locations, ~33k providers, ~120k ratings rows).

### WS3 — ETag-based change detection (S)

**Status:** Open.

State stored as a single committed JSON file at `data/cqc-refresh-state.json` with the shape:

```json
{
  "last_run": "2026-05-19T15:00:00Z",
  "files": {
    "directory_csv": {"url": "...", "etag": "...", "last_modified": "..."},
    "hsca_ods": {...},
    "ratings_ods": {...}
  }
}
```

The cron job HEADs each URL with `If-None-Match` and `If-Modified-Since` headers from the state file. If all three return 304, the job exits — no further work. If any return 200, that file's downloaded and the pipeline runs.

Committed JSON chosen over GH Actions cache (opaque, evicts) or PR-body embedding (fragile). The diff noise is intentional — the state file appears in every refresh PR, making the trigger auditable.

**Deliverables:** `data/cqc-refresh-state.json` (created empty on first run), `cqc_refresh.check_for_updates(state) -> dict[str, ChangedFile]`.

**Exit:** Two consecutive runs against the live URLs — first downloads everything, second exits in seconds with no work.

### WS4 — Schema mapping (L)

**Status:** Open.

Three transforms required to land the bulk-file data in the existing CSV shape (so the importers don't need to change in this PR — they keep reading the same column names they do today).

| Source | Target | Transform |
|---|---|---|
| `<DD>_<Month>_<YYYY>_CQC_directory.csv` | `output.csv` | Column rename per the table in the spike's "Findings"; **split the new single `Address` field** (quoted multi-line, e.g. `"7-9 White Kennet Street,London"`) back into `Address 1`, `Address 2`, `Town/City`, `County` |
| `<DD>_<Month>_<YYYY>_HSCA_Active_Locations.ods` (HSCA_Active_Locations sheet) | `Locations.csv` | Subset to current columns; flatten **one-hot Y/N service-type cols 76-108** into a comma-separated `Service Types` string; flatten **one-hot Y/N service-user-band cols 109-120** into `Service users supported`; rename per spike's table |
| `<DD>_<Month>_<YYYY>_Latest_ratings.ods` (Locations sheet) | merged into `Locations.csv` (sub-rating columns) | Filter to `Service / Population Group = "Overall"`; pivot from long (one row per `Domain`) to wide (5 columns: Safe / Effective / Caring / Responsive / Well-led); join into the HSCA-derived Locations.csv by `Location ID` |

The "Address" splitting is the highest-risk transform — CQC's collapsed format isn't formally specified. Strategy: split on `,` to recover comma-separated address parts, then bucket into `Address 1`, `Address 2`, `Town/City`, `County` by position. Spike with 20 known rows from the current `output.csv` to validate; expect a small residual of unparseable rows (logged, not failed).

**Deliverables:** `cqc_refresh.map_directory_csv()`, `cqc_refresh.map_hsca_ods()`, `cqc_refresh.map_ratings_ods()`. Each takes an iterator of source rows and returns an iterator of target rows in the existing CSV shape.

**Exit:** Regenerated CSVs round-trip cleanly through the existing `import_records.py` / `enrich_locations.py` against a local Postgres (manual verification).

### WS5 — Diff detection + PR creation (M)

**Status:** Open.

After the mappers run, compare regenerated CSVs against the committed ones:

- Byte-identical → no PR (state file may still update if ETags advanced but content is unchanged).
- Different → row-count delta per file + sample of changed Location IDs (first 10) in the PR body.

PR opened via `gh pr create` using `GITHUB_TOKEN`. Branch naming: `data/refresh-<YYYY>-<MM>-<DD>` (date the cron ran, not the CQC file date — gives a clean history). Title: `data: refresh CSVs from CQC <Month> <YYYY> bulk export`.

**Deliverables:** `cqc_refresh.diff_and_pr(regenerated_paths, state)`. Idempotent: re-running on the same data is a no-op (no duplicate PRs).

**Exit:** A test run on the feature branch produces a PR against the feature branch with a real diff.

### WS6 — Workflow YAML (S)

**Status:** Open.

`.github/workflows/cqc-refresh.yml`:

- Triggers: `schedule: cron: "17 4 * * *"` (daily, 04:17 UTC — off-peak), and `workflow_dispatch:` for manual runs.
- Permissions block (explicit): `contents: write`, `pull-requests: write`. **Nothing else.**
- Single job `refresh`: checkout, setup-python 3.12, install pinned `requirements.txt` + the `requests` dep added in WS1, run `python -m cqc_refresh refresh`, commit + push + open PR if changed.
- No CQC API key required. The script must `assert os.getenv('CQC_PRIMARY_KEY') is None` at startup as a sanity check — the bulk-download path doesn't need it; if anyone sets it as a workflow secret it's a misconfiguration.

**Deliverables:** `.github/workflows/cqc-refresh.yml`. Passes the actionlint job from PR #7.

**Exit:** Manual `workflow_dispatch` from the Actions UI opens a refresh PR end-to-end.

### WS7 — Local CLI for development (S)

**Status:** Open.

`python -m cqc_refresh` exposes the same pipeline locally so we don't have to push-and-wait to iterate on the mappers. Subcommands:

- `discover` — print the three URLs.
- `check` — HEAD-only; print which files have changed since the state file.
- `refresh --dry-run` — download + parse + map + diff, but don't commit or open a PR.
- `refresh` — full pipeline.

**Deliverables:** `__main__.py` (or a click-based CLI in `cqc_refresh.py`).

**Exit:** A developer can run `python -m cqc_refresh refresh --dry-run` and see the same output the cron job would log.

## Phase exit criteria

When all of these are true, this plan closes (`Status: Closed (YYYY-MM-DD)`):

- [ ] WS1–WS7 deliverables shipped on this branch.
- [ ] A real refresh PR has been opened by the workflow (against the feature branch during development) and reviewed.
- [ ] The regenerated CSVs round-trip through the existing importers against a local Postgres.
- [ ] PR #7 has merged so the new workflow gets PR-time `actionlint` + `smoke` checks.
- [ ] ADR 0007's amendment-walk-back triggers are unmodified by anything in this plan (sanity check on scope creep).

## References

- [ADR 0007 — CSVs in repo, amended](../adr/0007-csvs-checked-into-repo.md) — the decision this plan implements.
- [Spike — CQC source selection](../spikes/cqc-source-selection.md) — desk research + lesson on the ODS-parser memory trap.
- [ADR 0005 — Two-stage CSV ingest](../adr/0005-two-stage-csv-ingest.md) — the importer this plan deliberately doesn't change.
- [PR #7 — PR-time CI](https://github.com/mooperd/cqc-companies/pull/7) — provides actionlint over the new workflow.
- `docs/cqc_authentication_flow.odt` — API contingency, kept gated.
