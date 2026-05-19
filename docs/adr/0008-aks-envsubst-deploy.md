# ADR 0008 — AKS deploy via envsubst-templated manifests

**Status:** Accepted (2025-08-29).

**TL;DR.** In the context of deploying this Flask app to an existing Azure Kubernetes Service cluster shared with other `darwinist.io` services, facing a single deployment target and no existing tooling investment, we chose to template the four k8s manifests with shell-level `envsubst` from inside a single GitHub Actions workflow, accepting that this scales poorly to multi-environment / multi-tenant deploys but is dead-simple to read and audit.

## Context

The deployment target is one AKS cluster, one namespace (`cqc`), one image (built from `Dockerfile`). The four manifests (`namespace.yaml`, `deployment.yaml`, `service.yaml`, `ingress.yaml`) need only a handful of substitutions: the image tag (`${BUILD_IMAGE}`), the service name (`${SERVICE_NAME}` = repo basename), and the wildcard cert name (`${CERT_NAME}`).

Helm and Kustomize would both work, but both bring concepts (charts, kustomizations, overlays) that the project doesn't need. The team is small and the manifest churn is low; the cost of "what does this template render to?" is borne well by a literal `cat … | envsubst | kubectl apply -f -`.

The certs are managed at the cluster level (cert-manager + a wildcard for `*.darwinist.io`). The workflow copies the wildcard secret from `default` into the `cqc` namespace each deploy (`gh-actions/main.yml:93-96`) — a known footgun but consistent with how the sibling darwinist.io services do it.

## Decision

1. The `.github/workflows/main.yml` workflow on every push to `main`:
   1. Builds the image and pushes to ACR (`${AKS_RESOURCE_GROUP}.azurecr.io/${repo-name}:${short-sha}`).
   2. Logs in to AKS via `azure/login` + `az aks get-credentials`.
   3. Applies the four `k8s/*.yaml` manifests through `envsubst`.
   4. Copies the wildcard cert secret from `default` into `cqc`, rewriting its namespace with `jq`.
   5. Waits on `kubectl rollout status`.
2. The image tag is `${SHORT_SHA}` (first 8 chars of `GITHUB_SHA`). No `latest` tag; no semantic versioning yet.
3. The host is hardcoded as `cqc.darwinist.io` in `ingress.yaml`. There is no PR / staging environment.
4. The shared, default-namespace ConfigMap `application-environment-variables` is mirrored into `cqc` on each deploy by piping through `jq` to strip metadata (`gh-actions/main.yml:86-88`). This is how cross-service env vars (e.g. observability endpoints) are propagated.

## Alternatives considered

- **Helm chart.** Rejected at this scope: a chart for four manifests with three substitutions is paperwork. Worth revisiting if the manifest set grows beyond ~8 files or a second deployment target appears.
- **Kustomize.** Rejected for similar reasons; also, the substitutions are runtime values (image tag) rather than overlay deltas.
- **ArgoCD / Flux (GitOps).** Rejected: requires standing up the controller; no current need for drift detection or multi-cluster sync.
- **`kubectl apply -k` with Kustomize patches.** Rejected; same reasoning as raw Kustomize.

## Consequences

- The deploy is one workflow, one pass; CI logs are the single source of truth for what was applied. Easy to read.
- There is no preview / staging environment. Every merge to `main` lands in prod. The image tag is the SHA, so rollback is `kubectl set image ... :<old-sha>` — feasible but manual.
- The cert-copy step couples `cqc` namespace's TLS to whatever the wildcard happens to be in `default` at apply time. A cert rotation in `default` doesn't propagate to `cqc` until next deploy.
- The `Dockerfile` uses `FROM python` (no version pin). The build cache (`type=gha`) hides this most of the time, but it's a real reproducibility hole.
- The deployment's `DATABASE_URL` is hardcoded inline in `k8s/deployment.yaml` (`postgresql://darwinist:darwinist@postgres-service-cqc:5432/darwinist`) rather than read from the namespace's Postgres secret. See [ADR 0009](0009-in-cluster-postgres.md) — both ADRs share the smell.
- No healthcheck pre-flight beyond Kubernetes probes; CI succeeds the moment `rollout status` returns 0. A start-up failure (e.g. DB unreachable) shows up as a CrashLoopBackOff, not a CI red.

## Walk-back options

- **If a staging environment is needed** — split the manifests' image-tag/host substitutions into a per-env file, keep `envsubst`; or move to Helm if the per-env knobs grow.
- **If multiple services need to share the cert-copy hack** — promote it to a cluster-level cert-manager `ClusterIssuer` + `Certificate` rather than copying secrets manually.
- **If reproducibility bites** — pin the `Dockerfile` base image (`FROM python:3.12-slim`) and pin `requirements.txt` versions.

## Links

- `.github/workflows/main.yml` — the deploy workflow.
- `k8s/*.yaml` — the four envsubst-templated manifests.
- [ADR 0009](0009-in-cluster-postgres.md) — the in-cluster Postgres these manifests assume.
