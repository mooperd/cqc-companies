# Handoff — data freshness (ADR 0015): WS1–WS3 shipped, WS4 (Companies House filing-history producer) is next

**Created:** 2026-06-22
**Working tree:** clean (after committing the `.gitignore` `*.swp` line alongside this handoff)
**Branch:** main

<!--
Ephemeral. At resolution, fold load-bearing pieces into docs/plans/data-freshness.md
/ the ADRs and `git rm` this file. See docs/handoff-discipline.md.
-->

## Goal & next-up

**Goal of this session:** implement [ADR 0015](../adr/0015-data-freshness-strategy.md)
— change-event files are canonical, the DB is a projection. (Earlier in the
session: Person/Role + Companies House officers+PSC enrichment, ADR 0013/0014,
and a full live enrichment run.)

**Next session should pick up:** **WS4 — Companies House filing-history producer**
in [`docs/plans/data-freshness.md`](../plans/data-freshness.md#ws4--companies-house-change-event-file-producer).
Extend `enrich_people.py`: per provider with a CH number, one `filing-history`
call; if the latest officer/PSC-category filing is newer than
`Provider.ch_filing_watermark`, re-poll officers+PSC, diff against current roles,
emit `role_appointed|ended|changed` ChangeEvents into
`data/changes/companies-house-YYYY-MM-DD.json`; update watermark + `ch_enriched_at`.
First file = current 155k roles as the `*_added` seed. Then **WS5** (simplify
`cqc-refresh.yml` to commit the delta file + run `apply_events`).

**Verification command:**

```sh
# Build a throwaway venv (no .venv in repo; /tmp one from this session is gone):
uv venv /tmp/cqc-venv --python 3.12 && uv pip install --python /tmp/cqc-venv/bin/python -r requirements.txt
PY=/tmp/cqc-venv/bin/python
# All offline tests green:
for t in test_cqc_mapping test_cqc_refresh test_apply_events test_companies_house test_enrich_people; do "$PY" $t.py >/dev/null && echo "$t OK"; done
# Dev DB is populated + ADR-0015-migrated:
psql postgresql://darwinist:darwinist@localhost:5432/darwinist -tAc \
  "SELECT (SELECT count(*) FROM provider), (SELECT count(*) FROM person), (SELECT count(*) FROM role), (SELECT count(*) FROM change_events);"
# Expect: tests OK; 36982 providers, 95629 people, 155628 roles, 0 change_events.
# (36982 because the dev DB was seeded BEFORE the WS3b keying fix; a re-seed via
#  the refactored importer yields 36970 — the de-duplicated name-variants.)
```

## Done this session (freshness arc)

| Commit | Subject |
|---|---|
| `f2fd751` | docs: ADR 0015 — change-event files as source of truth, DB as projection |
| `1f0e87e` | WS1: ChangeEvent + AppliedEventFile + provider lifecycle schema |
| `cbecdff` | WS2: cqc_refresh emits append-only delta files (not full CSVs) |
| `278d474` | WS3a: Facility.active + removed_at (location-level soft-delete) |
| `a877833` | WS3b: shared cqc_mapping; importers key providers by cqc_provider_id |
| `904f289` | WS3c: apply_events — change-event files → DB projection |

(Earlier in the session, also on `main`: ADR 0012/0013/0014, Person/Role + PSC
correlation, `enrich_people`, and a full live CH enrichment run.)

## Open follow-ups (priority-ordered)

### 1. WS4 — Companies House filing-history producer (L)

See plan WS4 (linked above). Key facts already established:
- Endpoint: `GET /company/{n}/filing-history`; filter entries to officer/PSC
  categories; compare latest to `Provider.ch_filing_watermark`.
- Reuse `companies_house.fetch_officers/fetch_psc` (already resilient: retries
  5xx/429/network; `fetch_psc` returns `[]` on 404). Reuse `enrich_people`'s
  correlation + `sync_provider` for the role diff; emit `role_*` ChangeEvents.
- Cost (chosen): ~25k cheap filing-history calls/month + heavy re-poll only for
  changed companies (rate limit ~600/5min).

### 2. WS5 — simplify `cqc-refresh.yml` (S)

Commit the small `data/changes/cqc-*.json` (not force-pushed CSVs); optionally
run `apply_events` / schedule the CH producer. See plan WS5.

### 3. Move ADR 0013 / 0014 / 0015 Proposed → Accepted (S)

Once their pipelines have run for real (0013/0014 are effectively done; 0015
after WS4/WS5).

## Critical context

- **Dev DB is provisioned and fully populated.** `darwinist` role + `darwinist`
  DB on local Homebrew Postgres 18 (superuser `roberttaylor`). Holds the real
  seed import + the full CH enrichment (95,629 people / 155,628 roles) and is
  migrated for the ADR 0015 schema (change_events, applied_event_file,
  Provider/Facility lifecycle columns; `ch_enriched_at` backfilled for the
  24,742 enriched providers). **554 providers errored** during enrichment →
  `ch_enriched_at IS NULL` → WS4 retries them.
- **No `.venv` in the repo** (requirements.txt project). Build a throwaway venv
  as in the verification command. `uv run` alone fails (no deps).
- **`.env.local`** (gitignored) holds `COMPANIES_HOUSE_ENV=live` + live & test
  keys. The CLIs (`enrich_people`, `companies_house`, `apply_events`) load it.
- **CQC pipeline is end-to-end but unproven on a real changed bulk** — the diff
  logic is unit-tested (replay invariant) and apply is unit-tested, but no live
  CQC delta has been produced/applied yet (data hasn't changed since the seed).
- Schema changes are applied to the dev DB via `ADD COLUMN IF NOT EXISTS` +
  `create_all` (ADR 0002, no Alembic). A fresh DB just needs `create_all` +
  seed import + `apply_events`.

## References

- [`docs/plans/data-freshness.md`](../plans/data-freshness.md) — WS1–WS5 state (WS4/WS5 open)
- [`docs/adr/0015-data-freshness-strategy.md`](../adr/0015-data-freshness-strategy.md) — the decision
- [`docs/plans/companies-house-enrichment.md`](../plans/companies-house-enrichment.md) — CH enrichment WS4 reuses

## Migration note

When WS4/WS5 land: their detail is already in `data-freshness.md` — just update
those WS statuses, move ADR 0015 (and 0013/0014) Proposed→Accepted, then
`git rm docs/handoffs/data-freshness-handoff.md` in the same commit.
