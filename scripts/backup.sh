#!/usr/bin/env bash
set -euo pipefail

# SmartRSS nightly backup script (NFR-OPS-01)
# Creates mode-0600 custom-format pg_dump archives, rotates local dumps (14d),
# and optionally copies to an off-host LAN target without --delete.

umask 077

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DB_SERVICE="${BACKUP_DB_SERVICE:-db}"
POSTGRES_USER="${POSTGRES_USER:-smartrss}"
POSTGRES_DB="${POSTGRES_DB:-smartrss}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_RSYNC_TARGET="${BACKUP_RSYNC_TARGET:-}"
BACKUP_REMOTE_RETENTION_DAYS="${BACKUP_REMOTE_RETENTION_DAYS:-30}"
LOCAL_RETENTION_DAYS="${BACKUP_LOCAL_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="${BACKUP_DIR}/smartrss-${TIMESTAMP}.dump"
TMP_FILE="${DUMP_FILE}.tmp"

cleanup_tmp() {
  rm -f "$TMP_FILE"
}
trap cleanup_tmp EXIT

echo "[backup] Dumping ${POSTGRES_DB} from compose service '${DB_SERVICE}'..."
docker compose exec -T "$DB_SERVICE" \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$TMP_FILE"

if [[ ! -s "$TMP_FILE" ]]; then
  echo "[backup] ERROR: Backup file is empty." >&2
  exit 1
fi

# Verify archive integrity via pg_restore --list inside the db container
if ! docker compose exec -T "$DB_SERVICE" pg_restore --list < "$TMP_FILE" > /dev/null; then
  echo "[backup] ERROR: pg_restore --list verification failed." >&2
  exit 1
fi

mv "$TMP_FILE" "$DUMP_FILE"
chmod 600 "$DUMP_FILE"
echo "[backup] Created verified archive: ${DUMP_FILE}"

# Prune local dumps older than 14 days
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-*.dump' -mtime +"$LOCAL_RETENTION_DAYS" -delete

# Optional off-host LAN copy (NEVER uses --delete)
if [[ -n "$BACKUP_RSYNC_TARGET" ]]; then
  mapfile -t local_dumps < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-*.dump' -size +0c | sort)
  if [[ "${#local_dumps[@]}" -eq 0 ]]; then
    echo "[backup] ERROR: No valid local dumps found in ${BACKUP_DIR}; skipping rsync." >&2
    exit 1
  fi

  echo "[backup] Syncing ${#local_dumps[@]} dump(s) to ${BACKUP_RSYNC_TARGET} (no --delete)..."
  rsync -az "${local_dumps[@]}" "${BACKUP_RSYNC_TARGET%/}/"

  # Independent remote retention pruning (only after rsync succeeds)
  REMOTE_HOST="${BACKUP_RSYNC_TARGET%%:*}"
  REMOTE_PATH="${BACKUP_RSYNC_TARGET#*:}"
  if [[ -n "$REMOTE_HOST" && -n "$REMOTE_PATH" && "$REMOTE_HOST" != "$BACKUP_RSYNC_TARGET" ]]; then
    echo "[backup] Pruning remote dumps older than ${BACKUP_REMOTE_RETENTION_DAYS} days on ${REMOTE_HOST}:${REMOTE_PATH}..."
    ssh "$REMOTE_HOST" "find $(printf '%q' "$REMOTE_PATH") -maxdepth 1 -type f -name 'smartrss-*.dump' -mtime +${BACKUP_REMOTE_RETENTION_DAYS} -delete"
  fi
fi

echo "[backup] Done."
