# Plan — Hetzner Cloud deploy

**Status:** In progress (2026-07-18). Decision recorded in
[ADR 0018](../adr/0018-hetzner-single-box-deploy.md); provisioning artifacts
live in [`deploy/`](../../deploy/).

The *why* is in the ADR. This plan is the *what next, in what order*.

## Goal

A hosted home for the app + its Postgres DB on one cheap Hetzner box, reproducibly
provisioned, with the full LinkedIn/Companies-House-enriched DB loaded.

## Done

- [x] ADR 0018 — single-box Hetzner decision.
- [x] `deploy/cloud-init.yaml` — static, secret-free first-boot setup (PG18,
      deps, CSV-seeded base DB, gunicorn + Caddy, ufw).
- [x] `deploy/provision.sh` — idempotent create + finalise Caddy + one-time
      enriched-dump restore.
- [x] `gunicorn` added to `requirements.txt`.
- [x] `deploy/README.md` — run + operate instructions.
- [x] **Backups mechanism** — `deploy/backup.sh` (encrypted Borg → Hetzner Storage
      Box, `keep-daily=14`, restore drill) + `cqc-backup.{service,timer}` units in
      cloud-init + provision.sh wiring. Required by
      [ADR 0019](../adr/0019-scraped-data-lives-in-deployed-db.md) once the DB
      became the authoritative home for scraped data. **Operator step remains:**
      provision a Storage Box, authorise the box's backup key, set `BORG_REPO`.

## Next up (operator actions — needs a Hetzner token + a domain)

1. **Provision.** `export HCLOUD_TOKEN=...`, pick a domain, run
   `./deploy/provision.sh <domain>`.
2. **DNS.** Point an A record at the printed IP so Caddy can issue TLS.
3. **Verify** end-to-end (see command below): HTTPS + basic-auth reach the app,
   the statistics page renders, and the enriched tables are present.
4. **Store the basic-auth password** the script generated (or set your own via
   `BASIC_AUTH_PASS=`).

## Open follow-ups (deferred, tracked here so they aren't lost)

- **Backups — mechanism built (see Done); operator step + first real run remain.**
  The trigger tripped (ADR 0019: the DB is now the authoritative home for scraped
  data). What's left is operational: provision a Storage Box, authorise the box's
  backup key, set `BORG_REPO`, re-run provision.sh, and **verify the restore drill**
  (`backup.sh restore-latest`) actually round-trips against the live box — the code
  is untested against a real Storage Box until then.
- **App deploy / update path.** cloud-init clones `main` once at first boot.
  There is no "push a new version" story yet — today it's `ssh` in, `git pull`
  in `/opt/cqc`, `pip install -r`, `systemctl restart cqc`. Formalise if updates
  get frequent (a small `deploy/update.sh`, or CI on push to `main`).
- **Real auth (Phase 6).** Basic-auth is a stopgap ([ADR 0011](../adr/0011-defer-authentication.md)).
  The WhatsApp webhook needs a public unauthenticated path anyway, which forces
  the auth question — fold into the Phase 6 work.
- **Postgres tuning.** Defaults are fine at 126 MB; revisit only if the box is
  resized or the DB grows an order of magnitude.

## Verification command

After provisioning + DNS, from the box:

```sh
# schema + enriched-row presence (proves the dump restore, not just CSV seed)
ssh root@<ip> 'sudo -u postgres psql darwinist -c \
  "SELECT (SELECT count(*) FROM person) AS people, (SELECT count(*) FROM role) AS roles, (SELECT count(*) FROM facility) AS facilities;"'
```

and from anywhere:

```sh
curl -u <user>:<pass> -sSf https://<domain>/healthz    # -> OK
```
