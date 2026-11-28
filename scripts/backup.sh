#!/usr/bin/env bash
set -euo pipefail

# SmartRSS nightly backup script (NFR-OPS-01, NFR-OPS-02, E-11, O-03, O-04)
# 1. Creates & verifies mode-0600 custom-format pg_dump archives (fatal on error).
# 2. Exports & verifies a mode-0600 OPML 2.0 portability snapshot (subscriptions + categories;
#    pg_dump remains authoritative for status & settings). Best-effort within the run so an
#    OPML export error never blocks off-host rsync of a verified dump (O-03).
# 3. Independently prunes local dumps and OPML snapshots (14d default).
# 4. Optionally copies dumps + OPML snapshots to an off-host LAN target without --delete,
#    then prunes remote archives independently (30d default).
# 5. Exits non-zero at the end if OPML export failed so cron/systemd alerts.

umask 077

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DB_SERVICE="${BACKUP_DB_SERVICE:-db}"
API_SERVICE="${BACKUP_API_SERVICE:-api}"
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
OPML_FILE="${BACKUP_DIR}/smartrss-feeds-${TIMESTAMP}.opml"
TMP_OPML="${OPML_FILE}.tmp"

cleanup_tmp() {
  rm -f "$TMP_FILE" "$TMP_OPML"
}
trap cleanup_tmp EXIT

# Step 1: Database dump & integrity verification (fatal on error)
echo "[backup] Dumping ${POSTGRES_DB} from compose service '${DB_SERVICE}'..."
docker compose exec -T "$DB_SERVICE" \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$TMP_FILE"

if [[ ! -s "$TMP_FILE" ]]; then
  echo "[backup] ERROR: Backup file is empty." >&2
  exit 1
fi

if ! docker compose exec -T "$DB_SERVICE" pg_restore --list < "$TMP_FILE" > /dev/null; then
  echo "[backup] ERROR: pg_restore --list verification failed." >&2
  exit 1
fi

mv "$TMP_FILE" "$DUMP_FILE"
chmod 600 "$DUMP_FILE"
echo "[backup] Created verified archive: ${DUMP_FILE}"

# Step 2: OPML portability snapshot (best-effort within run; deferred exit 1 on failure, O-03)
OPML_FAILED=0
export_opml_snapshot() {
  if [[ "${BACKUP_USE_LOCAL_CLI:-0}" == "1" ]]; then
    uv run --directory backend smartrss feeds export-opml > "$TMP_OPML" || return 1
  elif docker compose ps --status running --services 2>/dev/null | grep -qx "$API_SERVICE"; then
    docker compose exec -T "$API_SERVICE" smartrss feeds export-opml > "$TMP_OPML" || return 1
  else
    docker compose run --rm -T --no-deps "$API_SERVICE" smartrss feeds export-opml > "$TMP_OPML" || return 1
  fi
  [[ -s "$TMP_OPML" ]] && grep -q '<opml' "$TMP_OPML" && grep -q '</opml>' "$TMP_OPML"
}

echo "[backup] Exporting OPML portability snapshot..."
if export_opml_snapshot; then
  mv "$TMP_OPML" "$OPML_FILE"
  chmod 600 "$OPML_FILE"
  echo "[backup] Created verified OPML snapshot: ${OPML_FILE}"
else
  rm -f "$TMP_OPML"
  echo "[backup] ERROR: OPML snapshot export failed; continuing to retention and off-host sync." >&2
  OPML_FAILED=1
fi

# Step 3: Prune local dumps and OPML snapshots independently
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-*.dump' -mtime +"$LOCAL_RETENTION_DAYS" -delete
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-feeds-*.opml' -mtime +"$LOCAL_RETENTION_DAYS" -delete

# Step 4: Optional off-host LAN copy (NEVER uses --delete; always runs for verified dumps)
if [[ -n "$BACKUP_RSYNC_TARGET" ]]; then
  mapfile -t local_dumps < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-*.dump' -size +0c | sort)
  if [[ "${#local_dumps[@]}" -eq 0 ]]; then
    echo "[backup] ERROR: No valid local dumps found in ${BACKUP_DIR}; skipping rsync." >&2
    exit 1
  fi
  mapfile -t local_opmls < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-feeds-*.opml' -size +0c | sort)
  sync_files=("${local_dumps[@]}" "${local_opmls[@]}")

  echo "[backup] Syncing ${#local_dumps[@]} dump(s) and ${#local_opmls[@]} OPML snapshot(s) to ${BACKUP_RSYNC_TARGET} (no --delete)..."
  rsync -az "${sync_files[@]}" "${BACKUP_RSYNC_TARGET%/}/"

  # Independent remote retention pruning (only after rsync succeeds)
  REMOTE_HOST="${BACKUP_RSYNC_TARGET%%:*}"
  REMOTE_PATH="${BACKUP_RSYNC_TARGET#*:}"
  if [[ -n "$REMOTE_HOST" && -n "$REMOTE_PATH" && "$REMOTE_HOST" != "$BACKUP_RSYNC_TARGET" ]]; then
    echo "[backup] Pruning remote backups older than ${BACKUP_REMOTE_RETENTION_DAYS} days on ${REMOTE_HOST}:${REMOTE_PATH}..."
    ssh "$REMOTE_HOST" "find $(printf '%q' "$REMOTE_PATH") -maxdepth 1 -type f -name 'smartrss-*.dump' -mtime +${BACKUP_REMOTE_RETENTION_DAYS} -delete"
    ssh "$REMOTE_HOST" "find $(printf '%q' "$REMOTE_PATH") -maxdepth 1 -type f -name 'smartrss-feeds-*.opml' -mtime +${BACKUP_REMOTE_RETENTION_DAYS} -delete"
  fi
fi

# Step 5: Deferred non-zero alert if OPML snapshot failed (O-03)
if [[ "$OPML_FAILED" -ne 0 ]]; then
  echo "[backup] ERROR: Database dump completed (and synced if configured), but OPML snapshot export failed." >&2
  exit 1
fi

echo "[backup] Done."
