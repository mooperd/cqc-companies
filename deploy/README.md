# deploy/ — Hetzner Cloud provisioning

One-box deploy for cqc-companies: the Flask app (gunicorn) + PostgreSQL 18 +
Caddy (TLS + basic-auth) on a single Hetzner Cloud VPS. See
[ADR 0018](../docs/adr/0018-hetzner-single-box-deploy.md) for *why* and
[`docs/plans/hetzner-deploy.md`](../docs/plans/hetzner-deploy.md) for the wider
plan / open follow-ups.

## Files

| File | Role |
|------|------|
| `cloud-init.yaml` | **Static, secret-free.** First-boot setup: PG18 (PGDG), venv + deps, clone repo, seed base DB from the public CSVs, gunicorn + Caddy under systemd, ufw. Runs once. |
| `provision.sh` | **Idempotent.** Creates the SSH key + server if absent, waits for cloud-init, finalises the Caddyfile (domain + basic-auth), and restores the local enriched dump once. |

## Prerequisites

```sh
brew install hcloud                        # Hetzner CLI (already installed here)
```

`provision.sh` reads config from (highest precedence first) the shell
environment, then `.env.local`, then `.env` — both gitignored. Put your token
(and optionally the domain) in `.env.local`:

```sh
# .env.local  (gitignored)
HCLOUD_TOKEN=...          # Hetzner Cloud API token, read+write
DEPLOY_DOMAIN=cqc.example.com   # optional; or pass as the CLI arg
```

You also need a **domain** to point at the box — Caddy issues TLS for it via
Let's Encrypt, so the A record must resolve to the box before HTTPS works.

## Run

```sh
./deploy/provision.sh                    # domain from DEPLOY_DOMAIN in .env.local
./deploy/provision.sh cqc.example.com    # or pass it explicitly
```

That will:
1. upload `~/.ssh/id_ed25519.pub` to the project (as `cqc-companies-key`),
2. create a `cx23` Ubuntu 24.04 server running `cloud-init.yaml`,
3. wait for cloud-init to finish (installs PG18 + deps — up to ~15 min),
4. write the real Caddyfile for your domain with generated basic-auth,
5. rsync + restore the newest local `darwinist-dump-*.sql.gz` (full enriched DB),
6. print the URL, basic-auth credentials, and the IP to point DNS at.

**Point the A record** (`cqc.example.com → <printed IP>`) so Caddy can issue the
certificate. Re-running the script is safe — it reuses the existing server and
skips the dump restore (unless `FORCE_RESTORE=1`).

## Common overrides

```sh
SERVER_TYPE=cpx21 LOCATION=hel1 ./deploy/provision.sh cqc.example.com
BASIC_AUTH_USER=rob BASIC_AUTH_PASS='choose-your-own' ./deploy/provision.sh cqc.example.com
FORCE_RESTORE=1 ./deploy/provision.sh cqc.example.com     # re-restore the dump
```

## Operate

```sh
ssh root@<ip> journalctl -u cqc -f        # app logs
ssh root@<ip> systemctl status caddy      # TLS / proxy
ssh root@<ip> 'sudo -u postgres psql darwinist -c "\dt"'   # DB
```

## Not yet automated

- **Backups.** The data is largely rebuildable (public CSVs + the local dump), so
  there is no scheduled backup yet. See ADR 0018 Walk-back — add one the moment
  the DB holds non-rebuildable outreach state.
- **API keys.** The serving app doesn't need CQC / Companies House keys (it only
  reads the DB). To run enrichment *on the box*, add those to `/opt/cqc/.env`.
