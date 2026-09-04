#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: sudo $0 BACKUP.sqlite.gz" >&2
    exit 2
fi

BACKUP="$(readlink -f "$1")"
DB_PATH="${LICENSE_DB:-/var/lib/xianyu-license/cloud-license.db}"
RESTORE_TMP="${DB_PATH}.restore.$$"
SAFETY_COPY="${DB_PATH}.before-restore.$(date -u +%Y%m%dT%H%M%SZ)"

test -f "$BACKUP"
if [[ -f "${BACKUP}.sha256" ]]; then
    (cd "$(dirname "$BACKUP")" && sha256sum -c "$(basename "${BACKUP}.sha256")")
fi

gzip -dc "$BACKUP" > "$RESTORE_TMP"
test "$(sqlite3 "$RESTORE_TMP" 'PRAGMA integrity_check;')" = "ok"

systemctl stop xianyu-license
trap 'rm -f "$RESTORE_TMP"' EXIT
if [[ -f "$DB_PATH" ]]; then
    cp -a "$DB_PATH" "$SAFETY_COPY"
fi
install -o xianyu-license -g xianyu-license -m 0600 "$RESTORE_TMP" "$DB_PATH"
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
systemctl start xianyu-license
curl --fail --silent http://127.0.0.1:8787/health >/dev/null
echo "restore complete; previous database: $SAFETY_COPY"

