#!/bin/sh
# topo-db.sh — back up, list, export settings and restore the confetti-topo database from the shell.
#
#   topo-db.sh backup [file]       online backup (sqlite3 .backup) to <file>, or into the backups folder
#   topo-db.sh list                the stored backups, newest first
#   topo-db.sh env [file]          topo.env without its secrets (to move the settings to a new VM)
#   topo-db.sh restore [-y] <file> replace the database with <file>: checks it, saves the current one as
#                                  topo-pre-restore-<time>.db, stops the service, swaps the file, starts it again
#                                  and waits for /api/health. Asks first unless -y.
#
# The Settings page does the same over the web (app/backup.py). Backups hold device credentials in plaintext like the
# database itself: the folder is 0700 and the files 0600. Installed as /usr/local/bin/topo-db.sh by build-topo.sh.

set -eu

INSTALL_DIR="/opt/confetti-topo"
ENV_FILE="${INSTALL_DIR}/topo.env"
DB_FILE="/var/lib/confetti-topo/topo.db"
SERVICE="confetti-topo"
HEALTH_WAIT_S=30

if [ -f "$ENV_FILE" ]; then
    _v=$(sed -n 's/^TOPO_DB_PATH=//p' "$ENV_FILE" | tr -d '"' | tail -1)
    [ -z "$_v" ] || DB_FILE="$_v"
fi
BACKUP_DIR="$(dirname "$DB_FILE")/backups"

log() { printf '[topo-db] %s\n' "$1"; }
die() { printf '[topo-db] FATAL: %s\n' "$1" >&2; exit 1; }
usage() { die "Usage: topo-db.sh backup [file] | list | env [file] | restore [-y] <file>"; }

need_sqlite() { command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not found (apk add sqlite)"; }

stamp() { date -u +%Y%m%d-%H%M%S; }

backup_to() {
    _out="$1"
    _tmp="${_out}.tmp"
    rm -f "$_tmp"
    sqlite3 "$DB_FILE" ".backup '${_tmp}'" || { rm -f "$_tmp"; die "sqlite3 .backup failed"; }
    chmod 600 "$_tmp"
    mv -f "$_tmp" "$_out"
}

health_url() {
    _port=$(sed -n 's/^TOPO_PORT=//p' "$ENV_FILE" 2>/dev/null | tr -d '"' | tail -1)
    printf 'http://127.0.0.1:%s/api/health' "${_port:-80}"
}

wait_healthy() {
    _url=$(health_url)
    _i=0
    while [ "$_i" -lt "$HEALTH_WAIT_S" ]; do
        if curl -fs -m 3 -o /dev/null "$_url"; then
            return 0
        fi
        sleep 1
        _i=$((_i + 1))
    done
    return 1
}

cmd="${1:-}"
[ -n "$cmd" ] || usage
shift

case "$cmd" in
backup)
    need_sqlite
    [ -f "$DB_FILE" ] || die "no database at $DB_FILE"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    out="${1:-${BACKUP_DIR}/topo-manual-$(stamp).db}"
    backup_to "$out"
    log "backup written: $out (holds device credentials: keep it private)"
    ;;
list)
    [ -d "$BACKUP_DIR" ] || { log "no backups yet ($BACKUP_DIR)"; exit 0; }
    ls -1t "$BACKUP_DIR"/*.db 2>/dev/null | while IFS= read -r f; do
        printf '%10s  %s\n' "$(wc -c < "$f")" "$f"
    done
    ;;
env)
    [ -f "$ENV_FILE" ] || die "no $ENV_FILE"
    out="${1:-}"
    # Lines whose key looks like a secret are left out, so the file can be copied around.
    filtered=$(printf '# confetti-topo settings exported %s (secrets left out)\n' "$(date -u '+%Y-%m-%d %H:%M UTC')"
               grep -E '^[A-Z_]+=' "$ENV_FILE" | grep -viE '^TOPO_[A-Z_]*(PASS|SECRET|TOKEN|COMMUNITY|KEY)[A-Z_]*=' || true)
    if [ -n "$out" ]; then
        printf '%s\n' "$filtered" > "$out"
        log "settings written: $out"
    else
        printf '%s\n' "$filtered"
    fi
    ;;
restore)
    need_sqlite
    [ "$(id -u)" -eq 0 ] || die "Must run as root"
    auto=""
    if [ "${1:-}" = "-y" ] || [ "${1:-}" = "--yes" ]; then
        auto="1"
        shift
    fi
    src="${1:-}"
    [ -n "$src" ] && [ -f "$src" ] || usage
    check=$(sqlite3 "file:${src}?mode=ro" "PRAGMA quick_check;" 2>&1) || die "$src is not an SQLite database"
    [ "$check" = "ok" ] || die "$src is damaged: $check"
    devices=$(sqlite3 "file:${src}?mode=ro" "SELECT COUNT(*) FROM devices;" 2>/dev/null) ||
        die "$src is not a confetti-topo database (no devices table)"
    log "$src: $devices device(s)"
    if [ -z "$auto" ]; then
        printf 'Replace %s with %s? The current database is saved first. [y/N] ' "$DB_FILE" "$src"
        answer=""
        read -r answer || answer=""
        case "$answer" in y|Y|yes|YES) ;; *) log "cancelled"; exit 0 ;; esac
    fi
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    saved="${BACKUP_DIR}/topo-pre-restore-$(stamp).db"
    if [ -f "$DB_FILE" ]; then
        backup_to "$saved"
        log "current database saved: $saved"
    fi
    rc-service "$SERVICE" stop >/dev/null 2>&1 || true
    tmp="${DB_FILE}.restore"
    cp -f "$src" "$tmp"
    chmod 600 "$tmp"
    rm -f "${DB_FILE}-wal" "${DB_FILE}-shm"
    mv -f "$tmp" "$DB_FILE"
    rc-service "$SERVICE" start >/dev/null 2>&1 || true
    # The service upgrades an older schema in place on start (db.init_db).
    if wait_healthy; then
        log "restored and running"
    else
        die "the service did not become healthy; the previous database is $saved"
    fi
    ;;
*)
    usage
    ;;
esac
