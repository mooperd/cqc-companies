# Plan — Automate CSV refresh from CQC bulk monthly downloads

**Status:** Closed (2026-06-20).

## Goal

Implement the refresh mechanism committed to in [ADR 0007 Amendment (2026-05-19)](../adr/0007-csvs-checked-into-repo.md): a scheduled GitHub Actions job that polls CQC's three bulk URLs, detects publication via ETag/Last-Modified, regenerates the committed CSVs in their current shape when something changes, and opens a PR for human review.

## Prerequisites

- ADR 0007 amendment accepted ([commit `fc110e7`](../adr/0007-csvs-checked-into-repo.md#amendment-2026-05-19)).
- Spike `cqc-source-selection.md` Resolved — three named URLs, no auth, schema mapping understood.
- PR-checks workflow from PR #7 merged (provides `smoke` + `actionlint` against the new workflow file).

## Where things stand (2026-06-20)

**Fully shipped and closed.** The whole pipeline landed in `cqc_refresh.py`
(stdlib-only) with the cron workflow at `.github/workflows/cqc-refresh.yml`, and
the mechanism has run for real: the cron opened a refresh PR that was reviewed
and merged to `main` (commit `0b4eea8`, *data: refresh CSVs from CQC monthly
bulk export*). All five phase-exit criteria are met (see bottom of file); the
round-trip criterion was verified on 2026-06-20 by importing the regenerated
`output.csv` + `Locations.csv` into a throwaway local Postgres — both stages
clean (100% match rate, 0 not-found), and the WS4 transforms populated sensibly
(34,678 facilities with overall ratings, 56,810 with `service_types`).

Two deviations from the plan as originally written, both deliberate and
self-evident in the code:

- **WS4 ratings transform** shipped as `pivot_ratings_to_wide()` +
  `merge_ratings_into_locations()` rather than a single `map_ratings_ods()`.
- **WS5 PR creation** lives in the workflow YAML (`gh pr create`/`gh pr edit`
  against one stable bot-owned `data/refresh` branch), not a `diff_and_pr()`
  Python function. The diff/no-op decision is in `_cmd_refresh`.

## Workstreams

### WS1 — URL discovery (S)

**Status:** Shipped — `discover_urls()` (`cqc_refresh.py:111`).

Scrape `https://www.cqc.org.uk/about-us/transparency/using-cqc-data` for the three current month's URLs. Return a `{kind: url}` dict for `{directory_csv, hsca_ods, ratings_ods}`. Pure HTTP + regex/BeautifulSoup; no auth.

**Deliverables:** `cqc_refresh.discover_urls() -> dict[str, str]`. Verified against the live page from this dev session.

**Exit:** Discovery returns the three known May 2026 URLs.

### WS2 — Streaming ODS parser (M)

**Status:** Shipped — `stream_ods()` (`cqc_refresh.py:149`).

Productionised version of `/tmp/probe-ods-headers.py` from the spike. Reads all rows from a named sheet inside an `.ods` (zip-of-OpenDocument-XML) using `ElementTree.iterparse` on the embedded `content.xml`. Yields dicts keyed by header. Must correctly handle `number-columns-repeated` (sparse-cell encoding) and `<text:p>` paragraph-wrapped values.

**Critical constraint** (from the spike): the alternative — `odfpy + pandas.read_excel(engine='odf')` — consumes >3 GB of RAM on the 26 MB ratings file. The streaming parser keeps memory in single-digit MB.

**Deliverables:** `cqc_refresh.stream_ods(path, sheet) -> Iterator[dict[str, str]]`. Memory profile under 50 MB on the largest file.

**Exit:** Reads all three .ods files end-to-end, row counts match what CQC publishes (~88k locations, ~33k providers, ~120k ratings rows).

### WS3 — ETag-based change detection (S)

**Status:** Shipped — `load_state`/`save_state`/`head_with_validators` (`cqc_refresh.py:220`–`242`); state file at `data/cqc-refresh-state.json`.

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

**Status:** Shipped — `map_directory_csv()` (`:349`), `map_hsca_ods()` (`:396`), `pivot_ratings_to_wide()` (`:460`) + `merge_ratings_into_locations()` (`:491`); address split in `_split_collapsed_address()` (`:325`), one-hot flatten in `_make_one_hot_flattener()` (`:375`). Round-trip verified 2026-06-20 (see "Where things stand").

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

**Status:** Shipped — diff/no-op logic in `_cmd_refresh` (`cqc_refresh.py:554`); PR creation lives in `.github/workflows/cqc-refresh.yml` (`gh pr create`/`gh pr edit` on one stable `data/refresh` branch) rather than a `diff_and_pr()` function.

After the mappers run, compare regenerated CSVs against the committed ones:

- Byte-identical → no PR (state file may still update if ETags advanced but content is unchanged).
- Different → row-count delta per file + sample of changed Location IDs (first 10) in the PR body.

PR opened via `gh pr create` using `GITHUB_TOKEN`. Branch naming: `data/refresh-<YYYY>-<MM>-<DD>` (date the cron ran, not the CQC file date — gives a clean history). Title: `data: refresh CSVs from CQC <Month> <YYYY> bulk export`.

**Deliverables:** `cqc_refresh.diff_and_pr(regenerated_paths, state)`. Idempotent: re-running on the same data is a no-op (no duplicate PRs).

**Exit:** A test run on the feature branch produces a PR against the feature branch with a real diff.

### WS6 — Workflow YAML (S)

**Status:** Shipped — `.github/workflows/cqc-refresh.yml` (cron `17 4 * * *` + `workflow_dispatch`; `CQC_PRIMARY_KEY` sanity check at `cqc_refresh.py:652`).

`.github/workflows/cqc-refresh.yml`:

- Triggers: `schedule: cron: "17 4 * * *"` (daily, 04:17 UTC — off-peak), and `workflow_dispatch:` for manual runs.
- Permissions block (explicit): `contents: write`, `pull-requests: write`. **Nothing else.**
- Single job `refresh`: checkout, setup-python 3.12, install pinned `requirements.txt` + the `requests` dep added in WS1, run `python -m cqc_refresh refresh`, commit + push + open PR if changed.
- No CQC API key required. The script must `assert os.getenv('CQC_PRIMARY_KEY') is None` at startup as a sanity check — the bulk-download path doesn't need it; if anyone sets it as a workflow secret it's a misconfiguration.

**Deliverables:** `.github/workflows/cqc-refresh.yml`. Passes the actionlint job from PR #7.

**Exit:** Manual `workflow_dispatch` from the Actions UI opens a refresh PR end-to-end.

### WS7 — Local CLI for development (S)

**Status:** Shipped — `build_parser()` (`cqc_refresh.py:625`) with `discover`/`check`/`refresh` subcommands (`refresh --dry-run` supported).

`python -m cqc_refresh` exposes the same pipeline locally so we don't have to push-and-wait to iterate on the mappers. Subcommands:

- `discover` — print the three URLs.
- `check` — HEAD-only; print which files have changed since the state file.
- `refresh --dry-run` — download + parse + map + diff, but don't commit or open a PR.
- `refresh` — full pipeline.

**Deliverables:** `__main__.py` (or a click-based CLI in `cqc_refresh.py`).

**Exit:** A developer can run `python -m cqc_refresh refresh --dry-run` and see the same output the cron job would log.

## Phase exit criteria

All true as of 2026-06-20 — plan Closed:

- [x] WS1–WS7 deliverables shipped (in `cqc_refresh.py` + `.github/workflows/cqc-refresh.yml`).
- [x] A real refresh PR has been opened by the workflow, reviewed, and merged to `main` (commit `0b4eea8`).
- [x] The regenerated CSVs round-trip through the existing importers against a local Postgres — verified 2026-06-20 (both stages 100% match rate, 0 not-found; WS4 ratings/service-type columns populated).
- [x] PR #7 has merged (2026-05-19) so the new workflow gets PR-time `actionlint` + `smoke` checks.
- [x] ADR 0007's amendment-walk-back triggers are unmodified by anything in this plan.

## References

- [ADR 0007 — CSVs in repo, amended](../adr/0007-csvs-checked-into-repo.md) — the decision this plan implements.
- [Spike — CQC source selection](../spikes/cqc-source-selection.md) — desk research + lesson on the ODS-parser memory trap.
- [ADR 0005 — Two-stage CSV ingest](../adr/0005-two-stage-csv-ingest.md) — the importer this plan deliberately doesn't change.
- [PR #7 — PR-time CI](https://github.com/mooperd/cqc-companies/pull/7) — provides actionlint over the new workflow.
- `docs/cqc_authentication_flow.odt` — API contingency, kept gated.
