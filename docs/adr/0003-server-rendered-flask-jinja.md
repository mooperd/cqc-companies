# ADR 0003 — Server-rendered Flask + Jinja, no front-end framework

**Status:** Accepted (2025-08-28).

**TL;DR.** In the context of a read-mostly directory of ~50k facilities filtered/searched by simple ilike clauses, facing a single-developer cadence and zero requirement for offline / mobile / real-time UX, we chose server-rendered Jinja templates with full-page reloads over an SPA + JSON API, to achieve the smallest plausible front-end surface area, accepting that any client-side richness later (sortable tables, inline charts, partial updates) will require either HTMX-style augmentation or a deliberate pivot.

## Context

The UI has three pages: facility list with search and pagination (`/`), provider list with search/filter/sort (`/providers`), and a statistics dashboard (`/statistics`). All of them are populated by SQLAlchemy queries that already produce exactly the shape the template needs; the only client-side state is "which page am I on" and "what's in the search box", both of which round-trip via the URL.

The pagination is `Flask-SQLAlchemy`'s `query.paginate(...)`, the filters are GET-parameter-driven, and there is no authenticated state. There is also no requirement for fast partial updates: a 200 ms full-page reload on a search is acceptable and arguably clearer than an SPA equivalent.

There is a `google_login.py` module present in the tree but it imports a `app.telemetry` package that does not exist; it is not wired into `app.py`. It represents aspirational scope, not a current decision.

## Decision

1. The UI is rendered entirely server-side from Jinja templates under `templates/`. No JavaScript framework, no SPA, no fetch-based JSON API.
2. URL query parameters carry all filter / search / pagination state. Forms submit as `GET` for filters, `POST` for imports.
3. Bootstrap-ish styling lives inline in the templates; there is no asset pipeline.
4. The only JSON endpoint is implicit (`healthz` returns plain text, see [ADR 0008](0008-aks-envsubst-deploy.md)). All data-bearing routes return HTML.

## Alternatives considered

- **SPA (React / Vue / Svelte) over a JSON API.** Rejected: would double the surface area (a build pipeline, a state layer, a routing layer) for no UX gain at this scope. A single developer can ship Jinja faster than they can ship an SPA *and* keep it under maintenance.
- **HTMX over Jinja.** Considered, deferred. It would buy partial-page updates without an SPA, and the upgrade path from the current templates is cheap. Worth revisiting *if and when* a UX need surfaces (e.g. live filter chips on the providers page).
- **Server-rendered + a JSON sub-API for a future mobile/external consumer.** Rejected for now: YAGNI. The CQC public CSVs are the canonical external API; if external consumers appear, they should go via the CSV pipeline, not via this Flask app.

## Consequences

- The codebase is small and the page logic is colocated with the SQL query that feeds it — easy to trace from URL to query.
- Sortable columns, expandable rows, in-page filtering all require a round-trip. Acceptable today; would become annoying if dataset queries get slow.
- There is no API to test independently of the templates; future automation (LLM-driven scraping, mobile clients) has no contract to bind to.
- The `SECRET_KEY` in `app.py:14` is the literal string `'your-secret-key-here'`. There is no session-sensitive state today, but this must change before any auth lands (the `google_login.py` aspiration).

## Walk-back options

- **If a single page genuinely needs partial updates** — adopt HTMX for that page only. No full-tree SPA conversion required.
- **If a structured API consumer appears** — extract the queries currently feeding the templates into a thin Blueprint that returns JSON, then have the templates call it via fetch. Don't rewrite the whole app.
- **If auth comes online (via `google_login.py` or otherwise)** — replace the hardcoded `SECRET_KEY`, add `flask_login` or equivalent. The current decision doesn't preclude this; it just hasn't happened.

## Links

- `app.py:24-54` (index), `:127-268` (providers), `:270-281` (statistics) — the three rendered routes.
- `templates/` — Jinja templates.
- `google_login.py` — present but unwired (imports a non-existent `app.telemetry`); a *future* decision, not a current one.
