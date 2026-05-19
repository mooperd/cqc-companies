# ADR 0004 — Matplotlib server-side charts, base64-embedded in HTML

**Status:** Accepted (2025-09-01).

**TL;DR.** In the context of the `/statistics` page rendering six charts derived from full-table aggregates, facing the existing all-server-rendered architecture from [ADR 0003](0003-server-rendered-flask-jinja.md) and a desire to keep the front-end JS-free, we chose Matplotlib (Agg backend) to render each chart in-process and inline the PNG as a base64 `data:` URI in the HTML response, accepting per-request rendering cost (~hundreds of ms) and no interactivity.

## Context

The statistics page surfaces six aggregates: rating distribution (pie), facilities by region (bar), care-home bed distribution (histogram), top service types (horizontal bar), provider-size distribution (pie), facilities-vs-beds by region (dual axis). Each is computed from a `Facility`/`Provider` query at render time.

A client-side charting library (Chart.js, Plotly, ApexCharts) would have required either embedding a JS asset bundle or rebuilding the page as an SPA. Both contradict [ADR 0003](0003-server-rendered-flask-jinja.md). Matplotlib was already a familiar tool, and the Agg (non-interactive) backend renders cleanly in a server process with `matplotlib.use('Agg')`.

The page is not user-facing in a high-traffic sense — it's a developer/internal-stakeholder dashboard. Caching wasn't a forcing concern.

## Decision

1. Matplotlib (Agg backend, set at import in `app.py:7-9`) renders each chart to a PNG `BytesIO`.
2. `fig_to_base64(fig)` (`app.py:602-609`) base64-encodes the PNG and `plt.close()`s the figure to free memory.
3. Each render is done synchronously inside the `/statistics` request; no precomputation, no caching, no background job.
4. The colours for the rating pie are pinned to a CQC-evocative palette (Outstanding=green, Good=blue, Requires improvement=amber, Inadequate=red) in `create_rating_pie_chart` (`app.py:400-405`).

## Alternatives considered

- **Client-side charts (Chart.js / Plotly).** Rejected: would force a JS pipeline contradicting [ADR 0003](0003-server-rendered-flask-jinja.md). Best deferred until/unless interactivity is requested.
- **Static SVG with Jinja.** Considered for the simpler bar/pie charts; rejected because Matplotlib already covers all six chart types uniformly.
- **Precompute charts on import / on a cron, store on disk or in Postgres.** Rejected: the data changes only when a new CSV is imported, and `/statistics` is hit rarely enough that per-request render is cheaper than a cache invalidation story.

## Consequences

- The `/statistics` page makes 7+ queries (one per chart plus the basic-counts block) and renders 6 figures per request. Page load is multi-hundred-ms; acceptable for internal use, would not survive a load spike.
- The charts are pixel-perfect PNGs; no zoom, no hover, no link-out from chart elements.
- Matplotlib + numpy add ~80 MB to the container image and a few seconds to cold-start time. The Dockerfile (`FROM python` — see ADR-008 consequence list) doesn't pin Python or matplotlib versions, so cold-start cost is not deterministic.
- Memory bug-trap: forgetting `plt.close(fig)` leaks a figure per request. The helper does this correctly — if anyone adds a new chart function, they must call through `fig_to_base64` (which closes) rather than rolling their own.

## Walk-back options

- **If `/statistics` becomes externally exposed or load-sensitive** — cache the rendered charts keyed on the import timestamp; invalidate when a new CSV import commits.
- **If users ask for hover / zoom / drill-down on any single chart** — switch *that one chart* to a client library (Chart.js, ECharts) and keep the rest as PNG. Don't rewrite the whole page.

## Links

- `app.py:362-609` — `generate_charts()` and the six `create_*_chart` functions.
- `app.py:7-9` — Agg backend pin.
- [ADR 0003](0003-server-rendered-flask-jinja.md) — the no-JS framework constraint this decision sits inside.
