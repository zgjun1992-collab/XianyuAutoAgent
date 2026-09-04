#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${LICENSE_DB:-/var/lib/xianyu-license/cloud-license.db}"
BACKUP_DIR="${LICENSE_BACKUP_DIR:-/var/backups/xianyu-license}"
KEEP_DAYS="${LICENSE_BACKUP_KEEP_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_DB="${BACKUP_DIR}/.${STAMP}.sqlite.tmp"
OUTPUT="${BACKUP_DIR}/cloud-license-${STAMP}.sqlite.gz"

install -d -m 0700 "$BACKUP_DIR"
test -f "$DB_PATH"
trap 'rm -f "$TMP_DB"' EXIT
sqlite3 "$DB_PATH" ".timeout 10000" ".backup '$TMP_DB'"
test "$(sqlite3 "$TMP_DB" 'PRAGMA integrity_check;')" = "ok"
gzip -9 -c "$TMP_DB" > "$OUTPUT"
chmod 0600 "$OUTPUT"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$OUTPUT")" > "$(basename "${OUTPUT}.sha256")")
find "$BACKUP_DIR" -type f -name 'cloud-license-*.sqlite.gz*' -mtime "+$KEEP_DAYS" -delete

if command -v ossutil >/dev/null 2>&1 && [[ -n "${OSS_BACKUP_URI:-}" ]]; then
    ossutil cp -f "$OUTPUT" "${OSS_BACKUP_URI%/}/$(basename "$OUTPUT")"
    ossutil cp -f "${OUTPUT}.sha256" "${OSS_BACKUP_URI%/}/$(basename "${OUTPUT}.sha256")"
fi

echo "$OUTPUT"
