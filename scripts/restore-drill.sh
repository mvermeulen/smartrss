#!/usr/bin/env bash
set -euo pipefail

# SmartRSS automated restore drill (NFR-OPS-01)
# Restores latest (or newly taken) dump into a temporary database, rotates
# instance_epoch strictly against the drill DB (passing URL via env var, I-18),
# verifies production is untouched, verifies Alembic head and row counts,
# and drops the temporary DB.

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DB_SERVICE="${DRILL_DB_SERVICE:-db}"
POSTGRES_USER="${POSTGRES_USER:-smartrss}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-smartrss}"
POSTGRES_DB="${POSTGRES_DB:-smartrss}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DRILL_DB="${DRILL_DB_NAME:-smartrss_restore_drill}"
DUMP_FILE="${1:-}"
FRESH_DUMP=0

COUNTS_SQL="SELECT 'feeds=' || (SELECT count(*) FROM feeds) || ' items=' || (SELECT count(*) FROM items) || ' devices=' || (SELECT count(*) FROM devices);"

if [[ -z "$DUMP_FILE" ]]; then
  DUMP_FILE="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-*.dump' -size +0c 2>/dev/null | sort | tail -n 1 || true)"
fi

if [[ -z "$DUMP_FILE" || ! -s "$DUMP_FILE" ]]; then
  echo "[restore-drill] No existing dump found; creating a fresh backup first..."
  BACKUP_DB_SERVICE="$DB_SERVICE" ./scripts/backup.sh
  DUMP_FILE="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'smartrss-*.dump' -size +0c | sort | tail -n 1)"
  FRESH_DUMP=1
fi

echo "[restore-drill] Using dump: ${DUMP_FILE}"

cleanup() {
  echo "[restore-drill] Dropping temporary database '${DRILL_DB}'..."
  docker compose exec -T "$DB_SERVICE" \
    psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DRILL_DB}' AND pid <> pg_backend_pid();" \
    > /dev/null 2>&1 || true
  docker compose exec -T "$DB_SERVICE" \
    dropdb -U "$POSTGRES_USER" --if-exists "$DRILL_DB" > /dev/null 2>&1 || true
}
trap cleanup EXIT

# Record production instance_epoch and counts before drill
PROD_EPOCH_BEFORE="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT instance_epoch FROM sync_state WHERE id = 1;")"
PROD_COUNTS="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$COUNTS_SQL")"

echo "[restore-drill] Creating fresh drill database '${DRILL_DB}'..."
docker compose exec -T "$DB_SERVICE" dropdb -U "$POSTGRES_USER" --if-exists "$DRILL_DB"
docker compose exec -T "$DB_SERVICE" createdb -U "$POSTGRES_USER" "$DRILL_DB"

echo "[restore-drill] Restoring archive into '${DRILL_DB}'..."
docker compose exec -T "$DB_SERVICE" \
  pg_restore -U "$POSTGRES_USER" -d "$DRILL_DB" --no-owner --clean --if-exists < "$DUMP_FILE"

OLD_EPOCH="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$DRILL_DB" -tAc "SELECT instance_epoch FROM sync_state WHERE id = 1;")"

if [[ -z "$OLD_EPOCH" ]]; then
  echo "[restore-drill] ERROR: sync_state.instance_epoch missing in drill DB." >&2
  exit 1
fi

echo "[restore-drill] Drill DB old instance_epoch: ${OLD_EPOCH}"

# Build explicit drill DATABASE_URL and pass strictly via env var (not CLI argv, I-18)
if [[ -n "${DRILL_DATABASE_URL:-}" ]]; then
  TARGET_URL="$DRILL_DATABASE_URL"
else
  TARGET_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DB_SERVICE}:5432/${DRILL_DB}"
fi

# Export the URL into this shell's environment only; never interpolate it into any argv
# (`-e NAME` without `=value` makes docker compose forward it from the environment, so the
# password never appears in `ps` output for the docker CLI process).
export SMARTRSS_DRILL_DATABASE_URL="$TARGET_URL"
unset TARGET_URL

if [[ "${DRILL_USE_LOCAL_CLI:-0}" == "1" ]]; then
  uv run --directory backend smartrss db rotate-epoch --database-url-env SMARTRSS_DRILL_DATABASE_URL
  EXPECTED_HEAD="$(uv run --directory backend alembic heads | awk '{print $1}' | head -n 1)"
else
  docker compose run --rm --no-deps \
    -e SMARTRSS_DRILL_DATABASE_URL \
    api smartrss db rotate-epoch --database-url-env SMARTRSS_DRILL_DATABASE_URL
  EXPECTED_HEAD="$(docker compose run --rm --no-deps api alembic heads | awk '{print $1}' | head -n 1)"
fi

NEW_EPOCH="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$DRILL_DB" -tAc "SELECT instance_epoch FROM sync_state WHERE id = 1;")"

echo "[restore-drill] Drill DB new instance_epoch: ${NEW_EPOCH}"

if [[ "$NEW_EPOCH" == "$OLD_EPOCH" || -z "$NEW_EPOCH" ]]; then
  echo "[restore-drill] ERROR: instance_epoch did not rotate!" >&2
  exit 1
fi

PROD_EPOCH_AFTER="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT instance_epoch FROM sync_state WHERE id = 1;")"

if [[ "$PROD_EPOCH_BEFORE" != "$PROD_EPOCH_AFTER" ]]; then
  echo "[restore-drill] ERROR: Production instance_epoch changed during drill!" >&2
  exit 1
fi

DRILL_HEAD="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$DRILL_DB" -tAc "SELECT version_num FROM alembic_version;")"

if [[ "$DRILL_HEAD" != "$EXPECTED_HEAD" ]]; then
  echo "[restore-drill] ERROR: Alembic head mismatch (drill=${DRILL_HEAD}, expected=${EXPECTED_HEAD})." >&2
  exit 1
fi

DRILL_COUNTS="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$DRILL_DB" -tAc "$COUNTS_SQL")"

if [[ -z "$DRILL_COUNTS" ]]; then
  echo "[restore-drill] ERROR: Failed to query expected table row counts in drill DB." >&2
  exit 1
fi

if [[ "$FRESH_DUMP" -eq 1 && "$DRILL_COUNTS" != "$PROD_COUNTS" ]]; then
  echo "[restore-drill] ERROR: Row count mismatch (drill='${DRILL_COUNTS}' vs source='${PROD_COUNTS}')." >&2
  exit 1
fi

# Even on a pre-existing dump, verify non-empty source DB didn't restore as an empty DB
DRILL_TOTAL_ROWS="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$DRILL_DB" -tAc "SELECT (SELECT count(*) FROM feeds) + (SELECT count(*) FROM items) + (SELECT count(*) FROM devices);")"
PROD_TOTAL_ROWS="$(docker compose exec -T "$DB_SERVICE" \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT (SELECT count(*) FROM feeds) + (SELECT count(*) FROM items) + (SELECT count(*) FROM devices);")"
if [[ "$PROD_TOTAL_ROWS" -gt 0 && "$DRILL_TOTAL_ROWS" -eq 0 ]]; then
  echo "[restore-drill] ERROR: Source DB has ${PROD_TOTAL_ROWS} rows but drill DB restored empty!" >&2
  exit 1
fi

echo "[restore-drill] Verified Alembic head (${DRILL_HEAD}), epoch rotation, untouched production DB, and row counts: ${DRILL_COUNTS} (source: ${PROD_COUNTS})"
echo "[restore-drill] PASS"
