#!/usr/bin/env bash
#
# provision.sh — stand up (or re-converge) the Hetzner Cloud box for
# cqc-companies. See ADR 0018 and docs/plans/hetzner-deploy.md.
#
# Idempotent: safe to re-run. It creates the SSH key + server only if absent,
# waits for cloud-init to finish, finalises the Caddyfile (domain + HTTPS +
# basic-auth), and — once, on first success — restores the local pg_dump so the
# box has the full LinkedIn/Companies-House-enriched DB (the public CSVs that
# cloud-init seeds from cannot reproduce that enrichment).
#
# Config is read from (highest precedence first):
#   1. the shell environment / CLI arg
#   2. <repo>/.env.local   (gitignored — put HCLOUD_TOKEN etc. here)
#   3. <repo>/.env
#
# Usage:
#   # put HCLOUD_TOKEN (and optionally DEPLOY_DOMAIN) in .env.local, then:
#   ./deploy/provision.sh                     # domain from DEPLOY_DOMAIN
#   ./deploy/provision.sh crm.darwinist.io     # or pass it explicitly
#
# Recognised keys (env var or .env.local line):
#   HCLOUD_TOKEN=...        # Hetzner Cloud API token (read+write) — REQUIRED
#   DEPLOY_DOMAIN=...       # domain for TLS/basic-auth; or pass as $1
#   SERVER_TYPE=cx23        # Hetzner server type (default cx23: 2 vCPU / 4GB)
#   LOCATION=nbg1           # datacentre (nbg1|fsn1|hel1|...)
#   SERVER_NAME=cqc-companies
#   SSH_PUBKEY=~/.ssh/id_ed25519.pub
#   BASIC_AUTH_USER=admin
#   BASIC_AUTH_PASS=...     # generated + printed if unset
#   FORCE_RESTORE=1         # re-restore the dump even if already done once
#
# Backups (ADR 0019 — the deployed DB is the authoritative home for scraped data,
# so offboard encrypted backups are required). Optional here: if BORG_REPO is
# unset, the script generates + prints the box's backup SSH key and instructions,
# and leaves backups pending. Set these (in .env.local) to finish wiring them:
#   BORG_REPO=...          # Storage Box repo, e.g. ssh://uNNNN@uNNNN.your-storagebox.de/./cqc-companies
#   BORG_PASSPHRASE=...    # borg repo passphrase; generated + printed if unset — STORE IT OFF-BOX
#   BACKUP_KEEP_DAILY=14   # daily archives kept (this window is also the GDPR erasure-lag bound)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLOUD_INIT="$SCRIPT_DIR/cloud-init.yaml"

# Load KEY=value lines from a dotenv file WITHOUT clobbering anything already set
# in the environment (so an explicit `export` or CLI value still wins). Simple
# format only: `KEY=value`, optional surrounding quotes, `#` comments, blanks.
load_dotenv() {
  local file="$1" key val
  [ -f "$file" ] || return 0
  while IFS='=' read -r key val; do
    key="${key#"${key%%[![:space:]]*}"}"        # ltrim
    key="${key#export }"; key="${key%"${key##*[![:space:]]}"}"  # strip `export `, rtrim
    case "$key" in ''|'#'*) continue ;; esac
    [ -n "${!key+x}" ] && continue              # already set in env → keep it
    val="${val%$'\r'}"
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    export "$key=$val"
  done < "$file"
}
load_dotenv "$REPO_DIR/.env.local"   # loaded first → wins over .env
load_dotenv "$REPO_DIR/.env"

DOMAIN="${1:-${DEPLOY_DOMAIN:-}}"
SERVER_NAME="${SERVER_NAME:-cqc-companies}"
SERVER_TYPE="${SERVER_TYPE:-cx23}"
IMAGE="${IMAGE:-ubuntu-24.04}"
LOCATION="${LOCATION:-nbg1}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
SSH_KEY_NAME="${SSH_KEY_NAME:-cqc-companies-key}"
BASIC_AUTH_USER="${BASIC_AUTH_USER:-admin}"
BACKUP_KEEP_DAILY="${BACKUP_KEEP_DAILY:-14}"

# --- preflight ---
command -v hcloud >/dev/null || { echo "ERROR: hcloud not installed (brew install hcloud)"; exit 1; }
[ -n "$DOMAIN" ] || { echo "ERROR: no domain — pass it as an argument or set DEPLOY_DOMAIN in .env.local"; exit 1; }
: "${HCLOUD_TOKEN:?ERROR: set HCLOUD_TOKEN (in .env.local or the environment)}"
[ -f "$SSH_PUBKEY" ] || { echo "ERROR: SSH public key not found: $SSH_PUBKEY"; exit 1; }
[ -f "$CLOUD_INIT" ] || { echo "ERROR: cloud-init not found: $CLOUD_INIT"; exit 1; }

SSH_PRIVKEY="${SSH_PUBKEY%.pub}"
SSH_OPTS=(-i "$SSH_PRIVKEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

# Hetzner requires key *fingerprints* to be unique per project. The same public
# key is often already uploaded under a different name — reuse it by fingerprint
# rather than trying to re-create it (which fails with uniqueness_error).
echo "==> Resolving SSH key in Hetzner project"
FINGERPRINT="$(ssh-keygen -l -E md5 -f "$SSH_PUBKEY" | sed -E 's/.*MD5:([0-9a-f:]+).*/\1/')"
SSH_KEY_REF="$(hcloud ssh-key list -o noheader -o columns=fingerprint,name \
  | awk -v fp="$FINGERPRINT" '$1==fp{$1="";sub(/^[ \t]+/,"");print;exit}')"
if [ -n "$SSH_KEY_REF" ]; then
  echo "    reusing existing key: $SSH_KEY_REF"
else
  echo "    uploading new key: $SSH_KEY_NAME"
  hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_PUBKEY"
  SSH_KEY_REF="$SSH_KEY_NAME"
fi

echo "==> Ensuring server '$SERVER_NAME' ($SERVER_TYPE, $IMAGE, $LOCATION)"
if ! hcloud server describe "$SERVER_NAME" >/dev/null 2>&1; then
  hcloud server create \
    --name "$SERVER_NAME" \
    --type "$SERVER_TYPE" \
    --image "$IMAGE" \
    --location "$LOCATION" \
    --ssh-key "$SSH_KEY_REF" \
    --user-data-from-file "$CLOUD_INIT"
else
  echo "    server already exists — reusing"
fi

IP="$(hcloud server ip "$SERVER_NAME")"
echo "==> Server IP: $IP"

echo "==> Waiting for cloud-init to finish (installs PG18 + deps; up to ~15 min)"
deadline=$(( $(date +%s) + 900 ))
until ssh "${SSH_OPTS[@]}" "root@$IP" 'test -f /opt/cqc/.cloud-init-complete' 2>/dev/null; do
  [ "$(date +%s)" -lt "$deadline" ] || { echo "ERROR: timed out waiting for cloud-init"; echo "  inspect: ssh root@$IP 'cloud-init status --long; tail -50 /var/log/cloud-init-output.log'"; exit 1; }
  printf '.'; sleep 15
done
echo " done."

# Resolve a persisted secret idempotently: an explicit value ($1) wins; else reuse
# the copy persisted on the box at $2; else generate `openssl rand -base64 $3`.
# Returns via RESOLVED_SECRET; sets GENERATED=1 when it had to mint a fresh one.
# (bash 3.2-safe — the operator's Mac — so no namerefs: results come back in globals.)
resolve_or_generate_secret() {
  local current="$1" remote_file="$2" rand_bytes="$3"
  RESOLVED_SECRET="$current"; GENERATED=0
  [ -n "$current" ] && return 0
  RESOLVED_SECRET="$(ssh "${SSH_OPTS[@]}" "root@$IP" "cat $remote_file 2>/dev/null" || true)"
  if [ -z "$RESOLVED_SECRET" ]; then
    RESOLVED_SECRET="$(openssl rand -base64 "$rand_bytes")"; GENERATED=1
  fi
}

# --- finalise Caddy (domain + HTTPS + basic-auth) ---
# Resolve the basic-auth password idempotently (explicit → persisted → generated),
# so a re-run never mints a new random password and invalidates the old credentials.
PASS_FILE=/opt/cqc/.basic_auth_pass
resolve_or_generate_secret "${BASIC_AUTH_PASS:-}" "$PASS_FILE" 15
BASIC_AUTH_PASS="$RESOLVED_SECRET"; GENERATED_PASS="$GENERATED"
# Persist (600, root) so subsequent runs — and domain changes — keep this password.
printf '%s' "$BASIC_AUTH_PASS" | ssh "${SSH_OPTS[@]}" "root@$IP" "install -m 600 /dev/stdin $PASS_FILE"
echo "==> Finalising Caddy vhost for https://$DOMAIN (basic-auth user: $BASIC_AUTH_USER)"
HASH="$(ssh "${SSH_OPTS[@]}" "root@$IP" caddy hash-password --plaintext "$BASIC_AUTH_PASS")"
CADDYFILE="$(cat <<EOF
$DOMAIN {
    encode zstd gzip
    basic_auth {
        $BASIC_AUTH_USER $HASH
    }
    reverse_proxy 127.0.0.1:5000
}
EOF
)"
printf '%s\n' "$CADDYFILE" | ssh "${SSH_OPTS[@]}" "root@$IP" 'cat > /etc/caddy/Caddyfile && systemctl reload caddy'

# --- Data load: the single decision lives here (cloud-init loads nothing).
#     Dump present  -> restore the full enriched DB (person/role + CH/LinkedIn).
#     No dump       -> seed provider/facility base data from the repo CSVs. ---
DUMP="$(ls -t "$REPO_DIR"/darwinist-dump-*.sql.gz 2>/dev/null | head -1 || true)"
if [ -n "$DUMP" ]; then
  if [ "${FORCE_RESTORE:-0}" = "1" ] || ! ssh "${SSH_OPTS[@]}" "root@$IP" 'test -f /opt/cqc/.dump-restored' 2>/dev/null; then
    echo "==> Restoring enriched DB from $(basename "$DUMP")"
    rsync -e "ssh ${SSH_OPTS[*]}" --progress "$DUMP" "root@$IP:/tmp/restore.sql.gz"
    ssh "${SSH_OPTS[@]}" "root@$IP" 'bash -s' <<'REMOTE'
set -euo pipefail
systemctl stop cqc
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='darwinist' AND pid <> pg_backend_pid();" >/dev/null
sudo -u postgres dropdb --if-exists darwinist
sudo -u postgres createdb -O darwinist darwinist
# Rewrite object ownership to the app role. The dump is taken off-box by whoever
# runs pg_dump (a superuser like 'roberttaylor' or 'darwinist'), and that role
# may not exist here — a role-agnostic rewrite restores cleanly from any source.
# (Belt-and-braces; `pg_dump --no-owner` at dump time avoids the OWNER lines too.)
gunzip -c /tmp/restore.sql.gz \
  | sed -E 's/ OWNER TO [A-Za-z_][A-Za-z0-9_]*;/ OWNER TO darwinist;/g' \
  | sudo -u postgres psql -v ON_ERROR_STOP=1 darwinist >/dev/null
rm -f /tmp/restore.sql.gz
touch /opt/cqc/.dump-restored
systemctl start cqc
echo "    restore complete"
REMOTE
  else
    echo "==> DB already restored once (set FORCE_RESTORE=1 to redo); skipping"
  fi
else
  if ssh "${SSH_OPTS[@]}" "root@$IP" 'test -f /opt/cqc/.data-seeded' 2>/dev/null; then
    echo "==> No local dump; base data already seeded — skipping"
  else
    echo "==> No local dump — seeding provider/facility base data from the repo CSVs (no LinkedIn enrichment)"
    ssh "${SSH_OPTS[@]}" "root@$IP" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo -u cqc bash -c 'set -a; . /opt/cqc/.env; set +a; cd /opt/cqc && .venv/bin/python import_records.py output.csv && .venv/bin/python enrich_locations.py Locations.csv'
touch /opt/cqc/.data-seeded
echo "    seed complete"
REMOTE
  fi
fi

# --- Backups (ADR 0019): encrypted, offboard, retention-pruned Borg → Storage Box.
#     The box needs an SSH key the Storage Box trusts; generate it here (idempotent)
#     and surface its public half. If BORG_REPO is configured, finish wiring: write
#     the secret-free-in-repo .backup.env, generate/persist the passphrase, init the
#     repo, and take a first backup to validate. Otherwise print the operator step. ---
echo "==> Configuring backups (BorgBackup)"
BACKUP_PUBKEY="$(ssh "${SSH_OPTS[@]}" "root@$IP" 'bash -s' <<'REMOTE'
set -euo pipefail
install -d -m 700 /opt/cqc/.ssh
if [ ! -f /opt/cqc/.ssh/backup_ed25519 ]; then
  ssh-keygen -t ed25519 -N '' -C cqc-backup -f /opt/cqc/.ssh/backup_ed25519 >/dev/null
fi
chmod 600 /opt/cqc/.ssh/backup_ed25519
cat /opt/cqc/.ssh/backup_ed25519.pub
REMOTE
)"

if [ -n "${BORG_REPO:-}" ]; then
  # Passphrase: explicit env → persisted-on-box → generated (same idiom as basic-auth).
  BORG_PASS_FILE=/opt/cqc/.borg_passphrase
  resolve_or_generate_secret "${BORG_PASSPHRASE:-}" "$BORG_PASS_FILE" 24
  BORG_PASSPHRASE="$RESOLVED_SECRET"; GENERATED_BORG_PASS="$GENERATED"
  printf '%s' "$BORG_PASSPHRASE" | ssh "${SSH_OPTS[@]}" "root@$IP" "install -m 600 /dev/stdin $BORG_PASS_FILE"
  # Write the secret-free-in-repo config the backup script reads.
  printf 'BORG_REPO=%s\nBACKUP_KEEP_DAILY=%s\n' "$BORG_REPO" "$BACKUP_KEEP_DAILY" \
    | ssh "${SSH_OPTS[@]}" "root@$IP" 'install -m 600 /dev/stdin /opt/cqc/.backup.env'
  echo "==> Initialising the repo + taking a first backup (validates repo + Storage Box reachability)"
  if ssh "${SSH_OPTS[@]}" "root@$IP" 'bash /opt/cqc/deploy/backup.sh backup'; then
    BACKUP_STATUS="configured — first backup taken (daily timer active; keep-daily=$BACKUP_KEEP_DAILY)"
  else
    BACKUP_STATUS="ERROR — first backup FAILED (check BORG_REPO reachability + that the key below is on the Storage Box)"
  fi
else
  BACKUP_STATUS="PENDING — add the key below to a Storage Box + set BORG_REPO, then re-run"
fi

echo
echo "======================================================================"
echo " Provisioned: https://$DOMAIN"
echo "   DNS:        point an A record  $DOMAIN -> $IP  (before TLS can issue)"
echo "   Basic auth: $BASIC_AUTH_USER / ${BASIC_AUTH_PASS}"
[ "${GENERATED_PASS:-0}" = "1" ] && echo "               (generated — store it; re-run with BASIC_AUTH_PASS=... to set your own)"
echo "   SSH:        ssh -i $SSH_PRIVKEY root@$IP"
echo "   App logs:   ssh root@$IP journalctl -u cqc -f"
echo "   Backups:    $BACKUP_STATUS"
if [ "${GENERATED_BORG_PASS:-0}" = "1" ]; then
  echo "               Borg passphrase (generated): $BORG_PASSPHRASE"
  echo "               *** STORE THIS OFF THE BOX. Without it the backups are"
  echo "                   UNRECOVERABLE — it is not derivable from anything else. ***"
fi
if [ -z "${BORG_REPO:-}" ]; then
  echo "   To enable backups (ADR 0019):"
  echo "     1. Provision a Hetzner Storage Box."
  echo "     2. Authorise this box's backup key on it (append to its .ssh/authorized_keys):"
  echo "          ${BACKUP_PUBKEY}"
  echo "     3. Set  BORG_REPO=ssh://uNNNN@uNNNN.your-storagebox.de/./cqc-companies  in .env.local"
  echo "     4. Re-run ./deploy/provision.sh  (inits the repo + takes the first backup)"
fi
echo "======================================================================"
