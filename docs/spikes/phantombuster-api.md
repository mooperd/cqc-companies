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

## Still UNCONFIRMED → needs one live run (or the Phantombuster MCP)

1. **Per-phantom result field names.** `fullName` / `firstName`+`lastName`,
   `linkedInProfileUrl` vs `profileUrl`, `headline` vs `title`/`jobTitle`,
   `companyName`, `location` are INFERRED from PB's CSV-header conventions and
   integration mappings, **not** seen in a raw `result.json`. `parse_profile` is
   tolerant + case-insensitive to absorb variation, but the candidate-key list
   should be validated against real output. *This is the single most important
   thing to confirm before trusting ingested data.*
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
