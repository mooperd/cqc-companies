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
DEPLOY_DOMAIN=crm.darwinist.io   # optional; or pass as the CLI arg
```

You also need a **domain** to point at the box — Caddy issues TLS for it via
Let's Encrypt, so the A record must resolve to the box before HTTPS works.

## Run

```sh
./deploy/provision.sh                    # domain from DEPLOY_DOMAIN in .env.local
./deploy/provision.sh crm.darwinist.io    # or pass it explicitly
```

That will:
1. upload `~/.ssh/id_ed25519.pub` to the project (as `cqc-companies-key`),
2. create a `cx23` Ubuntu 24.04 server running `cloud-init.yaml`,
3. wait for cloud-init to finish (installs PG18 + deps — up to ~15 min),
4. write the real Caddyfile for your domain with generated basic-auth,
5. rsync + restore the newest local `darwinist-dump-*.sql.gz` (full enriched DB),
6. print the URL, basic-auth credentials, and the IP to point DNS at.

**Point the A record** (`crm.darwinist.io → <printed IP>`) so Caddy can issue the
certificate. Re-running the script is safe — it reuses the existing server and
skips the dump restore (unless `FORCE_RESTORE=1`).

## Common overrides

```sh
SERVER_TYPE=cpx21 LOCATION=hel1 ./deploy/provision.sh crm.darwinist.io
BASIC_AUTH_USER=rob BASIC_AUTH_PASS='choose-your-own' ./deploy/provision.sh crm.darwinist.io
FORCE_RESTORE=1 ./deploy/provision.sh crm.darwinist.io     # re-restore the dump
```

## Operate

```sh
ssh root@<ip> journalctl -u cqc -f        # app logs
ssh root@<ip> systemctl status caddy      # TLS / proxy
ssh root@<ip> 'sudo -u postgres psql darwinist -c "\dt"'   # DB
```

## Backups

The deployed DB is the **authoritative home** for scraped LinkedIn `Person`/`Role`
data ([ADR 0019](../docs/adr/0019-scraped-data-lives-in-deployed-db.md)) — it can't
be regenerated, so the box is a single point of loss. `deploy/backup.sh` (scheduled
daily by the `cqc-backup.timer` systemd unit) takes an **encrypted, offboard**
backup via [BorgBackup](https://www.borgbackup.org/) to a **Hetzner Storage Box**:
`pg_dump --no-owner` → a `repokey`-encrypted Borg archive → `borg prune`. The
Storage Box holds only ciphertext (PII — [ADR 0017](../docs/adr/0017-gdpr-controller-posture.md)).

**Enabling it is a one-time operator step** (like pointing DNS). Until it's done,
`backup.sh` warns and no-ops — the timer never fails, but you are unprotected:

1. **Provision a Hetzner Storage Box** (~€3.20/mo).
2. **Run `./deploy/provision.sh` once** with no `BORG_REPO` set — it generates the
   box's backup SSH key and **prints its public half**. Add that to the Storage
   Box's `~/.ssh/authorized_keys`.
3. **Set in `.env.local`** (both gitignored, never committed):

   ```sh
   BORG_REPO=ssh://uNNNN@uNNNN.your-storagebox.de/./cqc-companies
   # BORG_PASSPHRASE=...        # optional; if unset, provision.sh generates + PRINTS it
   # BACKUP_KEEP_DAILY=14       # daily archives kept — this window is ALSO the GDPR
   #                            #   erasure lag (an erased person persists in backups
   #                            #   only until the oldest kept archive ages out)
   ```

4. **Re-run `./deploy/provision.sh`** — it writes `/opt/cqc/.backup.env`, persists
   the passphrase, `borg init`s the repo, and takes a first backup to validate.
   **Store the printed passphrase off the box** — without it the backups are
   unrecoverable.

**Retention = the erasure window.** The default `BACKUP_KEEP_DAILY=14` bounds the
[ADR 0017 §5](../docs/adr/0017-gdpr-controller-posture.md) backup-erasure lag to
~14 days: an erased person can persist in backups only until the oldest kept
archive ages out. Raising it buys deeper DR at the cost of a longer lag — a
trade-off, not a free win.

**Operate / restore drill** (on the box):

```sh
bash /opt/cqc/deploy/backup.sh list             # list archives
bash /opt/cqc/deploy/backup.sh check            # verify repo + latest archive
bash /opt/cqc/deploy/backup.sh restore-latest   # restore newest into a THROWAWAY db
ssh root@<ip> 'sudo -u postgres psql darwinist_restore -c "SELECT count(*) FROM person;"'
systemctl list-timers cqc-backup.timer          # next scheduled run
```

`restore-latest` loads into `darwinist_restore` (not the live DB) so you can prove
recoverability without risk. Run it periodically — an unverified backup is a guess.

## Not yet automated

- **App deploy / update path.** cloud-init clones `main` once at first boot; there
  is no "push a new version" story (so the backup units/script land on the *next*
  fresh provision, or a manual `git pull` in `/opt/cqc`). See the plan.
- **API keys.** The serving app doesn't need CQC / Companies House keys (it only
  reads the DB). To run enrichment *on the box*, add those to `/opt/cqc/.env`.
