# ADR 0018 — Single Hetzner Cloud box: app + self-managed Postgres, cloud-init provisioned

**Status:** Accepted (2026-07-18).

> **Amendment (2026-07-19 — backups are now required, not deferred).**
> [ADR 0019](0019-scraped-data-lives-in-deployed-db.md) makes this box the
> **authoritative** home for scraped LinkedIn `Person`/`Role` data — non-regenerable
> state. That trips the "If the DB starts holding non-rebuildable state" walk-back
> trigger below: "just rebuild it" is no longer a recovery path for those rows, so
> **automated backups (Hetzner snapshots or `pg_dump` → object storage, with a
> retention window) move from deferred to required.** Tracked as the immediate
> follow-up in [the plan](../plans/linkedin-ingestion.md); until it lands, the box
> is a single point of loss for the scraped rows.

**TL;DR.** In the context of needing a hosted home for the app and its ~126 MB
Postgres database after the AKS deploy was withdrawn ([ADR 0008](0008-aks-envsubst-deploy.md),
[ADR 0009](0009-in-cluster-postgres.md)), facing a single low-traffic service
and a "cheapest that isn't a toy" constraint, we chose **one Hetzner Cloud VPS
(cx23, ~€5.49/mo + ~€0.50 IPv4) running both the Flask app (gunicorn behind Caddy) and a
self-managed PostgreSQL 18** — provisioned by a static, secret-free
`cloud-init.yaml` plus an idempotent `deploy/provision.sh` — accepting that we
own OS/Postgres patching and backups ourselves rather than paying a managed
provider for them.

## Context

The next deploy target was explicitly left open when Azure/AKS was withdrawn on
2026-05-20. The requirement that reopened it: a hosted place to run the app and
its database, chosen on cost.

The relevant facts about *this* workload:

- **Tiny, mostly-read data.** 126 MB, ~350k rows (facility 57k, person 96k,
  role 156k, provider 37k). It grows slowly.
- **Largely rebuildable.** The provider/facility base is re-derivable from the
  public CQC CSVs checked into the repo ([ADR 0007](0007-csvs-checked-into-repo.md))
  via `import_records.py` + `enrich_locations.py`. Only the LinkedIn /
  Companies House enrichment (`person`/`role`) is *not* CSV-derivable — it lives
  in a local, gitignored `pg_dump`.
- **One service, no fleet.** No multi-environment, no staging, no drift concern
  ([ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) still governs schema).
- **Phase 6 will need a public HTTPS endpoint** anyway (the WhatsApp inbound
  webhook), so a public box with TLS is the shape we're heading to regardless.

Managed Postgres (Neon/Supabase free tiers, or paid DO/Railway) was the
genuinely-cheapest option at $0, but it only hosts the *database* — we still need
somewhere to run the app, and we'd be paying (in cold-starts or dollars) for
durability guarantees this rebuildable dataset doesn't need. One box that runs
both is cheaper end-to-end and removes the app/DB split.

## Decision

1. **One Hetzner Cloud VPS.** Default `cx23` (2 vCPU / 4 GB / 40 GB SSD,
   ~€5.49/mo + ~€0.50/mo for the IPv4), Ubuntu 24.04, in `nbg1`. Server
   type/location are `provision.sh` env overrides. (`cx23` is the current
   entry Intel line; the older `cx22` this ADR first named was already retired
   from the account — `cax11` ARM is similar money but risks Python-wheel
   surprises, so x86 wins.)
2. **App + Postgres co-located.** Postgres listens on `127.0.0.1` only; gunicorn
   binds `127.0.0.1:5000`; **Caddy** terminates TLS (automatic Let's Encrypt)
   and reverse-proxies gunicorn, with **HTTP basic-auth** in front because the
   app itself still has no auth ([ADR 0011](0011-defer-authentication.md)). Only
   22/80/443 are open (ufw); 5432 and 5000 never leave loopback.
3. **PostgreSQL 18 from the PGDG apt repo**, not Ubuntu 24.04's default PG16 —
   so the box matches the local Homebrew PG18 that produces our dumps. This
   removes cross-version restore failures (`transaction_timeout`, the `\restrict`
   pg_dump-18 meta-command) that a PG16 target would hit.
4. **Provisioning = static cloud-init + idempotent script.**
   `deploy/cloud-init.yaml` contains **no secrets and no per-deploy values**; it
   clones the public repo, installs PG18 + deps, generates the DB password and
   `SECRET_KEY` on-box, and enables gunicorn + Caddy (the schema is created by
   the app's `db.create_all()` at service start). It loads **no data** —
   `deploy/provision.sh` (needs `HCLOUD_TOKEN`) creates the key/server if absent,
   waits for cloud-init, injects the domain + basic-auth into the Caddyfile, and
   then performs the single data load.
5. **Data load owned entirely by `provision.sh`, one decision, no wasted work.**
   Dump present → restore the full enriched DB (rewriting object ownership to the
   app role with a *role-agnostic* rewrite, so a dump from any machine/CI
   restores cleanly). No dump → seed provider/facility base data from the repo
   CSVs on the box (no LinkedIn/CH enrichment). cloud-init deliberately does not
   seed, so the restore path never builds-then-discards a multi-minute import.
6. **`FLASK_ENV=production`** on the box, so the app refuses to boot without a
   real `SECRET_KEY` (which cloud-init generates into `/opt/cqc/.env`).

## Alternatives considered

- **Managed Postgres free tier (Neon/Supabase) + app hosted elsewhere** —
  rejected as the *primary*: $0 for the DB but still needs an app host, adds a
  network hop, and pays in cold-starts/limits for durability this rebuildable
  data doesn't need. It remains the right call the day the DB holds
  *non*-rebuildable state (hand-entered outreach) — see Walk-back.
- **Managed Postgres paid (DO/Railway, ~$15–19/mo)** — rejected on cost for a
  126 MB read-mostly DB; the managed backups/HA are not worth it yet.
- **Hetzner default PG16** — rejected: our dumps are PG18; restoring 18→16 fails
  on newer GUCs/meta-commands. Matching versions is cheaper than sanitising
  every dump.
- **Docker Compose on the box** — rejected as premature: one app + one Postgres
  is served more simply by systemd + the system Postgres than by adding a
  container runtime. Revisit if a second service lands.
- **Re-run enrichment on the box instead of restoring the dump** — rejected:
  needs the Phantombuster/Companies House credentials and is slow; a dump
  restore is ~seconds and reproduces the exact local state.

## Consequences

- **~€6/mo all-in, always-on, no cold starts.** Cheapest realistic option that also
  hosts the app, and it's the public-HTTPS shape Phase 6 needs.
- **We own patching and backups.** No managed provider does it for us. Mitigated
  today by the data being largely rebuildable (CSVs + dump), but there is **no
  automated backup yet** — that is deferred and called out as a Walk-back
  trigger, not solved here.
- **Basic-auth, not real auth.** A stopgap consistent with ADR 0011; it keeps the
  no-auth app off the open internet but is not user-level authz.
- **Postgres is self-managed on the box.** ADR 0002's `create_all()` schema story
  is unchanged; the Alembic trigger it defines still applies.
- **cloud-init runs once.** Re-converging the box (new domain, dump refresh) is
  `provision.sh`'s job; changing what the *base image* does means editing
  cloud-init and rebuilding the server.
- **Secrets stay off the repo.** Domain, basic-auth, DB password, and
  `SECRET_KEY` are injected/generated at deploy time; the committed files are
  safe to be public.

## Walk-back options

- **If the DB starts holding non-rebuildable state** (hand-entered outreach,
  interaction history that isn't re-derivable from CSVs/dump) — that is the
  trigger to add real backups (Hetzner snapshots or `pg_dump` to object storage)
  and reconsider managed Postgres, since "just rebuild it" stops being a recovery
  path.
- **If a second service or environment appears** — revisit the systemd-on-one-box
  shape; Compose or a small orchestrator may then earn its keep. A new ADR
  supersedes this one and should reference [ADR 0008](0008-aks-envsubst-deploy.md).
- **If the box is under-resourced** — `SERVER_TYPE=` bumps it; the workload is
  vertical-scale-friendly at this size.
- **If Phase 6's webhook needs more than basic-auth in front** — replace the
  Caddy basic-auth block when the app grows real auth (walks back part of ADR
  0011 too).

## Links

- `deploy/cloud-init.yaml` — the static, secret-free box definition.
- `deploy/provision.sh` — idempotent create + finalise + restore.
- `deploy/README.md` — run instructions.
- [`docs/plans/hetzner-deploy.md`](../plans/hetzner-deploy.md) — provisioning steps + open follow-ups.
- [ADR 0008](0008-aks-envsubst-deploy.md), [ADR 0009](0009-in-cluster-postgres.md) — the withdrawn AKS deploy this replaces.
- [ADR 0002](0002-postgres-sqlalchemy-no-migrations.md) — schema management (unchanged).
- [ADR 0011](0011-defer-authentication.md) — why basic-auth stands in for app auth.
- [ADR 0007](0007-csvs-checked-into-repo.md) — the public CSVs the CSV-seed path uses.
- [`docs/product-vision.md`](../product-vision.md) — Phase 6 reopens the public-endpoint question.
