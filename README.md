# cqc-companies

A Flask app that ingests the Care Quality Commission (CQC) public CSV exports —
providers and locations — into PostgreSQL and surfaces them via a server-rendered
UI with search, filters, and a statistics page. It is growing into a
relationship-building CRM: Companies House director enrichment and LinkedIn
decision-maker identification hang off the same `Person`/`Role` model.

Runs locally; **no cloud deploy at present** (see [Deployment](#deployment)).

> The *why* behind the design lives in [`docs/adr/`](docs/adr/README.md); what's
> next in [`docs/plans/`](docs/plans/); validated experiments in
> [`docs/spikes/`](docs/spikes/). Start with [`CLAUDE.md`](CLAUDE.md) for the
> project-memory discipline and [`docs/product-vision.md`](docs/product-vision.md)
> for the direction.

## Quick start

```sh
# 1. Postgres running locally, and a database to point at.
#    The app defaults to postgresql://darwinist:darwinist@localhost:5432/darwinist
#    — override with DATABASE_URL for your own setup.
export DATABASE_URL="postgresql://<user>@localhost:5432/<db>"

# 2. Install deps + create/patch the schema.
uv venv && uv pip install -r requirements.txt
uv run python init_db.py            # idempotent: creates or updates tables

# 3. Load data (see "Building the database" — or restore a dump).
uv run python import_records.py output.csv
uv run python enrich_locations.py Locations.csv

# 4. Run the app.
uv run python app.py                # http://localhost:5000
```

`./run.sh` is a one-shot launcher (venv + pip + a generated `.env` + `python
app.py`). Note it writes its own `DATABASE_URL` into `.env`; for a real setup,
set `DATABASE_URL` yourself (in `.env` or the environment).

## The database

### Schema

Defined in [`model.py`](model.py) via SQLAlchemy. Per
[ADR 0002](docs/adr/0002-postgres-sqlalchemy-no-migrations.md) there are (for now)
no Alembic migrations — the schema is applied with **`init_db.py`**, which is
idempotent: it creates missing tables *and* additively adds any missing columns
and indexes to existing ones (plain `create_all()` never adds a column to a table
that already exists). Run it after pulling schema changes.

```sh
uv run python init_db.py            # create-or-update, non-destructive
uv run python init_db.py --dry-run  # show the DDL it would run
uv run python init_db.py --drop     # DANGER: drop all model tables and recreate
```

Core tables: `provider`, `facility`, `person`, `role` (person↔provider facts —
[ADR 0014](docs/adr/0014-person-role-correlation-model.md)), plus `app_user`,
`phantom_run`, `suppressed_contacts` for the CRM/LinkedIn work.

### Building the database from scratch

The pipeline is layered; each stage is a script you run in order.

| Stage | Command | Input | Notes |
|---|---|---|---|
| 1. Providers + facilities | `import_records.py output.csv` | committed seed CSV | the CQC directory export |
| 2. Location enrichment | `enrich_locations.py Locations.csv` | committed seed CSV | HSCA active-locations detail |
| 3. CQC deltas (optional) | `apply_events.py` | `data/changes/*.json` | monthly refresh deltas ([ADR 0015](docs/adr/0015-data-freshness-strategy.md)); none committed yet |
| 4. Companies House directors | `enrich_people.py` | **live CH API** | needs `COMPANIES_HOUSE_*` keys ([ADR 0013](docs/adr/0013-companies-house-source.md)) |
| 5. LinkedIn people | `run_extraction.py …` | **live Phantombuster** | needs keys + a connected LinkedIn session ([ADR 0016](docs/adr/0016-linkedin-phantombuster-ingestion.md)) |

Stages 1–2 rebuild the full CQC baseline (~37k providers) from the seed CSVs
committed to this repo. Stages 3–5 are enrichment.

### Reproducibility — read this before sharing data

Not everything in a populated database can be rebuilt from source. Know which
layer you're dealing with:

- **CQC core (providers, facilities): fully reproducible.** `output.csv` and
  `Locations.csv` are the immutable seeds ([ADR 0015](docs/adr/0015-data-freshness-strategy.md))
  and are **committed to this repo**, so stages 1–2 rebuild the same baseline on a
  fresh clone. (`output-head.csv` / `Locations-head.csv` are small samples used by
  the offline tests.)
- **Companies House enrichment (~95k people): re-derivable, not byte-identical.**
  Stage 4 hits the live CH API, so it needs a key and its output tracks whatever
  the API returns *now* — re-running reconstructs the shape, not a frozen snapshot.
- **LinkedIn people: NOT reproducible from source.** Stage 5 is a live, gated
  scrape (Phantombuster + a LinkedIn session, [ADR 0017](docs/adr/0017-gdpr-controller-posture.md));
  results are non-deterministic and depend on external state — re-running does
  **not** reproduce the same rows. Their **authoritative home is the deployed
  Postgres** ([ADR 0019](docs/adr/0019-scraped-data-lives-in-deployed-db.md)); the
  scrape writes straight into it and everyone reads it there. Because they can't be
  regenerated, that DB needs backups ([ADR 0018](docs/adr/0018-hetzner-single-box-deploy.md)
  amendment).

**Consequence:** the *current, enriched* state (CH + LinkedIn people) lives in the
deployed DB, not in any committed input. To run a **local copy**, either point
`DATABASE_URL` at the deployed DB or restore a shared **`pg_dump`** (below) — the
dump is a local-dev convenience, not the source of truth. To hand someone a
*reproducible base* (CQC providers/facilities only), the committed seed CSVs + the
scripts are enough.

### Working with a database dump

To **create** a dump (contains personal data — [ADR 0017](docs/adr/0017-gdpr-controller-posture.md);
`*.sql`/`*.sql.gz` are gitignored so they can't be committed):

```sh
pg_dump darwinist | gzip > darwinist-dump.sql.gz
```

To **restore** one someone shared with you:

```sh
createdb cqc                                             # a fresh target DB
gunzip -c darwinist-dump.sql.gz | psql -d cqc            # load it
export DATABASE_URL="postgresql://<user>@localhost:5432/cqc"
uv run python init_db.py                                 # patch schema to current model.py
uv run python app.py                                     # browse it
```

`init_db.py` after a restore matters: a dump captures the schema *as it was when
dumped*, so if `model.py` has moved on, `init_db.py` additively brings the restored
DB up to date (it won't touch the data).

**Handling caveat:** a dump is real personal data leaving one machine and landing
on another. Delete it when you're done, and don't put it anywhere it could be
committed or indexed. How to distribute scraped data *without* passing whole dumps
around is an open design question — see
[docs/spikes/scraped-data-distribution.md](docs/spikes/scraped-data-distribution.md).

## LinkedIn decision-maker identification (ADR 0016)

Resolves a provider's brand to a LinkedIn company id, then scrapes that company's
people into low-confidence `Person`/`Role` rows via Phantombuster. **Gated** on
credentials and a GDPR sign-off ([ADR 0017](docs/adr/0017-gdpr-controller-posture.md)).

```sh
uv run python pb_doctor.py                       # preflight: check the config chain
uv run python run_extraction.py --provider-name "Barchester"            # dry preview
uv run python run_extraction.py --provider-name "Barchester" --resolve --scrape --limit 10
```

Config (in `.env.local`, gitignored): `PHANTOMBUSTER_API_KEY`,
`PB_SEARCH_EXPORT_AGENT`, `PB_SOURCE_AGENT`, `APP_SECRETS_KEY`. See
[`.env.example`](.env.example) and the
[acquisition spike](docs/spikes/linkedin-acquisition-approach.md) for how this
actually behaves against live LinkedIn.

### Coverage is bounded by the scraping identity's network (not by config)

The people-scrape runs a LinkedIn **people search** as a connected identity, and a
**regular LinkedIn account only sees people within its own network** (2nd/3rd-degree
connections). So the Search Export returns *the target company's employees who are in
the scraping account's network* — often just a handful, sometimes zero, regardless of
how big the company is. Verified 2026-07-21: with the per-search cap raised to 25,
Marie Curie still returned only 3 and Leonard Cheshire 0 — the limiter is the
identity's network, **not** `numberOfResultsPerSearch`.

To get comprehensive employee lists (tens per company), the scraping identity needs
**LinkedIn Sales Navigator** — Sales Nav search returns people beyond your own network.
Without it, expect sparse, network-dependent yields; scrape many companies to
accumulate. Operational session mechanics (the managed-identity cookie, `exit 84`
vs `exit 1`) are in the [acquisition spike](docs/spikes/linkedin-acquisition-approach.md).

## Web UI

`/` facilities · `/providers` (search/filter) · `/provider/<id>` (a provider's
people, grouped by source) · `/statistics` (server-rendered charts).

## Testing

Every `test_*.py` is self-contained (in-memory SQLite, no live keys) and runs as a
plain script. CI gates `main` on the full suite ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

```sh
for t in test_*.py; do uv run python "$t"; done
```

## Deployment

**None currently.** The previous AKS deploy was removed on 2026-05-20 — see
[ADR 0008](docs/adr/0008-aks-envsubst-deploy.md) and
[ADR 0009](docs/adr/0009-in-cluster-postgres.md) (both Withdrawn). The next target
is a future decision; per [`docs/product-vision.md`](docs/product-vision.md) it
reopens at Phase 6.
