# SmartRSS

Self-hosted, single-user, LAN-first RSS reader backend with SSRF-hardened polite polling, isolated full-text extraction, PostgreSQL `SKIP LOCKED` job queue, per-device pairing auth, and backup/restore tooling.

> **Current Implementation:** Milestone `1a` — Backend Foundation, Security, Polling & Extraction ([Design Spec](docs/design/1a-backend-ingestion-and-security.md)).

## Quickstart (Docker Compose)

1. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`, `ALLOWED_HOSTS`, and `CORS_ALLOW_ORIGINS` for your LAN host:
   ```bash
   cp .env.example .env
   ```
   *(If your home router has port-forwards exposing services on your WAN IPv4 or your host has a global IPv6 address you want unreachable from feed/article fetches, add your home WAN IPv4 `/32` and host global IPv6 prefix to `SSRF_BLOCKED_CIDRS` in `.env`.)*

2. Start the stack and apply database migrations:
   ```bash
   make up
   make migrate
   ```

3. Pair your first device via CLI:
   ```bash
   docker compose exec api smartrss pair create --name "ThinkPad" --platform ubuntu
   ```
   Exchange the returned one-time code (`XXXX-XXXX-XXXX-XXXX`, valid 5 minutes) via:
   ```bash
   curl -X POST http://localhost:8080/api/v1/auth/pair \
     -H 'Content-Type: application/json' \
     -d '{"code":"XXXX-XXXX-XXXX-XXXX","device_name":"ThinkPad","platform":"ubuntu"}'
   ```

4. Check health:
   ```bash
   curl http://localhost:8080/api/v1/health
   ```

## Worker Execution Modes

SmartRSS supports two mutually exclusive worker modes (`WORKER_MODE=daemon` or `WORKER_MODE=cron`):

- **Daemon mode (default)**: Keep `WORKER_MODE=daemon` and `COMPOSE_PROFILES=daemon` in `.env`. Docker Compose runs the `worker` container continuously.
- **External cron mode**: Set `WORKER_MODE=cron` and `COMPOSE_PROFILES=` (empty) in `.env` so `docker compose up` does not start the daemon container. Invoke batches from host `cron` or a `systemd` timer:
  ```bash
  docker compose run --rm worker smartrss worker run-once --stage all --json
  ```

## Settings CLI (`1a`)

Initial `DEFAULT_*` env vars seed `app_settings` only during the first migration; the database is authoritative thereafter:
```bash
docker compose exec api smartrss settings get
docker compose exec api smartrss settings set backfill_cap 15
docker compose exec api smartrss settings set default_expiration_days 30
docker compose exec api smartrss settings set default_poll_interval_minutes 60
```

## Backups & Restore Runbook

### Automated Nightly Backups
- Run manually: `make backup`
- Install the example `systemd` units in `deploy/systemd/` (`smartrss-backup.service` + `smartrss-backup.timer`) or add a host cron entry:
  ```cron
  15 3 * * * cd /opt/smartrss && /usr/bin/make backup >> /var/log/smartrss-backup.log 2>&1
  ```
- Backups are created with `umask 077` (`0600`), verified with `pg_restore --list`, rotated locally after 14 days, and optionally copied off-host via `BACKUP_RSYNC_TARGET` (never using `--delete`, with independent remote retention via `BACKUP_REMOTE_RETENTION_DAYS`).

### Automated Restore Drill
Verify your latest backup restores cleanly and rotates `sync_state.instance_epoch` without touching production:
```bash
make restore-drill
```

### Production Restore Runbook
1. **Stop writers & API**:
   ```bash
   docker compose stop api worker
   ```
2. **Restore dump into a fresh database**:
   ```bash
   docker compose exec -T db dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
   docker compose exec -T db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
   docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner < backups/smartrss-<timestamp>.dump
   ```
3. **Rotate `instance_epoch` BEFORE serving traffic** (forces paired clients to re-bootstrap while keeping pending offline outbox mutations):
   ```bash
   docker compose run --rm api smartrss db rotate-epoch --database-url "$DATABASE_URL"
   ```
4. **Review & re-apply post-backup device revocations**:
   ```bash
   docker compose run --rm api smartrss devices list
   docker compose run --rm api smartrss devices revoke <device_id>
   ```
5. **Apply any newer migrations**:
   ```bash
   docker compose run --rm api smartrss db migrate
   ```
6. **Restart & verify**:
   ```bash
   docker compose start api worker   # omit worker in cron mode
   curl http://localhost:8080/api/v1/health
   ```

## Local Development & Verification

Requires Python 3.12+, `uv`, and Docker (for `db-test`):
```bash
make check        # ruff check/format + mypy + unit/boundary/integration tests + coverage gate
make test-unit    # fast unit & boundary tests (no DB needed)
make test-int     # real PostgreSQL 16 (pgvector) integration tests
```
