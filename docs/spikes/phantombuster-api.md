# Spike — Phantombuster v2 API + LinkedIn phantom I/O

**Status:** Resolved from public docs (2026-06-29). Transport is ground-truth and
reconciled into `phantombuster.py`; the per-phantom **result field names** remain
INFERRED and need one live confirmation run before they're trusted.

**Question:** the offline `phantombuster.py` ([ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md))
was built against best-guess API shapes. Which assumptions hold against
Phantombuster's actual v2 API, and which can only be confirmed by a live scrape?

## Confirmed from docs → applied to the client

| Assumption (was) | Ground truth | Change made |
|---|---|---|
| Auth header `X-Phantombuster-Key-1` | ✓ correct (not `X-Phantombuster-Key`) | none |
| Base `https://api.phantombuster.com/api/v2` | ✓ correct (v1 uses `phantombuster.com`) | none |
| Launch `POST /api/v2/agents/launch` `{id, argument}` | ✓; `argument` accepts object **or** JSON string, but v1 needs a string | **JSON-encode `argument`** (compat) |
| Container id at `data.containerId` (JSend `{status,data}`) | ✓ envelope confirmed; `_data` unwraps it and falls back to top-level | none (`_data` already defensive) |
| Status `{finished, stopped}` | ✗ enum is `starting\|running\|finished\|unknown\|launch error`; **`finished` ≠ success** | terminal = `{finished, launch error}`; gate on `lastEndStatus == "success"` |
| Results via `agents/fetch-output` → `resultObject` | ✗ that's the **secondary** path (a small JSON-string, often unset). **Canonical = S3 `result.json`** | `fetch_result` now: `agents/fetch` → `orgS3Folder`/`s3Folder` → GET `…/result.json`. resultObject kept as `fetch_result_object` |
| Field keys exact-case | ✗ `linkedIn`/`linkedin` casing varies across phantoms | `parse_profile` lowercases keys, matches case-insensitively |

Other confirmed facts now encoded as comments: **`result.csv` is a lossy subset**
of `result.json` (first job/school only) — always read the JSON; **key on the
Agent ID, not the display name** — phantoms get renamed; the **session cookie is
the `sessionCookie` argument field** (the browser-extension "connect" just writes
it), and Sales Navigator phantoms want the Sales Nav cookie.

## Live + hub-confirmed (2026-07-01)

Verified against a live API key and the rendered hub (`hub.phantombuster.com/reference`):

- **Auth works live.** The key authenticates; `GET /agents/fetch-all` returns a
  **bare JSON array** (not the `{status,data}` envelope the other endpoints use).
  The account currently has **0 agents** — nothing to launch until a phantom is
  added in the UI.
- **`agents/launch-sync` does not return result rows.** It streams NDJSON
  (start/heartbeat/logs/summary) + a `containerId`; the summary has exit/timing
  only. So results still come from the S3 `result.json` — the async
  launch→poll→fetch design stands; don't switch to launch-sync for results.
- **No API input-list mechanism.** `org-storage/leads/*` and
  `org-storage/companies-objects/*` are **output stores** (phantoms persist
  scraped people/companies there; leads-keyed on `linkedinProfileUrl`,
  companies-keyed on `linkedinCompanyId`). Lists are filter-views over them. A
  phantom's input is **`spreadsheetUrl` = a public CSV URL** (Google Sheets
  "publish to web → CSV", or a hosted CSV — Drive *share* links are blocked) or a
  single value via `bonusArgument`; **inline arrays are rejected**. We have no
  host → a published CSV is the feed until Phase 6.
- **Per-phantom argument keys come from `GET /agents/fetch?id=<agentId>`** — it
  returns the phantom's **saved argument**, the ground-truth schema as configured
  in the UI. Use that, not the API hub (which documents only generic endpoints).

## Two-stage gap (input side, not yet built)

Company Employees Export is keyed on a LinkedIn **company URL**, but `Provider`
has no such field. So a **stage-0 resolver** (provider → LinkedIn company URL,
stored on a new `Provider.linkedin_company_url`) plus the **feed** (emit a CSV →
publish at a public URL → `spreadsheetUrl`) are prerequisites to the people
scrape. Captured for the ADR 0016 amendment + plan workstream.

## Employee output field names — CONFIRMED via live run (2026-07-02)

A real Company Employees Export run (5 companies × 5 profiles) settled the last
unknown. Real result-row keys:

```
profileUrl, name, firstName, lastName, job, location, connectionDegree, query, timestamp
```

- **`job`** is the title field (e.g. `"Nursing Manager at Care UK"`) — NOT
  `title`/`headline`. `parse_profile` now lists `job` first.
- **No `companyName`.** The company is embedded in `job` and, authoritatively, in
  **`query`** — the input **company URL** for that row. So an Employees Export run
  is inherently **multi-company**: each row's `query` says which company it came
  from. Ingestion must map employee → provider via `query` (→ a future
  `Provider.linkedin_company_url`), not a single run-level provider.
- **Header row is scraped as data** (a `companyUrl` header row → a row with
  `error: "Error retrieving company data"`). Feed the phantom a **headerless**
  URL list, or configure skip-header. `parse_profiles` drops it (no name).
- Result lands at S3 `result.json` (confirmed); `orgS3Folder` populates after the
  first run; `lastEndStatus` was `None` even on success (don't hard-gate on it).
- **Feed:** a Drive `uc?export=download` file URL is **rejected** ("Cannot find a
  way to download"); a Google **Sheets** URL works. Publish input as a Sheet
  (via `gws-cli sheets create` + `drive share --type anyone`).
- **429** on rapid successive `agents/launch` — space launches out.

## Still UNCONFIRMED → needs one live run (or the Phantombuster MCP)

1. ~~**Per-phantom result field names.**~~ **CONFIRMED for the Search Export
   (2026-07-06)** via a live run — see
   [linkedin-acquisition-approach](linkedin-acquisition-approach.md#exp-2--store-search-export-path-a-pii-gated).
   Real keys: `fullName`, `firstName`/`lastName`, `headline`, `jobTitle`,
   `profileUrl`/`linkedinProfileUrl`, `company`/`companyId`/`companySlug`,
   `location`, `query`, `vmid`, … `parse_profile`'s tolerant candidate-key list
   handled them unchanged (`fullName`→name, `headline`→headline, `profileUrl`→url).
2. **Per-phantom launch argument keys.** The input field names (the company/search
   URL param, the profile-count limit) differ per phantom and are partly
   inferred (`spreadsheetUrl`, `numberOfProfiles`, `profileUrl` singular). Wrong
   keys raise "Your Phantom Argument Isn't Valid". Confirm per agent via
   `agents/fetch` (it returns the saved argument).
3. **Credit-usage field.** No documented per-container credit field was found; the
   driver reads `creditUsed`/`credits` tolerantly (→ `None` if absent). Don't rely
   on it for accounting; track credits out-of-band until confirmed.
4. **Custom output filename.** `fetch_result(filename=…)` defaults to
   `result.json`; a phantom configured with a custom output name needs it passed.
5. **`resultObject` wrapper path** (`data.resultObject` vs top-level) — only matters
   if we use the secondary `fetch_result_object`; tolerated by `_data`/`_result_rows`.

## Identification phantoms (current names, key on Agent ID)

- **LinkedIn Company Employees Export** — employees of a company page (not
  "Company Scraper", which scrapes company pages). Output: name, title, company,
  profile URL; ≤1000/company.
- **LinkedIn Search Export** / **Sales Navigator Search Export** — output is
  basic only (profile URL, name, headline); ~1000/day (2500 Sales Nav).
- **LinkedIn Profile Scraper** — single-profile enrich; arg `profileUrl`
  (singular) / a `spreadsheetUrl` of URLs; richest output.

## How to resolve the residual unknowns

A single live run against one test company (Company Employees Export) gives the
real `result.json` shape, confirming items 1–4 at once. The **Phantombuster MCP**
(deferred) is the convenient way to do that without wiring a full live driver.
Until then, `parse_profile` tolerance is the safety margin.

## Sources

Phantombuster API reference (`hub.phantombuster.com/reference/*`), support
articles (retrieve result files via API; get-started; Company Employees / Search
Export / Profile Scraper), legacy `api.rst`, and the dltHub Phantombuster source.
Full citation list captured in the research that produced this spike.
