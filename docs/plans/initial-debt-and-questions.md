# Plan — Initial debt and open questions

**Status:** Proposed.

<!--
Status lifecycle:
  Proposed → Active → Closed (YYYY-MM-DD)
Update in place; don't stack past states.
-->

## Goal

Resolve the items flagged in the "Consequences" sections of ADRs 0001–0010 (the reverse-engineering pass on 2026-05-19). Each item is either a **question** to verify against the live system or a **fix** for known debt the ADRs documented honestly.

This plan is bounded: when every workstream below is Shipped, Investigated, or explicitly deferred to its own follow-up plan, this file is updated to `Closed` and stays in the repo as a record of the first sweep.

## Prerequisites

- ADRs 0001–0010 Accepted (they are).
- A working `kubectl` context against the `cqc` AKS namespace, for the verification workstreams.

## Where things stand (2026-05-19)

Nothing started. This is the initial backlog produced from the reverse-engineering pass.

## Workstreams

The workstreams split into two groups: **verify** (we genuinely don't know the answer) and **fix** (we know what needs doing).

---

### WS1 — Verify: does the deployed app actually reach Postgres? (S)

**Status:** Open. Source: ADR 0009 §Decision-5, ADR 0008 §Consequences.

`k8s/deployment.yaml:30` hardcodes `DATABASE_URL=postgresql://darwinist:darwinist@postgres-service-cqc:5432/darwinist`, but the Postgres `Service` in `k8s/postgres.yaml:90-99` is named **`postgres`**, not `postgres-service-cqc`. The commit history (`e838157 fixed database url`) suggests this was deliberately set, so something must be filling the gap — most likely the `application-environment-variables` ConfigMap that the deploy workflow copies from the `default` namespace (`gh-actions/main.yml:86-88`) supplies a different value that takes precedence over the inline env var.

**Deliverables:**

- `kubectl exec`-based check in the running pod: `env | grep DATABASE_URL` and confirm what the process actually sees.
- `kubectl get configmap application-environment-variables -n cqc -o yaml` to see what the cross-service ConfigMap supplies.
- A one-paragraph update to ADR 0008 / ADR 0009 explaining the actual resolution mechanism.

**Exit criteria:**

- We can state, with `kubectl` evidence, *which* `DATABASE_URL` value the app uses in prod and *why*.
- Either the inline hardcoded URL is removed (if the ConfigMap supplies it) or the Service name is corrected (if the hardcoded URL is what's actually used).

---

### WS2 — Verify: does `db.create_all()` reliably set up the schema on first deploy? (S)

**Status:** Open. Source: ADR 0002 §Consequences.

The Dockerfile's `CMD ["python", "app.py"]` causes `create_tables()` (`app.py:612-617`) to run on every container start. `db.create_all()` is `CREATE TABLE IF NOT EXISTS`, so it's idempotent against an existing schema, but it does **not** add new columns to existing tables. This means any column added to `model.py` after the first prod import lands silently as a "missing column" error at query time, not at startup.

**Deliverables:**

- Confirm the current prod table set matches `model.py` (compare `\d+ <table>` output against the model).
- Write a one-line note in the next ADR-0002 amendment with the steps for adding a new column safely *without* Alembic, until Alembic arrives.

**Exit criteria:**

- We know whether prod schema is already drifted from `model.py`. If yes → that becomes WS-X in a follow-up plan.

---

### WS3 — Fix: rotate `SECRET_KEY` away from the literal placeholder (S)

**Status:** Open. Source: ADR 0003 §Consequences.

`app.py:14` is `app.config['SECRET_KEY'] = 'your-secret-key-here'`. Acceptable today because nothing in the app uses sessions, but a footgun the moment any auth, flash-message persistence, or CSRF protection is wired in.

**Deliverables:**

- Read `SECRET_KEY` from env (`os.getenv('SECRET_KEY')`) with a fail-loud default (`raise RuntimeError` if missing in production, generate-and-warn in dev).
- Add `SECRET_KEY` to the `application-environment-variables` ConfigMap pipeline (or a dedicated `Secret`).
- Add `SECRET_KEY` to `.env` and `run.sh`'s generated `.env` with a clear "rotate this" comment.

**Exit criteria:**

- `grep "your-secret-key-here" .` returns nothing in code; deploy still works.

---

### WS4 — Fix: rotate Postgres credentials away from `darwinist:darwinist` (S)

**Status:** Open. Source: ADR 0009 §Decision-3, §Consequences.

Currently identical between local dev (`.env`, `run.sh`) and prod (`k8s/postgres.yaml:13-15`). Plus the `DATABASE_URL` in `k8s/deployment.yaml:30` is hardcoded inline rather than read from the Secret.

**Deliverables:**

- Generate a real password (random ≥24 chars), update `postgres-secret-cqc` in the cluster.
- Change `k8s/deployment.yaml` to read `DATABASE_URL` from `postgres-secret-cqc` (or compose it from `secretKeyRef:` env vars), removing the hardcoded URL.
- Keep `darwinist:darwinist` in `.env` / `run.sh` for local dev only; document the separation in `CLAUDE.md`.

**Exit criteria:**

- `grep "darwinist:darwinist" k8s/` returns nothing.
- Deployment continues to roll out green.

---

### WS5 — Decide: wire up `google_login.py` or delete it (M)

**Status:** Open. Source: ADR 0003 §Context.

`google_login.py` imports `app.telemetry` (`from app.telemetry import traced_function, get_tracer`) which does not exist. It is not imported from `app.py`. It carries OpenTelemetry-decorated functions and reads `GOOGLE_OAUTH2_CONFIG_B64`. This is aspirational scope from a prior attempt.

This is a *fork in the road*, not a simple debt fix:

- **Path A: delete it.** Cheapest. Reclaim the file as future work when auth is genuinely needed.
- **Path B: wire it up.** Means committing to: (a) a telemetry layer (`app/telemetry.py`), (b) blueprinting the login routes into `app.py`, (c) replacing `SECRET_KEY` (see WS3), (d) deciding what gets gated behind auth.

If Path B, this should spawn an `auth` plan and a `0011-google-oauth.md` ADR before code lands. If Path A, just delete + a one-line note that auth was aspirational and is now out of scope.

**Deliverables:**

- A short ADR (`0011-…md`) recording the choice, whichever path is taken.
- Either: `git rm google_login.py` + closing this WS, or a new `docs/plans/auth.md` opening to scope the wire-up.

**Exit criteria:**

- Either `google_login.py` is gone, or it's wired and tested end-to-end.

---

### WS6 — Reshape the `Contact` placeholder for its actual purpose (M)

**Status:** Open. Source: ADR 0001 Amendment (2026-05-19).

Originally scoped as "delete the legacy `Contact` table". That was based on a misreading of the model — see the amendment on ADR 0001. **`Contact` is a deliberate placeholder for future CRM-style interaction tracking** (recording conversations, outreach, notes against providers / facilities), not a legacy artefact to remove.

The current field shape is wrong for the intended purpose (it mirrors the old flat Provider+Location row). A proper CRM `Contact` would carry interaction-specific fields:

- `contacted_at` (timestamp)
- `channel` (email / phone / in-person / etc.)
- `notes` (free text)
- `outcome` (categorised)
- FK to `Provider` and / or `Facility`
- FK to the user who recorded the interaction (depends on WS5 — auth)

This WS is now scoped to: **decide when to reshape `Contact` and what the minimum useful schema looks like**.

**Deliverables:**

- Update `model.py:63`'s misleading comment immediately (one-line fix): change `# Keep Contact for backward compatibility during migration` → `# Placeholder for future CRM-style interaction tracking (see ADR 0001 Amendment 2026-05-19).`
- A `docs/plans/crm-contacts.md` plan when the CRM feature is scoped, covering: target schema, UI surface, how it interacts with auth (WS5), how it gets migrated in (WS7 / Alembic).
- A `docs/adr/0011-crm-contact-model.md` when the schema decision is made.

**Exit criteria:**

- The misleading comment in `model.py:63` is corrected.
- The deliberate placeholder is documented so no future contributor (or Claude session) tries to delete it again.
- A separate plan exists when the CRM work is actually picked up.

---

### WS7 — Decide: when does Alembic come in? (M)

**Status:** Open. Source: ADR 0002 §Walk-back, ADR 0001 §Walk-back.

ADR 0002 calls the absence of Alembic a *phase*, with specific walk-back triggers: a non-additive schema change, dataset size past "re-import in an hour", or a second deployment target.

This WS is to **commit to a trigger**, not to add Alembic now. The output is a short ADR amendment that names the trigger explicitly so future-us can't drift past it without noticing.

**Deliverables:**

- Amendment to ADR 0002 (inline `## Amendment` block) committing to one of: (a) Alembic before the next non-additive change, (b) Alembic when dataset > N rows, (c) Alembic by date Y. Pick one.

**Exit criteria:**

- ADR 0002 has a `Status: Accepted. Walk-back trigger committed YYYY-MM-DD` line.

---

### WS8 — Fix: pin the Dockerfile base image and dependencies (S)

**Status:** Open. Source: ADR 0008 §Consequences.

`Dockerfile:1` is `FROM python` — no tag. `requirements.txt` is bare names (no `==` pins). Build cache hides this most of the time; a cache miss could pick up an incompatible Python or library version silently.

**Deliverables:**

- Pin to `FROM python:3.12-slim` (or whatever the team uses locally — confirm first).
- Generate a pinned `requirements.txt` from a fresh `pip freeze` in the current working venv.
- Verify the image still builds and the app starts.

**Exit criteria:**

- Both `Dockerfile` and `requirements.txt` show explicit versions; image build is reproducible.

---

### WS9 — Fix: protect CSVs from line-ending corruption (XS)

**Status:** Open. Source: ADR 0007 §Consequences, §Walk-back.

`.gitattributes:2` is `* text=auto`. A Windows commit could re-line-end-normalise the 50 MB of checked-in CSVs and produce silently-broken imports.

**Deliverables:**

- Add `*.csv binary` to `.gitattributes` (before the wildcard rule, so it wins).

**Exit criteria:**

- `git check-attr text *.csv` returns `text: unset`.

---

### WS10 — Decide: backup story for in-cluster Postgres (M)

**Status:** Open. Source: ADR 0009 §Consequences, §Walk-back.

Today's de-facto disaster recovery is "re-run the importers from the CSVs in the repo". This is true *only* as long as Postgres holds no data that isn't already in the CSVs. The moment user-generated state lands (auth via WS5, saved filters, notes on facilities), this assumption silently breaks.

**Deliverables:**

- Confirm: is there any non-CSV-derived data in Postgres today? (`SELECT` against `provider` / `facility` for rows with no matching CQC ID.)
- If no: write a one-line note in ADR 0009 confirming the importers-as-DR assumption holds today, and a sentinel: "the moment WS5 (or any auth feature) lands, this WS reopens with backup scope".
- If yes: scope a `CronJob` running `pg_dump` to Azure Blob, with a retention policy.

**Exit criteria:**

- Either ADR 0009 has the "importers-as-DR" sentinel, or there is a working scheduled backup.

---

### WS11 — Decide: do we need a staging environment? (M)

**Status:** Open. Source: ADR 0008 §Consequences.

Every push to `main` lands in prod. Acceptable when the audience is internal and the rollback path (`kubectl set image` to a prior SHA) is fast. Worth a deliberate decision rather than the current "no one set one up".

**Deliverables:**

- Decision: stay single-environment, or add a staging namespace (`cqc-staging`) and a `staging` branch that deploys to it.
- If single-environment: ADR amendment to ADR 0008 stating the choice and the rollback drill.
- If staging: scope into a follow-up plan; non-trivial (cert handling, DB seeding, host name choice).

**Exit criteria:**

- ADR 0008 has an explicit decision recorded; not left to "no one set one up".

---

## Phase exit criteria

When all of these are true, this plan closes (`Status: Closed (YYYY-MM-DD)`) and the open items have either shipped, been amended into the relevant ADR, or spawned their own follow-up plan:

- [ ] WS1 — `DATABASE_URL` resolution mechanism documented in ADR 0008/0009.
- [ ] WS2 — Prod-vs-`model.py` schema drift checked.
- [ ] WS3 — `SECRET_KEY` no longer the placeholder.
- [ ] WS4 — Postgres creds rotated; no `darwinist:darwinist` in `k8s/`.
- [ ] WS5 — Either `google_login.py` deleted or auth wired and ADR'd.
- [ ] WS6 — `Contact` class deleted, or explicitly deferred to WS7.
- [ ] WS7 — Alembic trigger committed in ADR 0002 amendment.
- [ ] WS8 — Dockerfile + requirements.txt pinned.
- [ ] WS9 — `*.csv binary` in `.gitattributes`.
- [ ] WS10 — DR sentinel in ADR 0009 or backup CronJob in place.
- [ ] WS11 — Staging decision recorded in ADR 0008.

## References

- [`docs/adr/0001-provider-facility-domain-model.md`](../adr/0001-provider-facility-domain-model.md) — WS6.
- [`docs/adr/0002-postgres-sqlalchemy-no-migrations.md`](../adr/0002-postgres-sqlalchemy-no-migrations.md) — WS2, WS7.
- [`docs/adr/0003-server-rendered-flask-jinja.md`](../adr/0003-server-rendered-flask-jinja.md) — WS3, WS5.
- [`docs/adr/0007-csvs-checked-into-repo.md`](../adr/0007-csvs-checked-into-repo.md) — WS9.
- [`docs/adr/0008-aks-envsubst-deploy.md`](../adr/0008-aks-envsubst-deploy.md) — WS1, WS8, WS11.
- [`docs/adr/0009-in-cluster-postgres.md`](../adr/0009-in-cluster-postgres.md) — WS1, WS4, WS10.
