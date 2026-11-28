# SmartRSS

Self-hosted, single-user, LAN-first RSS reader with an SSRF-hardened backend, commit-ordered sync engine + PostgreSQL FTS search, and a local-first Ubuntu SPA with Service Worker offline reload survival.

> **Current Implementation:** Milestones `1a`–`1c` (`Implemented & Approved`) plus sub-milestone `1d-ops` (`In Implementation Review`) — Backend Foundation & Extraction ([`1a`](docs/design/1a-backend-ingestion-and-security.md)), Sync Engine & Search ([`1b`](docs/design/1b-sync-protocol-and-search.md)), Local-First Ubuntu Reader UI, Detached Offline Shell & System Health ([`1c`](docs/design/1c-client-ui-and-offline-shell.md)), and Guided Server Setup, Headless Feed CLI, Backup OPML & Sync Resync Fencing ([`1d-ops`](docs/design/1d-ops-setup-and-cli.md)).

## Guided First-Time Setup (`make setup`)

For a fresh LAN server (Ubuntu / Debian with Docker and Docker Compose v2 installed and your user in the `docker` group), run the idempotent guided setup script:

```bash
make setup
```

`make setup` (wrapping [`scripts/setup-server.sh`](scripts/setup-server.sh)) never invokes `sudo` and performs:
1. Prerequisite checks (`docker`, `docker compose`, daemon access) with actionable hints on failure.
2. Safe `.env` creation (mode `0600`, atomic `O_EXCL` + `os.link`) with a freshly generated `POSTGRES_PASSWORD`, auto-detected LAN IPv4 (and resolvable `.local` mDNS hostname if active) in `ALLOWED_HOSTS` / `CORS_ALLOW_ORIGINS`, and `WORKER_MODE` (`daemon` or `cron`). Re-running `make setup` preserves an existing `.env` byte-for-byte.
3. Ordered startup: builds the shared `smartrss-backend:local` image and `web` image (`docker compose build api web`), starts `db` + `api`, applies Alembic migrations, starts the remaining services, and polls `/api/v1/health` through the `web` reverse proxy.
4. Optional user `systemd` worker timer installation (`--worker-mode cron --install-worker-timer`), optional OPML seeding (`--opml feeds.opml`), and prints a fresh pairing code plus the exact `make install-client API_ORIGIN=...` command for your laptop.

Non-interactive / flag-driven example:
```bash
make setup SETUP_ARGS="--non-interactive --worker-mode cron --install-worker-timer --lan-host 192.168.1.50 --opml ./feeds.opml"
```

- **LAN IP changes**: Assign a router DHCP reservation for the server. If the server's LAN IP changes later, edit `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS` in your existing `.env` and restart with `make up`.
- **Upgrading from pre-`1d-ops` builds**: `api` and `worker` now share the single local image tag `smartrss-backend:local`. You can remove legacy duplicate image tags after upgrading with `docker image rm smartrss-api smartrss-worker`.

## Advanced Manual Quickstart (Docker Compose)

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
- **External cron / systemd timer mode**: Set `WORKER_MODE=cron` and `COMPOSE_PROFILES=` (empty) in `.env` so `docker compose up` does not start the daemon container. Invoke batches either via the shipped user `systemd` units ([`deploy/systemd/smartrss-worker.service`](deploy/systemd/smartrss-worker.service) + [`deploy/systemd/smartrss-worker.timer`](deploy/systemd/smartrss-worker.timer), installed by `make setup SETUP_ARGS="--worker-mode cron --install-worker-timer"`; enable boot persistence with `sudo loginctl enable-linger "$USER"`) **or** via host `cron` — do not enable both simultaneously. Both use the same shared `~/.local/state/smartrss/worker.lock` path (`flock -n -E 75` with `SuccessExitStatus=75` in systemd so overlapping runs skip cleanly without failing the unit):
  ```cron
  */15 * * * * cd /path/to/smartrss && /usr/bin/flock -n ~/.local/state/smartrss/worker.lock /usr/bin/docker compose run --rm -T worker smartrss worker run-once --stage all --max-duration 840 --json >> ~/.local/state/smartrss/worker.log 2>&1
  ```
  - Note: `--json` leaves Python logging unconfigured so stdout stays machine-readable JSON; any unexpected stderr warnings or tracebacks will interleave when redirecting `2>&1` (redirect stderr to a separate file if parsing `worker.log` strictly as JSONL).
  - Rotate `worker.log` via `logrotate` (or invoke the command from a `systemd` timer so output is rotated automatically by `journald`):
    ```text
    /home/<user>/.local/state/smartrss/worker.log {
        weekly
        rotate 8
        compress
        missingok
        notifempty
        copytruncate
    }
    ```

## Headless Feed CLI (`1d-ops`)

Manage subscriptions directly from the server shell without needing a browser or container bind mounts (all commands share the same transactional validation, SSRF checks, and `SyncLogWriter` path as the REST API):
```bash
# List feeds (optional --status active|paused|muted|unsubscribed, --health, --json)
docker compose exec api smartrss feeds list

# Subscribe to a feed (--create-category opts into creating a new category;
# --if-not-exists exits 0 when already active, or exit 3 with status when paused/muted)
docker compose exec api smartrss feeds add https://lwn.net/headlines/rss \
  --title "LWN.net" --category Linux --create-category --if-not-exists

# Import OPML from stdin (capped at 2 MiB; no host bind mount required)
docker compose exec -T api smartrss feeds import-opml - < ./feeds.opml

# Export pure OPML 2.0 XML to stdout (zero log noise) or atomic mode-0600 file
docker compose exec -T api smartrss feeds export-opml > ./feeds-export.opml
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
- Backups are created with `umask 077` (`0600`), verified with `pg_restore --list`, paired with a verified mode-`0600` OPML 2.0 **portability snapshot** (`smartrss-feeds-<timestamp>.opml`, containing subscriptions and categories; `pg_dump` remains authoritative for feed `paused`/`muted` statuses and per-feed settings), rotated locally after 14 days (dumps and OPMLs pruned independently), and optionally copied off-host via `BACKUP_RSYNC_TARGET` (never using `--delete`, with independent remote retention via `BACKUP_REMOTE_RETENTION_DAYS`). If OPML export fails, off-host `rsync` of the verified database dump still completes before `backup.sh` exits `1` so cron/systemd alerts.

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

## Ubuntu Laptop Client (Localhost Offline Shell — `1c`)

For full detached offline reading, Service Worker reload survival, and `navigator.storage.persist()` on Ubuntu laptops, run the lightweight Python 3.12 stdlib static server on `http://localhost:4180` (default `CORS_ALLOW_ORIGINS` already allows `http://localhost:8080,http://localhost:4180`):

1. **Install or update** (builds `frontend/dist` and installs the runner, user `systemd` service, and `.desktop` launcher):
   ```bash
   make install-client API_ORIGIN=http://<server-lan-ip>:8080
   ```
   *(Additional flags such as `--from-docker` can be passed via `INSTALL_CLIENT_ARGS="--from-docker"`.)*
   - Installs into `~/.local/share/smartrss/` (using an atomic `mv` directory swap preserving `dist.prev` for rollback), `~/.config/smartrss-client/config.env`, `~/.config/systemd/user/smartrss-client.service`, and `~/.local/share/applications/smartrss.desktop`.
   - Ensure the server's `.env` includes `<server-lan-ip>:8080` in `ALLOWED_HOSTS` and `http://localhost:4180` in `CORS_ALLOW_ORIGINS`.
2. **Open the app** at `http://localhost:4180` (or launch **SmartRSS** from your desktop app menu) and paste a one-time pairing code generated via `smartrss pair create` or from another paired device's **Settings & Devices** tab.
3. **Rollback** to the previous client build if needed:
   ```bash
   ./scripts/install-client.sh --rollback
   ```
4. **LAN Fallback (`web` container)**: Opening `http://<server-lan-ip>:8080` directly in a browser on the home LAN also provides full connected reading and mutations (using `crypto.getRandomValues` UUIDs and fenced IndexedDB lease locking), without requiring the localhost runner.

## Local Development & Verification

Requires Python 3.12+ (for the backend test suite, `uv` automatically provisions Python 3.12 per `backend/.python-version`), `uv`, Node.js 22+ / npm (or `FRONTEND_RUNTIME=docker`), and Docker with daemon access (for `db-test` and Nginx `web` E2E):
```bash
make check               # prereqs + backend ruff/mypy/pytest/90% critical gate + runner-test (>=90%) + frontend lint/typecheck/vitest/90%&80% gates
make test-unit           # fast backend unit & boundary tests (no DB needed)
make test-int            # real PostgreSQL 16 (pgvector) integration tests
make runner-test         # localhost static runner unit & security tests (>= 90% branch+line coverage)
make frontend-test-cov   # frontend Vitest unit tests + two-tier 90%/80% coverage gate
make test-e2e            # Playwright E2E suite (1c-T09–1c-T15) against real API, PostgreSQL, localhost runner & Nginx web
```
