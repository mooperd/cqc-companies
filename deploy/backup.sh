#!/usr/bin/env bash
#
# backup.sh — encrypted, offboard, retention-pruned Postgres backups via
# BorgBackup to a Hetzner Storage Box. Runs ON the box; scheduled daily by
# cqc-backup.timer. See ADR 0018 (the box) and ADR 0019 (why this is mandatory).
#
# Why this exists: ADR 0019 makes the deployed DB the AUTHORITATIVE home for
# scraped (non-regenerable) LinkedIn Person/Role data, so the box is a single
# point of loss. A backup therefore must:
#   1. leave the box        — box loss is the risk, so a local-only copy is no backup;
#   2. be encrypted         — the dump is personal data (ADR 0017); Borg repokey
#                             encryption means the Storage Box holds only ciphertext;
#   3. have a stated window — retention (keep-daily) is ALSO the ADR 0017 §5
#                             backup-erasure lag: an erased person persists in
#                             backups only until the oldest kept archive ages out.
#
# Config comes from /opt/cqc/.backup.env (written by provision.sh, NEVER committed):
#   BORG_REPO          ssh://uXXXXX@uXXXXX.your-storagebox.de/./cqc-companies  (REQUIRED)
#   BACKUP_DB          Postgres db to dump                 (default: darwinist)
#   BACKUP_KEEP_DAILY  daily archives kept  → erasure lag  (default: 14 days)
# and two knobs with box-local defaults (rarely overridden):
#   BORG_PASSCOMMAND   how borg reads the repo passphrase  (default: cat the pass file)
#   BORG_RSH           ssh incl. the box's backup key      (default below)
#
# Usage (on the box; the timer runs the default `backup`):
#   deploy/backup.sh                     # pg_dump -> encrypted archive -> prune
#   deploy/backup.sh init                # one-time: create the encrypted repo
#   deploy/backup.sh list                # list archives
#   deploy/backup.sh check               # verify repo + latest archive
#   deploy/backup.sh restore-latest [DB] # restore drill into DB (default: darwinist_restore)
set -euo pipefail

# Load config ourselves — one path that works both under the systemd timer and for
# manual operator runs (so the unit needs no EnvironmentFile).
if [ -f /opt/cqc/.backup.env ]; then set -a; . /opt/cqc/.backup.env; set +a; fi

: "${BACKUP_DB:=darwinist}"
: "${BACKUP_KEEP_DAILY:=14}"
: "${BORG_PASSCOMMAND:=cat /opt/cqc/.borg_passphrase}"
: "${BORG_RSH:=ssh -i /opt/cqc/.ssh/backup_ed25519 -o StrictHostKeyChecking=accept-new}"
export BORG_PASSCOMMAND BORG_RSH
INIT_MARKER=/opt/cqc/.borg-repo-init

log() { printf '[backup] %s\n' "$*"; }

# Backups unconfigured is the expected pre-Storage-Box state, not an error: the
# timer would otherwise fail nightly before the operator provisions the box. Warn
# loudly (the ADR 0019 single-point-of-loss window is open) but exit 0.
require_configured() {
  if [ -z "${BORG_REPO:-}" ]; then
    log "WARNING: BORG_REPO unset — backups are NOT configured. The box is a single"
    log "         point of loss (ADR 0019) until you provision a Hetzner Storage Box"
    log "         and set BORG_REPO in /opt/cqc/.backup.env. Skipping (not a failure)."
    exit 0
  fi
  export BORG_REPO
  command -v borg >/dev/null || { log "ERROR: borgbackup not installed"; exit 1; }
}

cmd_init() {
  require_configured
  # After the first success a local marker skips the nightly `borg info` round-trip
  # to the Storage Box that would only re-confirm what's already permanently true.
  [ -f "$INIT_MARKER" ] && return 0
  if ! borg info "$BORG_REPO" >/dev/null 2>&1; then
    log "initialising encrypted repo (repokey-blake2): $BORG_REPO"
    # repokey (not keyfile): the key lives IN the repo, unlocked by the passphrase —
    # so a backup survives box loss and is restorable with the passphrase alone.
    borg init --encryption=repokey-blake2 "$BORG_REPO"
  fi
  touch "$INIT_MARKER"
}

cmd_backup() {
  require_configured
  cmd_init
  local name; name="cqc-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)"
  log "creating archive $name from pg_dump($BACKUP_DB)"
  # --no-owner: role-agnostic restore (the restoring cluster may lack the dump's
  # role), matching provision.sh's restore rewrite.
  sudo -u postgres pg_dump --no-owner "$BACKUP_DB" \
    | borg create --stdin-name "$BACKUP_DB.sql" --compression zstd "$BORG_REPO::$name" -
  log "pruning: keep-daily=$BACKUP_KEEP_DAILY (this window == the ADR 0017 §5 erasure lag)"
  borg prune --glob-archives 'cqc-*' --keep-daily "$BACKUP_KEEP_DAILY" "$BORG_REPO"
  borg compact "$BORG_REPO" || true
  log "done"
}

cmd_list()  { require_configured; borg list  --glob-archives 'cqc-*' "$BORG_REPO"; }
cmd_check() { require_configured; borg check --last 1 "$BORG_REPO" && log "repo + latest archive OK"; }

# Restore drill: pull the newest archive into a throwaway DB and prove it loads.
# Does NOT touch the live darwinist DB unless you name it as the target.
cmd_restore_latest() {
  require_configured
  local target="${1:-darwinist_restore}"
  local latest; latest="$(borg list --glob-archives 'cqc-*' --last 1 --short "$BORG_REPO")"
  [ -n "$latest" ] || { log "ERROR: no archives to restore"; exit 1; }
  log "restoring $latest -> database '$target' (throwaway unless you named the live DB)"
  local tmp; tmp="$(mktemp -d)"
  ( cd "$tmp" && borg extract "$BORG_REPO::$latest" )
  sudo -u postgres dropdb --if-exists "$target"
  sudo -u postgres createdb -O "$BACKUP_DB" "$target"
  # Archives are pg_dump --no-owner (no OWNER lines), so no ownership rewrite is
  # needed on restore — createdb -O already sets the target db's owner. Just load.
  sudo -u postgres psql -v ON_ERROR_STOP=1 "$target" <"$tmp/$BACKUP_DB.sql" >/dev/null
  rm -rf "$tmp"
  log "restore drill complete — inspect: sudo -u postgres psql $target -c '\\dt'"
}

case "${1:-backup}" in
  backup)          cmd_backup ;;
  init)            cmd_init ;;
  list)            cmd_list ;;
  check)           cmd_check ;;
  restore-latest)  shift; cmd_restore_latest "$@" ;;
  *) echo "usage: $0 {backup|init|list|check|restore-latest [DB]}" >&2; exit 2 ;;
esac
