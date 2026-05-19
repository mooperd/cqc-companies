# ADR 0010 — Hardcoded specialism / service-type filter taxonomy

**Status:** Accepted (2025-08-29).

**TL;DR.** In the context of the `/providers` page offering dropdown filters for "Specialisms" and "Service types", facing a CQC-defined vocabulary that changes slowly (≈yearly), we chose to hardcode the two option lists inline in the route (`app.py:201-255`) rather than deriving them from a `SELECT DISTINCT` over the current data, to achieve a stable, ordered, complete dropdown (including categories not yet present in our dataset) at the cost of a code change whenever CQC adds a category.

## Context

The `specialisms_services` and `service_types` columns store comma-separated strings as imported from CQC. A `SELECT DISTINCT` over them would return whatever happens to be in the current data — including misspellings, blank strings, and ordering accidents — and would miss categories that exist in CQC's universe but are absent from the current import.

The dropdowns are not exhaustive search; they are a curated entry-point. Showing "Caring for adults over 65 yrs" even when no current row matches is fine — it returns an empty result, which is itself informative.

The lists in question are exactly CQC's published taxonomy. They are short (≈20 entries each) and reproduced verbatim in `app.py:201-255`.

## Decision

1. The specialisms and service-type lists are Python lists hardcoded inside the `providers()` route handler.
2. Order is hand-picked (alphabetical with a few semantically-grouped exceptions, matching CQC's published order).
3. Filtering is done with a single `ilike '%<option>%'` against the comma-separated column. This means a row with `service_types = "Nursing homes, Residential homes"` matches both the "Nursing homes" and "Residential homes" filters.
4. The same list is used to populate the `<select>` *and* validate the filter value — i.e. there is no validation; any string passed via `?specialisms=` is fed into the `ilike` clause.

## Alternatives considered

- **`SELECT DISTINCT` at request time.** Rejected: would mirror data quirks (misspellings, missing categories, ordering) into the UI.
- **Normalise into a join table (`Facility ←→ Specialism`, `Facility ←→ ServiceType`).** Rejected for now: the comma-string field is already what CQC publishes; splitting it adds two tables and migration cost for marginal UX gain (faceted counts, "n facilities").
- **Cache the option lists in the database at import time.** Rejected: trades a hardcoded list for a hardcoded import step; same maintenance shape, more moving parts.
- **YAML config file.** Considered. Equivalent to hardcoding in Python for now; might justify itself if a third or fourth taxonomy joins the list.

## Consequences

- Adding a new CQC category requires a code change. Frequency: yearly at most.
- The `ilike '%X%'` filter is robust against the comma-separated storage format but means filters can produce false positives (e.g. a future "Nursing" category would match every "Nursing homes" row). This is acceptable given the current taxonomy has no overlapping substrings.
- The dropdown is decoupled from the data: a stale option (e.g. a category CQC retires) silently keeps appearing until removed. Worth a periodic audit.
- No input validation on `?specialisms=` means a crafted URL can substring-match arbitrary content. The query is parameterised by SQLAlchemy so SQL injection is not the worry; the worry is just "useless results from junk input", which is acceptable.

## Walk-back options

- **If CQC adds a category whose substring conflicts with an existing one** — switch from `ilike '%X%'` to a comma-aware tokenised match (e.g. split on commas at import time into a join table).
- **If a third or fourth taxonomy joins** (e.g. regions, inspection categories with curated copy) — extract to a single config module or YAML file.
- **If counts are wanted next to each dropdown option** — that's the normalised-join-table version; promote then.

## Links

- `app.py:201-255` — the two hardcoded lists.
- `app.py:149-187` — the filters' SQL behaviour.
- [ADR 0005](0005-two-stage-csv-ingest.md) — the importer that produces the comma-separated columns.
