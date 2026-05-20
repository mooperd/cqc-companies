# ADR 0009 — In-cluster PostgreSQL on a `managed-csi` PVC

**Status:** Withdrawn (2026-05-20). See Amendment below.

**TL;DR.** In the context of needing a Postgres for the deployed app without standing up Azure Database for PostgreSQL or any managed service, we chose to run a single Postgres 15 pod in the `cqc` namespace backed by a 10 Gi `managed-csi` (Azure Disk) PVC with `Recreate` rollout strategy, accepting that this is a single point of failure with no backup automation in exchange for zero managed-service spend and a self-contained deploy.

## Amendment (2026-05-20)

**Withdrawn without replacement.** Withdrawn alongside [ADR 0008](0008-aks-envsubst-deploy.md) — the in-cluster Postgres was part of the AKS deploy story that's being removed. The `k8s/postgres.yaml` manifest has been deleted.

Postgres still exists for local development and CI, but now via Docker (compose for local, service container for CI) rather than as a deployed pod. That's the foundation phase, not a "deploy story" — see the forthcoming `docs/plans/foundation.md`.

The "next-Postgres-target" decision is deferred to the same future-deploy ADR that supersedes 0008. Likely candidates when that time comes: a managed Postgres on whatever the new compute target is; an external managed Postgres reachable from multiple deploys; or — if the data stays small — back to a single-pod-with-volume model on the new target.

The original Context, Decision, Alternatives, Consequences, and Walk-back sections below are retained for the historical record. They describe a setup that no longer exists.

## Context

The dataset is small and the app is internal; the bar is "Postgres survives a pod restart". Azure Database for PostgreSQL would cost real money and add a managed-service surface to keep credentials and networking aligned with. A StatefulSet would be marginally more correct than a Deployment with `Recreate`, but the practical difference at one replica is zero — and the Deployment form keeps `kubectl get all` output simpler.

The credentials are stashed in a namespace-scoped `Secret` (`postgres-secret-cqc`) but the values are the literal string `darwinist` for DB, user, and password — same as the local-dev defaults in `.env` and `run.sh`. This is consistent with the project's "single deploy, no separation of concerns" posture but is a real risk once anything other than a public CQC export lands in the database.

## Decision

1. **`postgres:15` image** in a single-replica `Deployment` with `strategy.type: Recreate` (`k8s/postgres.yaml:30-46`). On rollout, the old pod is killed before the new one starts — required because the PVC is `ReadWriteOnce`.
2. **10 Gi PVC** named `postgres-pvc-cqc`, `storageClassName: managed-csi` (Azure Disk). Mounted at `/var/lib/postgresql/data` with `PGDATA=/var/lib/postgresql/data/pgdata` so the data lives in a subdir.
3. **`postgres-secret-cqc`** holds DB / user / password, all currently the literal `darwinist`. Wired into the Pod via `valueFrom: secretKeyRef:`.
4. **`pg_isready`-based liveness + readiness probes** (`k8s/postgres.yaml:70-80`), so the app pod's startup waits naturally on Postgres availability via DNS resolution of `postgres-service-cqc`.
5. **`ClusterIP` Service** named `postgres` (`k8s/postgres.yaml:90-99`). The app's `DATABASE_URL` in `k8s/deployment.yaml` references `postgres-service-cqc` — a name that doesn't match the Service object. **This is a smell** ([ADR 0008](0008-aks-envsubst-deploy.md) lists it under Consequences); it works only if the cluster has some other resolution mechanism in play. Worth investigating.

## Alternatives considered

- **Azure Database for PostgreSQL (managed).** Rejected for cost and added surface area at current scope. Strong walk-back candidate if downtime tolerance changes.
- **StatefulSet.** Rejected: one replica, one PVC — the StatefulSet gives no real benefit beyond a name; the additional concepts (headless service, ordinal pods) add noise.
- **SQLite on a PVC.** Rejected: the queries use `ilike`, `case`, server-side `func.sum` — SQLite would change behaviour subtly enough to make local-vs-prod parity worse.
- **Postgres on a `hostPath` or ephemeral volume.** Rejected: no durability across pod restarts.

## Consequences

- Postgres goes down with the pod. A node-eviction or PV blip = downtime. For an internal directory this is acceptable; for a customer-facing service it would not be.
- **No backup automation.** `pg_dump` is not scheduled. The CSV importers ([ADR 0005](0005-two-stage-csv-ingest.md)) are effectively the disaster recovery story — re-import from CSV reconstitutes the dataset. Acceptable only as long as the database holds no data that isn't in the CSVs.
- Credentials are weak (literal `darwinist`) and identical to local-dev defaults. **Must change** before any auth-bearing or user-PII data lands.
- The `DATABASE_URL` references `postgres-service-cqc` but the Service is named `postgres` — a probable misname in `k8s/deployment.yaml:30`. The system either works because the wrong name resolves anyway (unlikely) or the deploy as-written would fail to reach Postgres. Verify on next deploy.
- `Recreate` strategy means brief downtime on every Postgres rollout. Acceptable at this scope.

## Walk-back options

- **If downtime tolerance tightens** — migrate to Azure Database for PostgreSQL; keep the Service name stable so the app's `DATABASE_URL` only needs the env var swapped.
- **If sensitive data lands** — rotate creds to a real generated value held in `postgres-secret-cqc`; mount the same secret into the app pod's `DATABASE_URL` rather than hardcoding (currently hardcoded in `k8s/deployment.yaml:30`).
- **If backups become required** — schedule a `CronJob` that runs `pg_dump` to Azure Blob; rotation policy lives there.

## Links

- `k8s/postgres.yaml` — Deployment, Secret, PVC, Service.
- `k8s/deployment.yaml:28-30` — app's `DATABASE_URL` (note the name mismatch).
- [ADR 0008](0008-aks-envsubst-deploy.md) — the deploy workflow that applies this.
- [ADR 0005](0005-two-stage-csv-ingest.md) — the de-facto disaster recovery story.
