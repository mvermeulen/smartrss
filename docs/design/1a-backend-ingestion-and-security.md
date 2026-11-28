# Component Design 1a: Backend Foundation, Security, Polling & Extraction

> **Status:** Approved & Implemented — implementation **Approved** by project owner 2026-11-25 *(Design approved 2026-11-24 after [`1a` review](../reviews/2026-11-24-1a-component-design-review.md); implemented & [code-review findings](../reviews/2026-11-25-1a-implementation-review.md) `I-01`–`I-24` addressed 2026-11-25; live user-test fixes `E-01`–`E-13` from the [`1a`–`1c` user-test review](../reviews/2026-11-26-1a-1c-user-test-implementation-review.md) verified and **approved by project owner 2026-11-27**)*
> **Milestone:** `1a` — Backend Foundation, Auth, Polling & Extraction ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-FEED-01`–`06`, `FR-LLM-05` (cron/batch scheduler & CLI), `FR-STATE-01`–`02`, `NFR-SEC-01`, `NFR-SEC-02`, `NFR-SEC-03` (isolated HTML extraction), `NFR-OPS-01`
> **Related ADRs:** [ADR 0001](../decisions/0001-tech-stack-and-container-topology.md), [ADR 0003](../decisions/0003-postgres-backed-job-queue-and-async-llm-pipeline.md), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)

---

## 1. Scope & Boundaries

Milestone `1a` builds the complete, runnable, secure backend foundation and ingestion pipeline—verified against real PostgreSQL and real RSS/Atom feeds—without yet exposing the sync protocol (`1b`), SPA client (`1c`), local LLM enrichment (`1d`), clustering (`1e`), PDF mini-research (`1f`), or feedback evaluation (`1g`).

### In Scope for `1a`
1. Repository scaffolding, Docker Compose stack (`db`, `api`, `worker`, `web`, plus `--profile test` `db-test`), `Makefile`, and root `README.md`.
2. Initial Alembic schema for `1a` entities **plus** `sync_state` / `sync_changes` (with `SyncLogWriter` so `1a` writes are commit-ordered and the backup restore drill can rotate `instance_epoch` immediately).
3. LAN security & auth middleware: strict `Host` allow-list, strict CORS allow-list, JSON-only mutation guard, one-time device pairing (`smartrss pair` + `/api/v1/auth/pair`), revocable per-device bearer tokens.
4. Shared SSRF-hardened `SafeHttpClient` with DNS validation, IP pinning (preserving `Host` + TLS SNI), redirect-hop revalidation, byte/decompression caps, and per-domain rate limiting.
5. PostgreSQL `SKIP LOCKED` job queue with `dedupe_key` uniqueness, leases/heartbeats, stale-lease reaper, dead-letter inspection/retry, advisory-lock scheduler leadership, cron-friendly `smartrss worker run-once` batch CLI, and `pipeline_runs` / `stage_metrics` telemetry.
6. Polite feed polling (`poll_feed`), OPML import/export, isolated subprocess full-text HTML extraction (`extract_content`), configurable backfill cap (`N = 10`), unread expiration (`expire_items`), and operational retention cleanup.
7. Nightly `pg_dump` backup script (`scripts/backup.sh`) and tested restore drill (`scripts/restore-drill.sh` / `make restore-drill`).

### Explicitly Out of Scope for `1a`
- `/api/v1/sync/*` endpoints, conflict resolution, and FTS search API (`1b`).
- React/TypeScript/Dexie SPA & offline shell (`1c`) — `web` serves a minimal static placeholder page in `1a` while proxying `/api/v1/*`.
- `LLMAdapter`, `item_enrichments`, `prompt_configs`, `interest_profiles` (`1d`).
- `item_embeddings`, `clusters`, `cluster_exclusions` (`1e`).
- `research_sources` & PyMuPDF PDF extraction (`1f` — though `1a`'s isolated subprocess runner is designed to host `pymupdf` in `1f`).
- `feedback_events` & offline evaluation (`1g`).

---

## 2. Key `1a` Design Trade-offs & Decisions

Every choice below balances single-user homelab simplicity against SmartRSS's non-negotiable security, correctness, and milestone-composability invariants. Items marked **Confirm** are especially worth reviewing before approving `1a`.

| # | Decision Area | Chosen Approach | Alternative(s) Considered | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **T-01** *(S-05)* | **Muted feed item state** *(Confirmed)* | **Auto-mark `read` at ingest time** (`state='read'`, `read_at=fetched_at`, `read_state_updated_at=fetched_at`) while still archiving & extracting. Default for `1d`: extract full text (yes, for archive/FTS/dedup), skip LLM enrichment (no, to save GPU time unless explicitly requested). | Pure client-side filter leaving items `unread` in DB. | Keeps server and multi-device unread counts 100% consistent without duplicating mute filters in every client query; unmuting later does not flood the inbox with weeks of old items; integrates cleanly with `expire_items`. *(Aligned in [data-model.md](../data-model.md#31-item-state-machine-itemsstate--independent-itemsis_starred) §3.1–§3.2.)* |
| **T-02** | **Alembic schema phasing** *(Confirmed)* | **Milestone-scoped tables + early `sync_state`/`sync_changes`**: create `1a` tables + `sync_state`/`sync_changes` in `1a` (using fixed sentinel UUIDs for singleton entities per `data-model.md` §4.2); add `1b`/`1d`–`1g` tables in their own milestones. | (a) Create all 15+ tables upfront in `1a`; or (b) defer `sync_state`/`sync_changes` to `1b`. | Including `sync_state`/`sync_changes` in `1a` lets `1a`'s restore drill rotate `instance_epoch` and prevents retrofitting every `1a` write path in `1b`, while avoiding untested dead schema for `1d`–`1g`. |
| **T-03** | **Reverse proxy (`web`)** | **Nginx (`nginx:1.27-alpine`)** with explicit ~35-line config. | Caddy. | Plain-HTTP LAN entry point does not use auto-TLS (ADR 0002/0005/0006); Nginx avoids Caddy's auto-HTTPS friction, is tiny (~12 MB), and makes `Host` forwarding, CSP headers, and body caps completely explicit. Swapping to Caddy later if LAN TLS is ever enabled is a single-file change. |
| **T-04** | **SSRF IP pinning with HTTPS/SNI** | **Custom `httpcore.AsyncNetworkBackend`** wrapping `AnyIOBackend` inside `httpx.AsyncHTTPTransport`, intercepting `connect_tcp()`. | (a) Pre-flight DNS check before `httpx.get()` (vulnerable to DNS rebinding TOCTOU); (b) rewriting URL host to IP (breaks SNI/cert verification & IPv6). | Pinning at `connect_tcp()` validates all resolved A/AAAA IPs right before connecting and opens the socket to the validated IP while `httpcore` preserves the original hostname for HTTP `Host`, TLS SNI, and certificate validation. |
| **T-05** | **HTML parser isolation** | **Fresh per-job `spawn` subprocess** (`python -m smartrss.extract.runner`) with `start_new_session=True`, stripped env (no DB secrets), `RLIMIT_CPU=15s`, `RLIMIT_AS=512MB`, `RLIMIT_FSIZE=0`, wall-clock timeout `20s`. | (a) `fork()` from async worker (deadlock & secret-leak risk); (b) long-lived worker pool (risk of leaked C-library state across hostile docs). | At ~200 items/day (~8/hr), ~100 ms subprocess startup is negligible and guarantees zero shared heap, zero credential exposure, and clean crash recovery. `512 MB` `RLIMIT_AS` avoids false Linux virtual-memory OOMs in Python/`lxml`. *(See §13 for accepted `1a` network-namespace residual risk and `1f` `pymupdf` isolation requirement.)* |
| **T-06** | **PostgreSQL driver** | **SQLAlchemy 2.0 Async + `psycopg` (v3)** (`postgresql+psycopg://`). | `asyncpg` + separate `psycopg2` for Alembic. | One modern driver for FastAPI async, worker async, Alembic sync migrations, and native `pgvector.psycopg` support in `1e`. |
| **T-07** | **Job queue library vs. custom** | **Custom PostgreSQL `jobs` table (~180 LOC)** per ADR 0003 default. | `procrastinate`. | Our queue needs `feed_id`/`item_id`/`run_id` FKs, custom `dedupe_key` partial unique index, `worker run-once --stage --feed`, `stage_metrics` instrumentation, and dead-letter API; a small tailored module avoids shadow tables and framework coupling. |
| **T-08** | **Integration test DB** | **`docker compose --profile test` (`db-test` on `tmpfs`, port `5433`)** with `TEST_DATABASE_URL` override. | `testcontainers` per test session. | Keeps a warm `pgvector/pgvector:pg16` test instance on `tmpfs` for fast local `make check` runs (~1–3s) while remaining overrideable via `TEST_DATABASE_URL`. |

---

## 3. Repository Structure & Tooling

```text
smartrss/
├── AGENTS.md
├── README.md                              # Setup, Docker Compose, pairing, backup/restore quickstart
├── Makefile                               # Standard local verification & ops targets
├── docker-compose.yml                     # db, api, worker, web + --profile test (db-test)
├── .env.example                           # Documented infrastructure environment variables (prod defaults)
├── .env.dev.example                       # Optional dev-only overrides (e.g., Vite localhost:5173 CORS)
├── docs/                                  # Existing specs, ADRs, component designs
├── deploy/
│   ├── nginx/
│   │   └── nginx.conf                     # Single LAN entry point (/api/v1/* proxy + security headers)
│   └── systemd/
│       ├── smartrss-backup.service        # Example host systemd unit for nightly `make backup`
│       └── smartrss-backup.timer          # Example host systemd timer (03:15 daily)
├── scripts/
│   ├── backup.sh                          # Nightly pg_dump -Fc (umask 077) + 14-day rotation + optional rsync
│   ├── restore-drill.sh                   # Automated restore + instance_epoch rotation verification
│   └── check-critical-coverage.py         # Per-module >= 90% coverage gate over coverage.json (run by `make test-cov`)
└── backend/
    ├── Dockerfile                         # Multi-stage Python 3.12-slim image (shared by api & worker)
    ├── pyproject.toml                     # PEP 621 deps, ruff, mypy, pytest, coverage config, CLI entrypoint
    ├── alembic.ini
    ├── alembic/
    │   ├── env.py
    │   └── versions/
    │       └── 0001_milestone_1a_core.py  # Initial reversible migration
    ├── src/smartrss/
    │   ├── __init__.py
    │   ├── config.py                      # Pydantic BaseSettings (.env validation)
    │   ├── logging.py                     # Structured JSON logging setup
    │   ├── db/
    │   │   ├── engine.py                  # Async engine, sessionmaker, health check
    │   │   ├── models.py                  # SQLAlchemy 2.0 ORM models (1a tables)
    │   │   └── sync_log.py                # SyncLogWriter (sync_state FOR UPDATE + sync_changes) + strict test guard
    │   ├── security/
    │   │   ├── auth.py                    # Token/code hashing, pairing, bearer verification
    │   │   ├── middleware.py              # Host allow-list, JSON-only mutation guard
    │   │   └── safe_http.py               # SafeHttpClient, SafeNetworkBackend, DNS/IP validation, streaming gunzip, rate limiter
    │   ├── domain/
    │   │   ├── url_canon.py               # Pure URL canonicalization & tracking-param stripping
    │   │   ├── backoff.py                 # Pure HTTP 429/Cache-Control/error backoff + jitter math
    │   │   ├── expiration.py              # Pure effective_ttl_days calculation
    │   │   └── opml.py                    # Pure OPML 2.0 parser (`defusedxml`) & serializer
    │   ├── extract/
    │   │   ├── client.py                  # Parent-side isolated subprocess launcher + timeout/output caps
    │   │   └── runner.py                  # Subprocess entrypoint (rlimits + trafilatura extraction)
    │   ├── jobs/
    │   │   ├── queue.py                   # Enqueue, SKIP LOCKED claim, fenced heartbeat/complete/fail, reaper
    │   │   ├── locks.py                   # PostgreSQL advisory-lock helpers (dedicated conn & xact locks)
    │   │   └── metrics.py                 # pipeline_runs & stage_metrics recording + rollup queries
    │   ├── worker/
    │   │   ├── stages/
    │   │   │   ├── poll_feed.py           # Polite feed poller, backfill cap, updated-entry handling
    │   │   │   ├── extract_content.py     # Full-text fetch + isolated extraction + fallback
    │   │   │   ├── expire_items.py        # Batched unread expiration sweeper
    │   │   │   └── maintenance.py         # Stale-lease reaper & operational retention cleanup
    │   │   ├── scheduler.py               # Advisory-locked periodic scheduler
    │   │   └── runner.py                  # Daemon mode + one-shot batch mode (run-once)
    │   ├── api/
    │   │   ├── app.py                     # FastAPI app factory, lifespan, middleware wiring
    │   │   ├── deps.py                    # DB session, authenticated device dependency
    │   │   ├── schemas.py                 # Pydantic v2 request/response DTOs
    │   │   └── routes/
    │   │       ├── health.py              # GET /api/v1/health
    │   │       ├── auth.py                # Pairing & device management
    │   │       ├── categories.py          # Category CRUD
    │   │       ├── feeds.py               # Feed CRUD, health, manual poll, OPML import/export
    │   │       ├── items.py               # Minimal 1a item listing/detail inspection
    │   │       └── system.py              # System health, pipeline metrics, dead-letter queue
    │   └── cli.py                         # `smartrss` CLI (Typer/Click): pair, devices, worker, db
    └── tests/
        ├── conftest.py                    # Real-Postgres fixtures, schema isolation, sync-emission guard, factories
        ├── fixtures/                      # Sanitized RSS/Atom, OPML, hostile HTML/XSS/redirect/gzip-bomb fixtures
        ├── unit/                          # Pure domain, SSRF IP ranges, backoff, URL canon, OPML, auth
        ├── integration/                   # Real PostgreSQL: migrations, auth/Host/CORS, sync_log concurrency, queue/leases, poller, expiration, metrics, run-once
        └── boundary/                      # SafeHttpClient IP-pinning/redirects/byte-caps/gzip-bomb, isolated subprocess crash/CPU/RAM/stderr limits
```

### Standard `Makefile` Targets
- `make check`: Runs `lint` (`ruff check`, `ruff format --check`) + `typecheck` (`mypy`) + `test-cov` (unit + boundary + real-Postgres integration tests with coverage gates: `--cov-fail-under=80` overall, then `scripts/check-critical-coverage.py` enforcing `>= 90%` on each §12 critical module).
- `make test-unit`: Fast unit + boundary tests (no DB required).
- `make test-int`: Starts `db-test` (`docker compose --profile test up -d db-test`) and runs integration tests.
- `make up` / `make down`: Starts/stops the full Docker Compose stack.
- `make migrate`: Applies Alembic migrations to the local stack DB.
- `make backup` / `make restore-drill`: Executes backup and automated restore + epoch-rotation drill.

---

## 4. Container Topology & Configuration

### 4.1 `docker-compose.yml` Services

| Service | Image / Build | Ports | Key Configuration |
| :--- | :--- | :--- | :--- |
| **`db`** | `pgvector/pgvector:pg16` | Internal only (`5432`) | Named volume `pgdata`, `pg_isready` healthcheck, UTC timezone. |
| **`api`** | `./backend` (`uvicorn smartrss.api.app:create_app --factory --host 0.0.0.0 --port 8000`) | Internal only (`8000`) | Depends on `db` healthy; non-root UID `1000`; `read_only: true` + `/tmp` tmpfs. |
| **`worker`** | `./backend` (`smartrss worker run`) | None | Compose profile **`daemon`** (enabled by default via `COMPOSE_PROFILES=daemon` in `.env.example`; cron-mode installs drop it so the service is never started and cannot crash-loop); `restart: unless-stopped`; depends on `db` healthy; non-root UID `1000`; `init: true` (reaps orphaned extractor children); `mem_limit: 2g`, `pids_limit: 128` (bounds subprocess forks); `stop_grace_period: 40s`; `extra_hosts: ["host.docker.internal:host-gateway"]` (prepares for `1d`). Cron mode invokes the same image via `docker compose run --rm worker smartrss worker run-once` (`run` ignores profiles for the named service). |
| **`web`** | `nginx:1.27-alpine` | `${LAN_PORT:-8080}:80` *(single exposed LAN entry point)* | Mounts `deploy/nginx/nginx.conf` (read-only) + static placeholder (replaced by built SPA in `1c`). |
| **`db-test`** *(profile: `test`)* | `pgvector/pgvector:pg16` | `127.0.0.1:5433:5432` | `tmpfs: /var/lib/postgresql/data`, `fsync=off`, `synchronous_commit=off` for fast integration tests. |

### 4.2 Nginx Reverse Proxy (`deploy/nginx/nginx.conf`)
- Proxies `/api/` → `http://api:8000` with:
  - `proxy_set_header Host $http_host;` *(critical: preserves client `Host` header verbatim so FastAPI's `Host` allow-list catches DNS rebinding)*
  - `proxy_set_header X-Forwarded-For $remote_addr;`
  - `proxy_set_header X-Forwarded-Proto $scheme;`
  - `client_max_body_size 10m;` *(generous for large OPML imports while capping abuse)*
- Adds baseline security headers on all responses:
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `X-Frame-Options: DENY`
  - `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none';` *(refined in `1c` as needed for SPA assets)*

### 4.3 Environment Variables (`.env.example`)
| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `smartrss` / *(required)* / `smartrss` | Database credentials. |
| `DATABASE_URL` | `postgresql+psycopg://...` | SQLAlchemy async connection URL. |
| `LAN_PORT` | `8080` | Exposed host port for `web`. |
| `ALLOWED_HOSTS` | `localhost,localhost:8080,127.0.0.1,127.0.0.1:8080` | Comma-separated strict `Host` header allow-list (user adds e.g. `smartrss.lan:8080`, `192.168.1.50:8080`). |
| `CORS_ALLOW_ORIGINS` | `http://localhost:8080` | Strict CORS allow-list for Phase 1 `localhost` client origin + configured LAN origin. *(Vite dev origin `http://localhost:5173` lives in `.env.dev.example` only, never in production defaults.)* |
| `SSRF_BLOCKED_CIDRS` | *(empty; RFC1918 including default `172.16.0.0/12` Docker bridges always blocked)* | Additional CIDRs to block beyond standard private/loopback/link-local/CGNAT/ULA/NAT64/6to4 ranges (e.g., custom non-RFC1918 Docker bridges, home WAN IPv4 `/32`, or global IPv6 prefix). |
| `WORKER_MODE` | `daemon` | `daemon` (continuous `worker` container) or `cron` (external host cron/systemd invoking `smartrss worker run-once`). Running both simultaneously is **unsupported** (`smartrss worker run` refuses to start when `WORKER_MODE=cron`). |
| `COMPOSE_PROFILES` | `daemon` | Starts the continuous `worker` service. Set to empty for `WORKER_MODE=cron` so `docker compose up` does not start (and crash-loop) the daemon. |
| `WORKER_IO_CONCURRENCY` | `4` | Concurrent I/O jobs (`poll_feed`, `extract_content`) per worker process. |
| `WORKER_POLL_INTERVAL_SECONDS` | `30` | Scheduler tick interval in daemon mode. |
| `DEFAULT_POLL_INTERVAL_MINUTES` | `60` | Initial seed value only for `app_settings.default_poll_interval_minutes` during first migration; DB is authoritative thereafter. |
| `DEFAULT_BACKFILL_CAP` | `10` | Initial seed value only for `app_settings.backfill_cap` during first migration; DB is authoritative thereafter. |
| `DEFAULT_EXPIRATION_DAYS` | `30` | Initial seed value only for `app_settings.default_expiration_days` (`0` = never expire) during first migration; DB is authoritative thereafter. |
| `BACKUP_DIR` | `./backups` | Host directory for nightly `pg_dump` files. |
| `BACKUP_RSYNC_TARGET` | *(empty / optional)* | Optional `user@lan-host:/path` for off-host LAN copy. |
| `BACKUP_REMOTE_RETENTION_DAYS` | `30` | Independent retention window for the off-host copy (pruned remotely; never mirrored with `--delete`). |

---

## 5. Database Schema & Migration (`0001_milestone_1a_core.py`)

All timestamps are `TIMESTAMPTZ` stored in UTC. All UUID primary keys use `UUID` (`uuid4` generated in application code or `gen_random_uuid()` default).

### 5.1 Tables Created in `1a`

1. **`devices`** & **`pairing_codes`** (per [data-model.md §4.1](../data-model.md#41-devices--pairing_codes))
   - `devices`: `id (UUID PK)`, `name (TEXT NOT NULL)`, `platform (TEXT NOT NULL CHECK (platform IN ('ubuntu','android','web')))`, `token_hash (TEXT UNIQUE NOT NULL)`, `created_at (TIMESTAMPTZ NOT NULL DEFAULT now())`, `last_seen_at (TIMESTAMPTZ NULL)`, `revoked_at (TIMESTAMPTZ NULL)`.
   - `pairing_codes`: `id (UUID PK)`, `code_hash (TEXT UNIQUE NOT NULL)`, `device_name (TEXT NOT NULL)`, `platform (TEXT NOT NULL DEFAULT 'ubuntu' CHECK (platform IN ('ubuntu','android','web')))`, `expires_at (TIMESTAMPTZ NOT NULL)`, `consumed_at (TIMESTAMPTZ NULL)`, `created_by_device_id (UUID NULL REFERENCES devices(id) ON DELETE SET NULL)`, `created_at (TIMESTAMPTZ NOT NULL DEFAULT now())`.

2. **`sync_state`** & **`sync_changes`** (per [data-model.md §4.2](../data-model.md#42-sync_state--sync_changes), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md))
   - `sync_state`: `id (SMALLINT PK CHECK (id = 1))`, `current_seq (BIGINT NOT NULL DEFAULT 0)`, `instance_epoch (UUID NOT NULL DEFAULT gen_random_uuid())`, `updated_at (TIMESTAMPTZ NOT NULL DEFAULT now())`. Seeded with 1 row in migration `0001`.
   - `sync_changes`: `seq (BIGINT PK)`, `entity_type (TEXT NOT NULL)`, `entity_id (UUID NOT NULL)`, `op (TEXT NOT NULL CHECK (op IN ('upsert','delete')))`, `changed_at (TIMESTAMPTZ NOT NULL DEFAULT now())`.
     - **Singleton sentinel UUIDs**: `app_settings` uses fixed constant `APP_SETTINGS_ENTITY_ID = UUID('00000000-0000-0000-0000-000000000001')` (matching `data-model.md` §4.2, alongside `INTEREST_PROFILE_ENTITY_ID = UUID('00000000-0000-0000-0000-000000000002')` for `1b`/`1d`).
   - Index: `ix_sync_changes_entity (entity_type, entity_id)`, `ix_sync_changes_changed_at (changed_at)` (for 90-day pruning).
   - **`SyncLogWriter` invariant & structural guard**:
     - Every service/stage method that mutates a client-synced entity (`category`, `feed` on synced columns, `item`, `app_settings` on client-visible columns) calls `await sync_log.record(session, events)` **as the final step right before `session.commit()`**, after all slow I/O (HTTP fetches, subprocess extraction) has already completed. `record()` executes `SELECT current_seq FROM sync_state WHERE id = 1 FOR UPDATE`, increments `current_seq` by `len(events)`, updates `sync_state`, and bulk-inserts the `sync_changes` rows.
     - **Diff-gated `feed` emission**: Emit a `feed` `upsert` **only** when at least one client-synced feed column actually changed (`feed_url`, `site_url`, `title`, `status`, `health_state`, `consecutive_errors`, `last_error`, `category_id`, `expiration_days_override`, `fetch_full_text`, `research_policy`, `curation_instructions`, `config_version`, `field_updated_at`). Internal poll bookkeeping columns (`etag`, `last_modified`, `last_fetched_at`, `last_success_at`, `next_fetch_at`, `initial_backfill_done`) do **not** trigger a `feed` `upsert` when nothing client-visible changed.
     - **Server-side LWW stamping**: Every server-side write to an LWW-tracked field (e.g., `DELETE /categories/{id}` clearing `feeds.category_id`, `POST /feeds` reactivating `status`, `PATCH /feeds/{id}` updating structured fields) MUST stamp `field_updated_at[field] = now()` in the same transaction.
     - **Structural test guard (ORM + Core)**: Two complementary checks, enabled automatically in tests and via `SYNCLOG_STRICT_GUARD=true`, raise `MissingSyncEventError` if a synced mutation commits without a matching `SyncLogWriter.record()` call in that transaction:
       1. A SQLAlchemy `before_commit` session listener tracks dirty/new synced ORM entities (checking synced-column diffs on `Feed` and `AppSettings`).
       2. Because `poll_feed`, `expire_items`, and the failure hooks write through **Core SQL** (column-scoped `UPDATE feeds`, `INSERT … ON CONFLICT` into `items`) which ORM dirty-tracking cannot see, all Core `INSERT`/`UPDATE`/`DELETE` statements on synced tables (`categories`, `feeds`, `items`, `app_settings`) go through `sync_log.execute_synced(session, stmt, events_fn)` helpers in `db/sync_log.py`. A test-only `before_cursor_execute` engine hook flags any `INSERT`/`UPDATE`/`DELETE` against a synced table issued outside these helpers or ORM flush. *(Poll-bookkeeping-only `UPDATE feeds` statements pass `events_fn` returning `[]` explicitly, so the "no event" decision is always visible in code.)*

3. **`categories`** (per [data-model.md §4.3](../data-model.md#43-categories))
   - `id (UUID PK)`, `name (TEXT NOT NULL)`, `sort_order (INT NOT NULL DEFAULT 0)`, `created_at`, `updated_at`, `deleted_at (TIMESTAMPTZ NULL)`.
   - Partial unique index: `uq_categories_active_name ON categories (LOWER(name)) WHERE deleted_at IS NULL`.

4. **`feeds`** (per [data-model.md §4.4](../data-model.md#44-feeds))
   - All columns from `data-model.md` §4.4: `id`, `category_id (FK → categories.id ON DELETE SET NULL)`, `feed_url (TEXT UNIQUE NOT NULL)`, `site_url`, `title`, `status (CHECK IN ('active','paused','muted','unsubscribed'))`, `health_state (CHECK IN ('healthy','degraded','broken','gone'))`, `poll_interval_minutes (INT NOT NULL CHECK (poll_interval_minutes >= 5))` *(application insert reads default from `app_settings.default_poll_interval_minutes`)*, `etag`, `last_modified`, `last_fetched_at`, `last_success_at`, `next_fetch_at`, `consecutive_errors (INT NOT NULL DEFAULT 0)`, `last_error`, `initial_backfill_done (BOOLEAN NOT NULL DEFAULT FALSE)`, `expiration_days_override (INT NULL CHECK (expiration_days_override >= 0))`, `fetch_full_text (BOOLEAN NOT NULL DEFAULT TRUE)`, `research_policy (JSONB NOT NULL DEFAULT '{"enabled":false,"fetch_pdfs":false,"guidance":"","max_hops":1}')`, `curation_instructions (TEXT NOT NULL DEFAULT '')`, `field_updated_at (JSONB NOT NULL DEFAULT '{}')`, `config_version (INT NOT NULL DEFAULT 1)` *(increments **only** on `curation_instructions` writes, independent of `field_updated_at` LWW fields)*, `created_at`, `updated_at`.
   - Index: `ix_feeds_due_poll ON feeds (next_fetch_at) WHERE status IN ('active', 'muted') AND health_state <> 'gone'`.

5. **`items`** (per [data-model.md §4.5](../data-model.md#45-items))
   - Columns for `1a`: `id`, `feed_id (FK → feeds.id NOT NULL)`, `guid (TEXT NOT NULL)`, `raw_url (TEXT NOT NULL)`, `canonical_url (TEXT NOT NULL)`, `title (TEXT NOT NULL)`, `author`, `published_at (TIMESTAMPTZ NULL)`, `fetched_at (TIMESTAMPTZ NOT NULL)`, `raw_summary (TEXT NULL)`, `extracted_text (TEXT NULL)`, `extraction_status (TEXT NOT NULL DEFAULT 'pending' CHECK IN ('pending','ok','fallback_rss','failed'))`, `entry_hash (TEXT NULL)` *(SHA-256 of normalized feed-entry `title + "\n" + raw_summary`, owned by `poll_feed` for update detection and never overwritten by extraction)*, `content_hash (TEXT NULL)` *(SHA-256 of normalized `title + "\n" + (extracted_text or raw_summary)`, updated by `extract_content` on `ok` and used for `1e` dedup)*, `state (TEXT NOT NULL DEFAULT 'unread' CHECK IN ('unread','read','expired'))`, `is_starred (BOOLEAN NOT NULL DEFAULT FALSE)`, `read_at`, `starred_at`, `expired_at`, `read_state_updated_at (TIMESTAMPTZ NOT NULL)` *(initialized to `fetched_at` on every insert)*, `starred_updated_at (TIMESTAMPTZ NOT NULL)` *(initialized to `fetched_at` on every insert)*, `unexpired_at (TIMESTAMPTZ NULL)`, `created_at`, `updated_at`.
   - *(Note: `search_vector` is added in `1b` alongside FTS; `cluster_id` FK is added in `1e` alongside `clusters`.)*
   - Constraints & indexes:
     - `uq_items_feed_guid UNIQUE (feed_id, guid)`
     - `ix_items_feed_canonical_url (feed_id, canonical_url)` *(supports unstable-GUID fallback)*
     - `ix_items_canonical_url (canonical_url)` *(prepares for `1e` cross-feed exact dedup)*
     - `ix_items_expiration ON items (COALESCE(unexpired_at, fetched_at)) WHERE state = 'unread' AND is_starred = FALSE` *(fast expiration sweep)*
     - `ix_items_feed_fetched (feed_id, fetched_at DESC)`

6. **`jobs`**, **`pipeline_runs`**, **`stage_metrics`** (per [data-model.md §4.9](../data-model.md#49-prompt_configs-interest_profiles-sync_mutations-jobs-pipeline_runs-stage_metrics-app_settings))
   - `pipeline_runs`: exact schema from `data-model.md` §4.9 (`id`, `trigger`, `stages`, `status`, `started_at`, `finished_at`, `duration_ms`, counters, `total_llm_ms`, `notes`).
   - `jobs`: exact schema from `data-model.md` §4.9, with `run_id UUID NULL REFERENCES pipeline_runs(id) ON DELETE SET NULL` (so pruning 180-day-old `pipeline_runs` never fails on retained dead-letter `jobs`) and indexes:
     - `uq_jobs_active_dedupe UNIQUE (job_type, dedupe_key) WHERE status IN ('queued', 'running')`
     - `ix_jobs_claim ON jobs (priority ASC, run_after ASC, id ASC) WHERE status = 'queued'`
     - `ix_jobs_running_lease ON jobs (lease_expires_at) WHERE status = 'running'`
     - `ix_jobs_done_finished ON jobs (finished_at) WHERE status = 'done'` *(30-day retention pruning)*
   - `stage_metrics`: exact schema from `data-model.md` §4.9, with `run_id UUID NULL REFERENCES pipeline_runs(id) ON DELETE SET NULL`, indexed on `(job_type, created_at)` and `(feed_id, created_at)`.

7. **`app_settings`**
   - Single-row configuration table (`id SMALLINT PK CHECK (id = 1)`), `default_expiration_days (INT NOT NULL DEFAULT 30)`, `backfill_cap (INT NOT NULL DEFAULT 10)`, `default_poll_interval_minutes (INT NOT NULL DEFAULT 60)`, `last_retention_cleanup_at (TIMESTAMPTZ NULL)` *(internal operational timestamp for daily retention scheduling across daemon and `run-once` modes; not client-synced and does not emit an `app_settings` sync change)*, `updated_at (TIMESTAMPTZ NOT NULL DEFAULT now())`. Seeded in migration `0001` from `DEFAULT_EXPIRATION_DAYS`, `DEFAULT_BACKFILL_CAP`, and `DEFAULT_POLL_INTERVAL_MINUTES` (with DB authoritative thereafter). *(LLM/clustering/research settings columns added in `1d`–`1f`.)*

---

## 6. Security & Authentication Middleware (`NFR-SEC-01`)

### 6.1 Middleware Execution Pipeline (Order Matters)

```mermaid
flowchart LR
    Req["Incoming HTTP Request"] --> Host["1. HostAllowListMiddleware\n(blocks DNS rebinding;\nexempts GET /api/v1/health)"]
    Host --> CORS["2. CORSMiddleware\n(strict origin allow-list)"]
    CORS --> JSON["3. JsonMutationGuardMiddleware\n(POST/PUT/PATCH/DELETE must be application/json)"]
    JSON --> Route["4. FastAPI Router"]
    Route --> Public["Public: /api/v1/health, /api/v1/auth/pair\n(pair is rate-limited)"]
    Route --> Auth["Protected: BearerAuthDep\n(SHA-256 token lookup, revoked check)"]
```

1. **`HostAllowListMiddleware`**:
   - Extracts `Host` header (lowercased, trimmed). Rejects missing or non-matching `Host` with `400 Bad Request` (`{"detail": "Invalid Host header"}`) before any route or CORS logic runs.
   - Matches strictly against `ALLOWED_HOSTS` (exact `host` or `host:port` entries; no wildcards).
   - **Healthcheck exemption**: Exempts `GET /api/v1/health` (read-only, unauthenticated, returns `{status, db, version}` with no secrets) so internal Docker Compose healthchecks calling `http://api:8000/api/v1/health` succeed without adding container hostnames to `ALLOWED_HOSTS`. All other routes (including `/api/v1/auth/pair`) enforce the strict allow-list.
2. **`CORSMiddleware`**:
   - `allow_origins = settings.cors_allow_origins` (never `*`), `allow_credentials = False`, `allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]`, `allow_headers = ["Authorization", "Content-Type"]`.
3. **`JsonMutationGuardMiddleware`**:
   - For any request with method in `{"POST", "PUT", "PATCH", "DELETE"}`:
     - If `Content-Length` > 0 (or chunked body present) — or unconditionally on endpoints expecting a body — requires `Content-Type` media type `application/json` (ignoring `; charset=utf-8` parameters). Rejects `application/x-www-form-urlencoded`, `multipart/form-data`, `text/plain`, etc. with `415 Unsupported Media Type`.
     - **Design choice**: OPML import (`POST /api/v1/feeds/import-opml`) takes a JSON body `{"opml_xml": "<opml>...</opml>", "default_category_id": null}` rather than `multipart/form-data`, keeping **zero exceptions** to the JSON-only CSRF rule.
4. **Device Pairing & Per-Device Bearer Auth**:
   - **Token generation**: `secrets.token_urlsafe(32)` (256 bits of entropy); only `hashlib.sha256(token.encode()).hexdigest()` is persisted in `devices.token_hash`. Raw token is returned **once** on pair exchange and never logged.
   - **Pairing code generation**:
     - Bootstrap first device via CLI: `docker compose exec api smartrss pair create --name "ThinkPad" --platform ubuntu`.
     - Subsequent devices via CLI or `POST /api/v1/auth/pairing-codes` (authenticated).
     - Generates a 16-char Crockford Base32 code formatted `XXXX-XXXX-XXXX-XXXX` (~80 bits entropy; normalized by stripping hyphens/spaces and uppercasing before SHA-256 hashing), TTL = **5 minutes**, single-use.
   - **Pairing exchange (`POST /api/v1/auth/pair`)**:
     - Protected by a **single global sliding-window rate limiter** (**5 attempts per 5 minutes** across all callers → `429 Too Many Requests` with `Retry-After`). A single global bucket is intentional for this single-user homelab: at ~80 bits of pairing-code entropy it stops brute-force attempts cleanly while avoiding any `X-Forwarded-For` proxy-trust surface or container-IP misconfiguration behind Nginx.
     - Atomically consumes code: `UPDATE pairing_codes SET consumed_at = now() WHERE code_hash = :h AND consumed_at IS NULL AND expires_at > now() RETURNING *`. Prevents race-condition double redemption.
     - Inserts `devices` row and returns `{device_id, device_name, platform, bearer_token}`.
   - **Request authentication (`get_current_device` dependency)**:
     - Parses `Authorization: Bearer <token>`. Missing/malformed header, unknown hash, or `revoked_at IS NOT NULL` → `401 Unauthorized` (`WWW-Authenticate: Bearer`).
     - Updates `devices.last_seen_at` only if `last_seen_at IS NULL OR last_seen_at < now() - interval '60 seconds'` (avoids unnecessary write amplification on burst reads).

---

## 7. Shared `SafeHttpClient` (SSRF Prevention, `NFR-SEC-02`)

Every outbound server fetch (`poll_feed`, `extract_content`, and later `mini_research`) MUST instantiate/share `SafeHttpClient`. Direct calls to `httpx`, `urllib`, `requests`, or `feedparser.parse(url)` (which fetches internally!) are **forbidden** — `feedparser.parse()` is called **only** on bytes already fetched by `SafeHttpClient`.

### 7.1 IP-Pinning Transport Architecture

```python
# Conceptual contract inside smartrss/security/safe_http.py
class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend, policy: SSRFPolicy): ...

    async def connect_tcp(self, host: str, port: int, timeout=None, local_address=None, socket_options=None):
        # 1. Resolve host via policy.resolver (injectable for unit/boundary tests)
        ips = await self.policy.resolve_and_validate(host, port)
        # 2. Try validated IPs in order; pass the LITERAL IP to inner.connect_tcp()
        #    httpcore keeps the original `host` for HTTP Host header & TLS SNI/cert verification!
        for ip in ips:
            try:
                return await self.inner.connect_tcp(str(ip), port, timeout=timeout, ...)
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
        raise SSRFConnectionError(...)
```

- **Exception mapping (`E-01`)**: `SafeTransport.handle_async_request()` and `_PinnedStream` (`__aiter__`, `aclose`) map `httpcore` exceptions into the `SafeHttpError` hierarchy (`httpcore.TimeoutException` → `SafeHttpTimeoutError`, `httpcore.NetworkError`/`ProxyError` → `SSRFConnectionError`, `httpcore.ProtocolError`/`UnsupportedProtocol` → `SafeHttpError`) so raw `httpcore` errors never escape `SafeHttpClient.fetch()`.

### 7.2 Validation Rules & Guardrails
- **Scheme & fetch URL check (`SSRFPolicy.validate_url`)**: Only `http` and `https`. Rejects `file://`, `ftp://`, `gopher://`, `data://`, etc., embedded userinfo (`http://user:pass@host`), non-standard control characters, missing hostnames, and disallowed ports. Kept permissive on query/path punctuation so legitimate fetched URLs, redirects, and article links (e.g., RSS-Bridge `?url=https://…`, `?tags[]=python`, `?x=1|2`, archive `/web/2024/https://…`) work cleanly (`E-10-F1`).
- **Feed entry-time paste validation (`validate_feed_url_syntax`)**: Shared across `POST /api/v1/feeds`, `PATCH /api/v1/feeds/{id}`, and `POST /api/v1/feeds/import-opml` (`E-10`, `E-10-F1`) — NOT on `SafeHttpClient.fetch()`. Rejects whitespace, `<>"`, unescaped `[`/`]` in the URL path, and pasted Markdown link joins (`](http(s)://` / `)(http(s)://`) while allowing legitimate query brackets/pipes/embedded schemes and archive paths.
- **Port check**: Allow ports `80` and `443` by default (configurable allow-list in `SSRFPolicy` for test fixtures).
- **IP denylist (fail-closed)**: Every A and AAAA address returned for `host` is validated. If **any** resolved IP is forbidden (preventing dual-homed DNS trickery where one A record is public and another is `127.0.0.1`), the request is rejected with `SSRFBlockedError`:
  - **Embedded-IPv4 transition addresses (unwrapped & checked against all IPv4 rules)**:
    - IPv4-mapped IPv6 (`::ffff:0:0/96`): unwrapped via `ip.ipv4_mapped`.
    - 6to4 (`2002::/16`): unwrapped via `ip.sixtofour` (rejects embedded private/loopback/link-local/CGNAT IPv4).
    - NAT64 well-known prefix (`64:ff9b::/96`): low 32 bits unwrapped as `IPv4Address(int(ip) & 0xFFFFFFFF)` and checked against all IPv4 rules; NAT64 local-use prefix (`64:ff9b:1::/48`, RFC 8215) is blocked unconditionally.
  - Standard Python `ipaddress` checks: `is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved`, `is_unspecified`.
  - Explicit extra CIDRs:
    - CGNAT: `100.64.0.0/10`
    - IPv6 ULA: `fc00::/7`
    - Benchmark/test nets: `198.18.0.0/15`, `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`
    - Configured Docker bridge CIDRs (`SSRF_BLOCKED_CIDRS`).
  - *(See §13 for accepted residual risk regarding hairpin NAT / host WAN IPv4 & global IPv6 addresses.)*
- **Manual redirect loop**:
  - `httpx.AsyncClient(follow_redirects=False, transport=SafeTransport(...))`.
  - Follows `301`, `302`, `303`, `307`, `308` up to **5 hops** max, resolving relative `Location` headers via `urljoin`, and re-running scheme + DNS/IP validation on every hop.
  - Returns a `FetchResult(status_code, final_url, permanent_redirect_url, headers, body_bytes, content_type)` where `permanent_redirect_url` is set **only** if *every* redirect hop in the chain was permanent (`301`/`308`), matching RFC 9110.
- **Timeouts & size / streaming decompression-bomb caps**:
  - Per-operation `httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)` **wrapped in `async with asyncio.timeout(total):`** (default `total = 30s`) across the entire redirect + body-streaming lifecycle so a slow-drip server cannot bypass the total budget.
  - Advertises **`Accept-Encoding: gzip, deflate`** only (never `br`/`zstd`). Inspects the response `Content-Encoding` header and accepts only absent/`identity`, `gzip`, `x-gzip`, or `deflate`; rejects any other or chained encoding with `UnsupportedEncodingError` (permanent fetch error, not retried).
  - Checks `Content-Length` before streaming (rejecting compressed wire payloads exceeding `max_bytes`), then streams raw wire chunks via **`response.aiter_raw()`** (NOT `aiter_bytes()`, because `httpx` decompresses full network chunks without a `max_length` bound prior to yielding):
    - Counts raw wire bytes against `max_bytes` (default **5 MB** for feeds/HTML; caller-configurable up to **20 MB** for PDFs in `1f`).
    - For `gzip`/`x-gzip`, uses `zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)` and continues across concatenated gzip members (feeding `unused_data` into a new decompressor, sharing the same running total); for `deflate`, tries zlib-wrapped (`wbits=zlib.MAX_WBITS`) and falls back to raw deflate (`wbits=-zlib.MAX_WBITS`) if the first chunk fails the header check. It incrementally feeds each wire chunk into the decompressor calling **`decompressor.decompress(chunk, max_length=remaining + 1)`** (where `remaining = max_bytes - decompressed_total`). If `len(out) > remaining` or `decompressor.unconsumed_tail` remains after hitting `max_bytes` (or final `flush(remaining + 1)` overflows), aborts the response stream immediately and raises `ResponseTooLargeError`. A single 64 KB hostile gzip chunk expanding to 64 MB+ is stopped at `max_bytes` without ever allocating the full expansion in worker RAM.
- **Per-Domain Politeness (`DomainRateLimiter`)**:
  - In-process `asyncio.Lock` + monotonic timestamp per normalized host: guarantees **concurrency = 1 per domain** and at least **1.5 seconds** between requests to the same domain across `extract_content` (and `mini_research` in `1f`).

---

## 8. Job Queue, Worker Modes & Metrics (`ADR 0003`)

### 8.1 PostgreSQL Job Queue (`smartrss/jobs/queue.py`)
- **Enqueue (`enqueue`)**:
  - Default mode (scheduled/automatic dispatch):
    ```sql
    INSERT INTO jobs (job_type, dedupe_key, payload, feed_id, item_id, run_id, priority, run_after)
    VALUES (:job_type, :dedupe_key, :payload, :feed_id, :item_id, :run_id, :priority, :run_after)
    ON CONFLICT (job_type, dedupe_key) WHERE status IN ('queued', 'running')
    DO NOTHING
    RETURNING id;
    ```
  - Urgent/manual bump mode (`bump_existing=True`, used by `POST /api/v1/feeds/{id}/poll` so a manual poll isn't silently dropped behind a backed-off queued job):
    ```sql
    INSERT INTO jobs (job_type, dedupe_key, payload, feed_id, item_id, run_id, priority, run_after)
    VALUES (:job_type, :dedupe_key, :payload, :feed_id, :item_id, :run_id, :priority, :run_after)
    ON CONFLICT (job_type, dedupe_key) WHERE status IN ('queued', 'running')
    DO UPDATE SET priority = LEAST(jobs.priority, EXCLUDED.priority),
                  run_after = LEAST(jobs.run_after, EXCLUDED.run_after),
                  updated_at = now()
    WHERE jobs.status = 'queued'
    RETURNING id;
    ```
    *(If the conflicting job is already `running`, the `WHERE jobs.status = 'queued'` guard is a safe no-op.)*
  - Dedupe keys in `1a`:
    - `poll_feed`: `feed:{feed_id}`
    - `extract_content`: `item:{item_id}`
    - `expire_items`: `global:expire_items`
    - `retention_cleanup`: `global:retention_cleanup`
- **Claim (`claim_next`)**:
  ```sql
  WITH next_job AS (
      SELECT id FROM jobs
      WHERE status = 'queued'
        AND run_after <= now()
        AND job_type = ANY(:job_types)
      ORDER BY priority ASC, run_after ASC, id ASC
      FOR UPDATE SKIP LOCKED
      LIMIT 1
  )
  UPDATE jobs j
  SET status = 'running',
      locked_by = :worker_id,
      locked_at = now(),
      started_at = now(),
      heartbeat_at = now(),
      lease_expires_at = now() + make_interval(secs => :lease_seconds),
      attempts = attempts + 1,
      updated_at = now()
  FROM next_job
  WHERE j.id = next_job.id
  RETURNING j.*;
  ```
  - Default `lease_seconds = 120`. While a job runs, a background heartbeat coroutine renews `heartbeat_at = now(), lease_expires_at = now() + 120s` every **30 seconds**.
- **Lease-Fenced Heartbeat / Complete / Retry / Dead-Letter**:
  - **Fencing invariant**: Every heartbeat, completion, retry-requeue, dead-letter, and graceful-shutdown release executes conditionally:
    `WHERE id = :id AND status = 'running' AND locked_by = :worker_id AND attempts = :claimed_attempt`.
    If `rowcount == 0`, the worker lost its lease (reclaimed by the reaper while disconnected or stalled); it logs `warning("job_lease_lost")`, skips writing `stage_metrics`, and aborts further state transitions without clobbering the new attempt.
  - On success: `status = 'done'`, `finished_at = now()`, `queue_wait_ms = EXTRACT(EPOCH FROM (started_at - run_after))*1000` (clamped `>= 0`), `duration_ms`, plus inserts a `stage_metrics` row.
  - On transient error with `attempts < max_attempts` (default `5`): resets `status = 'queued'`, clears lock/lease, sets `run_after = now() + min(60 * 2^(attempts-1), 3600)s`, records `last_error`, inserts a `stage_metrics` row (`status='failed'`).
  - On exhausted attempts (`attempts >= max_attempts`) or permanent fatal error: `status = 'failed'` (dead-letter), `finished_at = now()`, `last_error`, visible in `/api/v1/system/jobs/dead-letter` for manual retry or dismiss.
  - **Stage-specific failure / dead-letter hooks**:
    - **`poll_feed` unexpected exception / dead-letter**: Whenever a `poll_feed` job fails with an unexpected exception (on retry or final dead-letter, including reaper dead-lettering), execute — in a **fresh session/transaction** (the stage's own transaction is rolled back first and never reused) using the same `SELECT … FOR UPDATE` re-read as the non-200 paths — a column-scoped `UPDATE feeds` that increments `consecutive_errors`, transitions `health_state` (`'broken'` if `>= 5` else `'degraded'`), sets `last_error`, and advances `next_fetch_at = now() + exponential_backoff(poll_interval, errors) + jitter` (emitting a diff-gated `feed` `upsert` via `SyncLogWriter` when `health_state`/`last_error`/`consecutive_errors` changes). This ensures an unhandled parser/DB error degrades Feed Health and backs off instead of triggering an infinite 30-second re-enqueue loop.
    - **`extract_content` exhausted retries**: On the final attempt (`attempts >= max_attempts`), update `items.extraction_status = 'failed'` (preserving `raw_summary`) and emit an `item` `upsert` via `SyncLogWriter` so items never remain stuck in `pending` forever.
- **Stale-Lease Reaper**:
  - Runs every 60s in daemon mode and unconditionally at the start of every `worker run-once` invocation, under transaction advisory lock `LOCK_REAPER`: finds `status = 'running' AND lease_expires_at < now()`. Resets rows with `attempts < max_attempts` to `queued` (with backoff) and transitions rows with `attempts >= max_attempts` to `failed` (`last_error = 'lease expired (worker crash/timeout)'`, invoking the `poll_feed`/`extract_content` dead-letter hooks above for affected rows).

### 8.2 Advisory Lock Keys (`smartrss/jobs/locks.py`)
Fixed 64-bit integer constants with strict connection-pool safety:
- **Per-tick transaction locks (`pg_try_advisory_xact_lock(key)`)** — acquired inside a short DB transaction and released automatically on commit/rollback (zero risk of leaking locks across pooled connections):
  - `LOCK_SCHEDULER = 0x5352_0001` — periodic daemon scheduler tick (prevents duplicate dispatch across replicas).
  - `LOCK_REAPER = 0x5352_0002` — stale-lease reaper pass.
- **Process-wide session lock (`pg_try_advisory_lock(key)`)** — held strictly on a **dedicated checked-out `AsyncConnection`** (`async with engine.connect() as lock_conn:`) that is never returned to the pool or used for unrelated queries until `pg_advisory_unlock(key)` executes in a `finally` block (and automatically released by PostgreSQL if the process crashes):
  - `LOCK_RUN_ONCE = 0x5352_0003` — prevents overlapping `smartrss worker run-once` cron invocations from racing batch accounting (if lock is held, the overlapping invocation logs a clear message and exits cleanly with code `0` without double-processing).

### 8.3 Execution Modes (`smartrss worker run` & `smartrss worker run-once`)
- **Mode Exclusivity (`WORKER_MODE=daemon|cron`)**: Running the continuous daemon and external host cron `run-once` simultaneously is **unsupported**. In `daemon` mode (default), Docker Compose runs `worker` continuously; in `cron` mode, the `daemon` compose profile is disabled (`COMPOSE_PROFILES=`) so the `worker` service is never started; `smartrss worker run` additionally refuses to start (exit `78`, `EX_CONFIG`) with a clear configuration message as a safety net, and the host runs `docker compose run --rm worker smartrss worker run-once` on its own schedule.
- **Daemon Mode (`smartrss worker run`)**:
  - Runs an async supervisor managing:
    1. Scheduler task (every `WORKER_POLL_INTERVAL_SECONDS` under `LOCK_SCHEDULER` xact lock): dispatches due `poll_feed` jobs (`next_fetch_at <= now()`), hourly `expire_items`, and daily `retention_cleanup` (whenever `app_settings.last_retention_cleanup_at` is `NULL` or `> 24h` old).
    2. Reaper task (every 60s under `LOCK_REAPER` xact lock): reclaims stale leases.
    3. `WORKER_IO_CONCURRENCY` (default `4`) I/O worker tasks claiming `['poll_feed', 'extract_content', 'expire_items', 'retention_cleanup']`.
  - Handles `SIGTERM`/`SIGINT` gracefully (aligned with `worker`'s `stop_grace_period: 40s`): stops claiming new jobs, waits up to **30s** for in-flight jobs to finish, and for any still-running jobs when the grace window expires, cancels their tasks and releases their DB rows back to `status = 'queued', locked_by = NULL, locked_at = NULL, lease_expires_at = NULL, run_after = now(), attempts = GREATEST(attempts - 1, 0)` (using the full lease fence `WHERE id = :id AND status = 'running' AND locked_by = :worker_id AND attempts = :claimed_attempt`, per §8.1) so work resumes immediately on restart instead of waiting for lease expiry.
- **One-Shot Batch Mode (`smartrss worker run-once`)**:
  - CLI options: `--stage all|poll|extract|expire|maintenance` (default `all`), `--feed <uuid>` (optional single-feed filter), `--force-poll` (ignore `next_fetch_at`), `--max-duration <seconds>` (default **`1800`** = 30 minutes), `--json` (machine-readable summary for cron logs).
  - Workflow:
    1. Acquires `LOCK_RUN_ONCE` on a dedicated `AsyncConnection` (if already held, exits `0` with `{"status": "skipped_locked"}`).
    2. **Always runs the stale-lease reaper (`reap_stale_leases`) first** so any leases orphaned by a previously killed `run-once` invocation are reclaimed immediately.
    3. Creates a `pipeline_runs` row (`trigger='cron_cli'`, `stages=[...]`, `status='running'`).
    4. Dispatches due jobs for the requested stages (tagging them with `run_id`). When `--stage all` is selected, also dispatches `retention_cleanup` if `app_settings.last_retention_cleanup_at IS NULL OR last_retention_cleanup_at < now() - interval '24 hours'`; when `--stage maintenance` is selected, dispatches `retention_cleanup` unconditionally.
    5. Runs the I/O worker pool until no **ready or running** jobs matching the requested stages remain — specifically counting only `status = 'running'` OR (`status = 'queued' AND run_after <= now()`) — or `--max-duration` expires. Backed-off retry jobs with `run_after > now()` are **not** awaited; they are counted and reported as `jobs_deferred` so cron invocations never hang holding `LOCK_RUN_ONCE`.
    6. Finalizes `pipeline_runs` (`status = 'completed' | 'partial' | 'failed'`, `duration_ms`, per-stage counts, `jobs_failed`, recording `jobs_deferred` in `notes` when `> 0`), prints summary (human-readable table or JSON including `jobs_deferred`), releases `LOCK_RUN_ONCE`, and exits (`0` on `completed`, `2` on `partial`, `1` on fatal error).

---

## 9. Ingestion & Lifecycle Stages

### 9.1 `poll_feed` (`smartrss/worker/stages/poll_feed.py`)
1. **Pre-check**: Read minimal poll inputs (`id`, `feed_url`, `status`, `health_state`, `etag`, `last_modified`, `poll_interval_minutes`). If `status IN ('paused', 'unsubscribed')` or `health_state == 'gone'`, mark job `done` (`stage_metrics.status = 'skipped'`).
2. **HTTP Fetch (outside DB write tx)**:
   - Call `SafeHttpClient.fetch(feed.feed_url, headers=conditional_headers, max_bytes=5_000_000)`.
   - All non-200 outcomes below open a short transaction, re-read the current synced health columns with `SELECT health_state, consecutive_errors, last_error, feed_url, status FROM feeds WHERE id = :id FOR UPDATE` *(needed for diff-gating: PostgreSQL 16 has no `OLD` in `RETURNING`; if `status` became `paused`/`unsubscribed` during I/O, only bookkeeping columns are written)*, then execute an explicit **column-scoped `UPDATE feeds SET ... WHERE id = :id`** touching **only poll-owned columns** (never persisting a stale pre-I/O ORM instance, so concurrent `PATCH` edits to `status`, `category_id`, `curation_instructions`, etc. can never be overwritten) and emit a `feed` `upsert` via `SyncLogWriter` **if and only if** a client-synced column (`health_state`, `consecutive_errors`, `last_error`, `feed_url`) changed:
     - `304 Not Modified`: reset `consecutive_errors = 0`, `health_state = 'healthy'`, `last_error = NULL`, update `last_fetched_at = now()`, `last_success_at = now()`, compute `next_fetch_at` (respecting `Cache-Control: max-age` floor + ±10% jitter), emit `feed` `upsert` if health/error changed (e.g., recovering from `degraded`/`broken`), commit.
     - `410 Gone`: set `health_state = 'gone'`, `last_error = 'HTTP 410 Gone'`, `next_fetch_at = NULL`, emit `feed` `upsert`, commit.
     - `429 Too Many Requests`: parse `Retry-After` (seconds or RFC 7231 HTTP-date, clamped `[60s, 24h]`), schedule `next_fetch_at = now() + retry_after + jitter`, increment `consecutive_errors`, update `health_state` (`'broken'` if `>= 5` else `'degraded'`), set `last_error`, emit `feed` `upsert`, commit.
     - Network / SSRF / unsupported encoding / 5xx / 4xx / XML parse error: increment `consecutive_errors`, set `health_state = 'broken'` if `>= 5` else `'degraded'`, `last_error`, exponential backoff `min(poll_interval * 2^(errors-1), 24h)` + ±10% jitter, emit `feed` `upsert`, commit.
   - Permanent redirect (`301`/`308` chain): check if another `feeds` row already owns `permanent_redirect_url`.
     - If **collision**: keep original `feed_url`, set `health_state = 'broken'`, `last_error = f"Permanent redirect collides with existing feed {other.id}: {new_url}"` (never crash on `UNIQUE`), emit `feed` `upsert`, commit.
     - If **no collision**: record `new_feed_url = permanent_redirect_url` to apply in step 4.
3. **Parse & Normalize (outside DB write tx)**:
   - Parse bytes off the event loop via `await asyncio.to_thread(...)` calling `feedparser.parse(io.BytesIO(body_bytes), response_headers={"content-location": final_url, "content-type": content_type})` — **never** passing raw `bytes`/`str` directly to `feedparser.parse()` (which treats path-like inputs as local files). Input is already capped at 5 MB by `SafeHttpClient`, relying on Python's bundled Expat parser's built-in billion-laughs / entity-expansion protection with external entity resolution disabled.
   - Multi-row item inserts execute in **500-row chunks** inside the same transaction so large full-archive feeds never exceed PostgreSQL's 65,535 bind-parameter limit.
   - Capture feed-level metadata (`feed_title`, `feed_site_url`).
   - For each entry:
     - Extract `raw_url` (reject/skip entries with non-HTTP(S) link or missing link+title), compute `canonical_url` via `domain/url_canon.py`.
     - Derive `guid`: trimmed entry `id` if non-empty; else `"fallback:" + sha256(f"{raw_url}\n{title.strip()}")` *(uses `raw_url` + `title`, NOT `canonical_url`, so future `url_canon` rule refinements never re-ingest id-less entries, and entries sharing a single page link with distinct titles do not collide)*.
     - Parse `published_at` (prefer `published_parsed`, fallback `updated_parsed`), clamped to `min(parsed_dt, fetched_at)`.
     - Extract `raw_summary` (prefer `content[0].value` if longer than `summary`, else `summary`).
     - Compute `entry_hash = sha256(normalize(title + "\n" + raw_summary))` and initial `content_hash = entry_hash`.
   - **Intra-batch deduplication**: Deduplicate parsed entries in memory by `guid` (keeping the first occurrence in feed order) before opening the DB transaction so duplicate entry IDs within a single feed response never violate `uq_items_feed_guid`.
4. **Persist Transaction (short DB tx + `SyncLogWriter` at end)**:
   - Re-read authoritative feed columns inside the transaction (`SELECT status, fetch_full_text, initial_backfill_done, poll_interval_minutes, health_state, consecutive_errors, last_error, title, site_url, feed_url, field_updated_at FROM feeds WHERE id = :id FOR UPDATE`). If `status IN ('paused', 'unsubscribed')` or `health_state == 'gone'` (changed concurrently during HTTP I/O), skip item writes and commit cleanly.
   - **Always run dedup lookup first** (for both initial/reactivation backfill and steady-state):
     - Look up existing items for this `feed_id` matching any incoming `guid`. Partition incoming batch into `existing_entries` and `new_entries`.
     - **Guarded unstable-GUID fallback** (`N-01`): an entry with no `guid` match is matched by `(feed_id, canonical_url)` **only if all of**: (a) exactly one existing item in the feed has that `canonical_url`; (b) exactly one entry in the current batch has that `canonical_url`; (c) the matched item's `guid` does **not** appear in the current batch (the old GUID genuinely disappeared). Otherwise the entry is treated as new. This prevents feeds whose entries share a single link (comics, podcasts, "latest" pages) from silently dropping new entries or overwriting an older item's `title`/`raw_summary`. When the fallback matches, the existing item keeps its original `guid`.
   - **Handle `existing_entries`**:
     - If `item.state == 'unread'` and `item.fetched_at >= now() - 7 days` and `entry.entry_hash != item.entry_hash`: update `title`, `raw_summary`, `entry_hash`, `content_hash = entry.entry_hash`, `updated_at = fetched_at`, set `extraction_status = 'pending'` if `feed.fetch_full_text` else `'fallback_rss'`, re-enqueue `extract_content` (if `feed.fetch_full_text`), and append `item` `upsert`. Never revert `read` or `expired` → `unread`.
   - **Handle `new_entries`**:
     - Determine initial active state from the **re-read** `feed.status`: `muted_state = ('read' if feed.status == 'muted' else 'unread')`, `muted_read_at = (fetched_at if feed.status == 'muted' else None)`.
     - Every new item insert initializes **`read_state_updated_at = fetched_at`** and **`starred_updated_at = fetched_at`** (and `expired_at = fetched_at` for backfilled-expired items).
     - Use **`INSERT INTO items (...) VALUES (...) ON CONFLICT (feed_id, guid) DO NOTHING RETURNING id`** as a defensive backstop; emit `item` `upsert`s and enqueue extraction jobs only for actually inserted rows.
     - **If `feed.initial_backfill_done == FALSE`** (`FR-FEED-05`):
       - Sort `new_entries` descending by `(published_at or fetched_at)` (preserving feed order on ties).
       - Top `N` (`app_settings.backfill_cap`, default `10`) new entries: insert with `state = muted_state`, `read_at = muted_read_at`, `extraction_status = ('pending' if feed.fetch_full_text else 'fallback_rss')`; enqueue `extract_content` at priority `90` if `feed.fetch_full_text`.
       - Remaining `new_entries` beyond `N`: insert with `state = 'expired'`, `expired_at = fetched_at`, `read_state_updated_at = fetched_at`, `starred_updated_at = fetched_at`, `extraction_status = 'fallback_rss'` (no `extract_content` job).
       - Set `initial_backfill_done = TRUE`.
     - **If `feed.initial_backfill_done == TRUE`** (steady-state):
       - Insert all `new_entries` with `state = muted_state`, `read_at = muted_read_at`, `extraction_status = ('pending' if feed.fetch_full_text else 'fallback_rss')`; enqueue `extract_content` at priority `50` if `feed.fetch_full_text`.
   - **Column-scoped feed metadata update**: Execute explicit `UPDATE feeds SET ... WHERE id = :id` touching **only poll-owned columns**: `etag`, `last_modified`, `last_fetched_at = fetched_at`, `last_success_at = fetched_at`, `next_fetch_at`, `consecutive_errors = 0`, `health_state = 'healthy'`, `last_error = NULL`, `initial_backfill_done = TRUE`, `updated_at = fetched_at`, plus:
     - `feed_url = new_feed_url` (only on non-colliding `301`/`308`),
     - `site_url = feed_site_url` (when non-empty),
     - `title = feed_title` **only if `feed_title` is non-empty AND `'title'` is NOT in `feed.field_updated_at`** (preserves user-renamed feed titles).
   - **Diff-gated sync emission**: Include a `feed` `upsert` in `events` **only if** a client-synced column changed (`feed_url`, `site_url`, `title`, `health_state`, `consecutive_errors`, `last_error`). If `events` is non-empty (`[*feed_upserts, *item_upserts]`), call `await sync_log.record(session, events)` and commit; otherwise commit without allocating a no-op `sync_changes` row.

### 9.2 `extract_content` (`smartrss/worker/stages/extract_content.py` + `smartrss/extract/`)
1. Load item + parent feed. If `not feed.fetch_full_text`, set `extraction_status = 'fallback_rss'`, emit `item` `upsert` via `SyncLogWriter`, commit, done.
2. Acquire `DomainRateLimiter` slot for `urlparse(item.raw_url).hostname`.
3. Fetch `item.raw_url` via `SafeHttpClient` (`max_bytes = 5_000_000`).
   - **Permanent / non-retryable failures** (HTTP 4xx such as 403/404/paywall, SSRF blocked, `UnsupportedEncodingError`, `ResponseTooLargeError`, or `Content-Type` not HTML/XHTML): mark `extraction_status = 'fallback_rss'`, emit `item` `upsert` via `SyncLogWriter`, commit, mark job `done` (do **not** retry).
   - **Deterministic extractor failures** (subprocess wall-clock timeout, `RLIMIT_CPU`/`RLIMIT_AS` kill, `stdout` cap breach, non-zero exit): treated as **permanent** — the same hostile document would fail identically — so mark `extraction_status = 'fallback_rss'`, emit `item` `upsert`, and complete the job without re-fetching.
   - **Transient failures** (HTTP 5xx, network/connect/read/total timeout): raise retryable error up to `max_attempts`. On the **final attempt (`attempts >= max_attempts`)**, transition `items.extraction_status = 'failed'` (preserving `raw_summary`), emit `item` `upsert` via `SyncLogWriter`, commit, and dead-letter the job so items never remain stuck in `pending` and downstream `1d`/`1e` stages proceed on `raw_summary`.
4. **Isolated Subprocess Extraction (`smartrss/extract/client.py` + `runner.py`)**:
   - Launch `asyncio.create_subprocess_exec(sys.executable, "-m", "smartrss.extract.runner", stdin=PIPE, stdout=PIPE, stderr=DEVNULL, start_new_session=True, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})` — **zero DB credentials or app secrets in child env**, dedicated POSIX process group (`start_new_session=True`), and `stderr=DEVNULL` so verbose `trafilatura`/`lxml` warnings can never fill the 64 KB OS pipe buffer and deadlock the child.
   - `runner.py` immediately sets POSIX limits (`resource.setrlimit(RLIMIT_CPU, (15, 15))`, `RLIMIT_AS` `(512 MiB, 512 MiB)`, `RLIMIT_FSIZE` `(0, 0)` — no disk writes), silences third-party stderr loggers, reads JSON `{url, html}` from `stdin`, calls `trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_precision=False)`, truncates extracted plain text to **500,000 chars**, and writes structured JSON `{ok, text, error}` to `stdout`.
   - Parent wraps `stdin` write + incremental bounded `stdout` read (**1 MB** hard cap) inside `async with asyncio.timeout(20.0):`. On timeout or `stdout` cap breach, terminates the entire child process group via `os.killpg(proc.pid, signal.SIGKILL)` and reaps the child (`await proc.wait()`).
5. **Persist (short DB tx)**:
   - If `ok` and `len(text.strip()) >= 100`: set `extracted_text = text`, `extraction_status = 'ok'`, recompute **`content_hash = sha256(normalize(title + "\n" + text))`** — **never modify `entry_hash`**.
   - Otherwise: set `extraction_status = 'fallback_rss'` (keeping `raw_summary` and `content_hash` intact).
   - Emit `item` `upsert` via `SyncLogWriter` and commit.

### 9.3 `expire_items` & `retention_cleanup`
- **`expire_items`**:
  - Runs hourly under scheduler leadership (and callable via `worker run-once --stage expire`).
  - Queries matching item IDs in batches of **500** using the exact formula from [data-model.md §6.1](../data-model.md#61-exact-unread-expiration-formula-expire_items):
    ```sql
    SELECT i.id
    FROM items i
    JOIN feeds f ON f.id = i.feed_id
    CROSS JOIN app_settings s
    WHERE s.id = 1
      AND i.state = 'unread'
      AND i.is_starred = FALSE
      AND COALESCE(f.expiration_days_override, s.default_expiration_days) > 0
      AND COALESCE(i.unexpired_at, i.fetched_at)
          < now() - make_interval(days => COALESCE(f.expiration_days_override, s.default_expiration_days))
    ORDER BY i.id
    LIMIT 500
    FOR UPDATE OF i SKIP LOCKED;
    ```
  - Updates matched rows: `state = 'expired'`, `expired_at = now()`, `read_state_updated_at = now()`, `updated_at = now()`, records `item` `upsert` in `sync_changes` via `SyncLogWriter`, and commits per batch.
- **`retention_cleanup`** (daily — triggered when `app_settings.last_retention_cleanup_at IS NULL OR < now() - interval '24 hours'` in both daemon mode and `run-once --stage all`, or unconditionally via `--stage maintenance`):
  - Deletes in FK-safe order:
    1. `jobs` where `status = 'done' AND finished_at < now() - interval '30 days'`.
    2. `sync_changes` where `changed_at < now() - interval '90 days'`.
    3. `stage_metrics` where `created_at < now() - interval '180 days'`.
    4. `pipeline_runs` where `started_at < now() - interval '180 days'` *(retained dead-letter `jobs` referencing pruned runs automatically have `run_id` set to `NULL` via `ON DELETE SET NULL`)*.
  - Updates `app_settings.last_retention_cleanup_at = now()` (internal operational column; does **not** emit an `app_settings` sync change).

---

## 10. Milestone `1a` API & CLI Surface

### 10.1 REST API (`/api/v1`)
All routes except `/health` and `/auth/pair` require `Authorization: Bearer <token>`.

| Method & Path | Purpose | Notes |
| :--- | :--- | :--- |
| `GET /api/v1/health` | Unauthenticated liveness/readiness | Exempt from `Host` allow-list for internal compose healthchecks; returns `{status: "ok", db: "ok", version}` (no secrets). |
| `POST /api/v1/auth/pair` | Exchange one-time pairing code for bearer token | Global sliding-window rate limit (5 / 5m); JSON body `{code, device_name, platform}`. |
| `POST /api/v1/auth/pairing-codes` | Create a new 5-minute pairing code | Authenticated; returns `{code, expires_at}`. |
| `GET /api/v1/devices` | List paired/revoked devices | Returns metadata only (never `token_hash`). |
| `POST /api/v1/devices/{id}/revoke` | Revoke a device's bearer token | Immediate `401` on next request from that device. |
| `GET /api/v1/categories` | List active categories | Ordered by `sort_order, name`. |
| `POST /api/v1/categories` | Create category | Emits `category` `upsert` in `sync_changes`. |
| `PATCH /api/v1/categories/{id}` | Rename / reorder category | Emits `category` `upsert`. |
| `DELETE /api/v1/categories/{id}` | Soft-delete category (`deleted_at`) | Sets `feeds.category_id = NULL` **and** stamps `feeds.field_updated_at['category_id'] = now()` on affected children; emits `category` `delete` + affected `feed` `upsert`s. |
| `GET /api/v1/feeds` | List feeds (optional `?status=&health_state=`) | Includes health & config fields. |
| `POST /api/v1/feeds` | Subscribe to a feed URL | Validates URL via `SSRFPolicy` before saving; initializes `title` from optional input (stamping `field_updated_at['title']` when the user supplied one, so the poller never overwrites it) or else from the URL hostname/path (unstamped, so the poller fills in the feed's own title), and `poll_interval_minutes` from `app_settings`; if `feed_url` exists as `unsubscribed`, reactivates (`status='active'`, stamps `field_updated_at['status']`, clears `etag`/`last_modified`, resets `health_state='healthy'`, `consecutive_errors=0`, `last_error=NULL`, `next_fetch_at=now()`, `initial_backfill_done=FALSE`); enqueues immediate `poll_feed` (priority `10`, `bump_existing=True`). |
| `GET /api/v1/feeds/health` | Feed Health dashboard summary | Returns feeds sorted by severity (`gone`, `broken`, `degraded`, `healthy`) with error counts & staleness. |
| `POST /api/v1/feeds/import-opml` | Import OPML 2.0 subscriptions | JSON body `{opml_xml}` parsed with **`defusedxml`** (iterative traversal capped at depth 32); validates URL syntax and literal IPs without blocking serial DNS (full DNS/IP pinning runs at fetch time in `SafeHttpClient`), strips non-HTTP(S) `htmlUrl` (`site_url`), creates missing categories, subscribes/reactivates feeds (OPML `title`/`text` attributes are treated as user-chosen names and stamp `field_updated_at['title']`; clearing `etag`/`last_modified` on reactivation, all starting with `initial_backfill_done=FALSE`, reading default poll interval from `app_settings`), enqueues `poll_feed` jobs; returns `{imported, reactivated, skipped_duplicates, rejected_invalid}`. |
| `GET /api/v1/feeds/export-opml` | Export active/paused/muted subscriptions | Returns `application/xml` OPML 2.0 grouped by category. |
| `GET /api/v1/feeds/{id}` | Get single feed | |
| `PATCH /api/v1/feeds/{id}` | Update feed config (`feed_url`, `title`, `status`, `category_id`, `poll_interval_minutes`, `expiration_days_override`, `fetch_full_text`, `research_policy`, `curation_instructions`) | Updates per-field `field_updated_at` for structured fields (including `'title'`, which prevents `poll_feed` from overwriting user-renamed titles); enforces optimistic `expected_config_version` (`409 Conflict`) and increments `config_version` **only** when `curation_instructions` changes. On `feed_url` edit: validates via `SSRFPolicy` (`422`), checks uniqueness (`409 Conflict` on collision), clears `etag`/`last_modified`, resets `health_state='healthy'`, `consecutive_errors=0`, `last_error=NULL`, `next_fetch_at=now()`, `initial_backfill_done=FALSE` (backfill cap protects inbox while deduplicating existing items), and enqueues immediate `poll_feed` (priority `10`, `bump_existing=True`). Emits `feed` `upsert`. |
| `POST /api/v1/feeds/{id}/poll` | Trigger immediate poll | Enqueues `poll_feed` at priority `10` with `bump_existing=True` (bumping any already-queued backed-off job to `priority=10, run_after=now()`). |
| `GET /api/v1/items` | Paginated item inspection (`1a` verification) | Filters: `feed_id`, `state`, `is_starred`, `limit` (max 100), `offset`. |
| `GET /api/v1/items/{id}` | Single item detail | |
| `GET /api/v1/system/health` | System health summary | DB status, queue counts by status/type, dead-letter count, last 10 `pipeline_runs`, `instance_epoch`. |
| `GET /api/v1/system/metrics?window=24h\|7d\|30d` | Pipeline timing metrics | Exact SQL `percentile_cont(0.5/0.95)`, `avg`, `max`, failure rate, and `not_modified_count` (`outcome = 'not_modified'`) per `job_type`, plus per-feed cost rollup. |
| `GET /api/v1/system/jobs/dead-letter` | List `failed` jobs | Paginated with `last_error`, `attempts`, `payload`. |
| `POST /api/v1/system/jobs/{id}/retry` | Requeue a dead-letter job | Resets `status='queued'`, `attempts=0`, `run_after=now()`. Catches `uq_jobs_active_dedupe` conflict if an active job with the same `(job_type, dedupe_key)` already exists and returns `409 Conflict` (with `active_job_id`). |
| `POST /api/v1/system/jobs/{id}/dismiss` | Delete a dead-letter job | Removes acknowledged failure from dead-letter queue. |

### 10.2 CLI Entrypoint (`smartrss`)
- `smartrss pair create --name <name> [--platform ubuntu|android|web]`
- `smartrss devices list` / `smartrss devices revoke <device_id>`
- `smartrss worker run` (continuous daemon; refuses to start if `WORKER_MODE=cron`)
- `smartrss worker run-once [--stage all|poll|extract|expire|maintenance] [--feed <id>] [--force-poll] [--max-duration <sec, default 1800>] [--json]` *(reports `feeds_polled`, `feeds_not_modified`, `items_ingested`, backwards-compatible `items_polled`, `items_extracted`, `jobs_failed`, `jobs_deferred`)*
- `smartrss settings get` / `smartrss settings set <key> <value>` (`backfill_cap`, `default_expiration_days`, `default_poll_interval_minutes`; validated, stamps `updated_at`, emits `app_settings` `upsert` via `SyncLogWriter` using `APP_SETTINGS_ENTITY_ID`; the only `1a` way to change `FR-FEED-05`'s configurable cap until the settings UI/API in `1b`/`1c`)
- `smartrss db migrate` (runs `alembic upgrade head`)
- `smartrss db recanonicalize [--batch-size 500]` *(recomputes `items.canonical_url` using current `url_canon` rules and emits `item` `upsert`s for changed rows, `E-12`)*
- `smartrss db rotate-epoch (--database-url <url> | --database-url-env <ENV_VAR>)` *(explicit target URL required via flag or env var — `--database-url-env` avoids exposing passwords in process argv — so drills can never accidentally rotate production's epoch; generates new `sync_state.instance_epoch` UUID)*

---

## 11. Backups, Restore Runbook & Restore Drill (`NFR-OPS-01`)

### 11.1 Automated Nightly Backup (`scripts/backup.sh`)
1. Sets **`umask 077`** so dump files are created mode `0600` (owner-readable only, protecting device token hashes and reading history).
2. Runs `docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc` writing atomically via `.tmp` → `${BACKUP_DIR}/smartrss-${TIMESTAMP}.dump`.
3. Verifies non-empty archive with `pg_restore --list`.
4. Prunes local dumps older than **14 days** (`find "$BACKUP_DIR" -name 'smartrss-*.dump' -mtime +14 -delete`).
5. If `BACKUP_RSYNC_TARGET` is set, copies dumps with `rsync -az` (**never `--delete`**, so an emptied/wrong local `BACKUP_DIR` can never wipe the off-host copy), skipping the copy entirely and exiting non-zero if no valid local dump exists. Remote retention is pruned independently with its own longer window (`BACKUP_REMOTE_RETENTION_DAYS`, default `30`) via `ssh <host> find <path> -name 'smartrss-*.dump' -mtime +30 -delete`, and only after the new dump has been copied successfully.
6. Ships an example host timer (`deploy/systemd/smartrss-backup.service` + `deploy/systemd/smartrss-backup.timer`, plus a cron one-liner in `README.md`) to invoke `make backup` nightly.

### 11.2 Production Restore Runbook (Documented in `README.md`)
1. **Stop writers & API**: `docker compose stop api worker` (prevents any client from syncing against a partially restored DB or observing the pre-rotation `instance_epoch`).
2. **Restore dump into a fresh database** (so objects created after the backup, e.g. by a later migration, cannot linger): `docker compose exec -T db dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"`, `docker compose exec -T db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"`, then `docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner < smartrss-<timestamp>.dump`.
3. **Rotate `instance_epoch` BEFORE serving traffic**: `docker compose run --rm api smartrss db rotate-epoch --database-url "$DATABASE_URL"` (forces all paired clients to detect epoch mismatch and cleanly re-bootstrap while preserving pending offline outbox mutations).
4. **Review & re-apply post-backup device revocations**: Because `devices` is restored to backup-time state, any device revoked *after* the backup becomes valid again. Run `docker compose run --rm api smartrss devices list` and immediately revoke any device revoked since the backup (`smartrss devices revoke <device_id>`). *(Manual checklist accepted by owner — see §13.)*
5. **Migrate to current schema**: `docker compose run --rm api smartrss db migrate` (brings an older-schema dump to Alembic head **before** any app process serves traffic).
6. **Restart & verify**: `docker compose start api worker` (omit `worker` in cron mode), then verify `GET /api/v1/health`.

### 11.3 Automated Restore Drill (`scripts/restore-drill.sh` / `make restore-drill`)
1. Takes a fresh dump (or uses latest in `BACKUP_DIR`).
2. Creates a clean temporary database `smartrss_restore_drill` inside `db` (or `db-test`), runs `pg_restore --no-owner --clean --if-exists`.
3. Captures `old_epoch` from `sync_state`, then invokes `smartrss db rotate-epoch --database-url-env SMARTRSS_DRILL_DATABASE_URL` explicitly targeting `smartrss_restore_drill`.
   - The drill URL contains the DB password, so it is only exported into the script's environment and forwarded to the container with `docker compose run -e SMARTRSS_DRILL_DATABASE_URL` (name only, no `=value`). It never appears in any process argv.
   - It then asserts `new_epoch != old_epoch`, verifies that production `sync_state.instance_epoch` is untouched, and verifies that the Alembic head matches.
   - It verifies the `feeds`/`items`/`devices` row counts. When the drill takes a fresh dump, the counts must match production exactly. With an older dump, the drill fails if production is non-empty but the restore is empty.
4. Drops the temporary drill database and exits `0`.
5. *(Note: The architecture's full end-to-end drill check—"confirms paired clients cleanly resync without losing pending outbox mutations"—is deferred to milestones **`1b`/`1c`** once the `/api/v1/sync/*` endpoints and client outbox exist.)*

---

## 12. Verification & Test Plan (`1a` Exit Criteria)

Per [development-process.md §2](../development-process.md#2-testing-standards), target **>= 80%** overall backend coverage and **>= 90%** line/branch coverage on critical core modules: `db/sync_log.py`, `security/safe_http.py`, `security/auth.py`, `security/middleware.py`, `domain/url_canon.py`, `domain/backoff.py`, `domain/expiration.py`, `extract/client.py`, `jobs/queue.py`, `jobs/locks.py`, `worker/stages/poll_feed.py`, and `worker/stages/expire_items.py`. This exact list is enforced by `CRITICAL_MODULES` in [`scripts/check-critical-coverage.py`](../../scripts/check-critical-coverage.py), invoked by `make test-cov` after `pytest` writes `coverage.json` (see [development-process.md §2.2](../development-process.md#22-coverage-expectations)); keep the two in sync.

### 12.1 Automated Test Matrix
| Test Suite | Key Cases Verified |
| :--- | :--- |
| **Unit (`tests/unit/`)** | URL canonicalization (tracking params, fragments, case, default ports); backoff & `Retry-After` (delta-seconds, HTTP-date, `Cache-Control: max-age`, jitter bounds); `effective_ttl_days` (global default, feed override, `0` = never, starred immunity, `unexpired_at` reset, clamped `published_at`); SSRF IP classifier (RFC1918, loopback, link-local, CGNAT, IPv6 ULA, IPv4-mapped `::ffff:127.0.0.1`, NAT64 `64:ff9b::127.0.0.1` / `64:ff9b::192.168.1.1` / `64:ff9b:1::1`, 6to4 `2002:c0a8:0101::1`, Docker CIDR, non-HTTP schemes); OPML 2.0 round-trip & malformed/XXE XML (`defusedxml`); pairing code format/normalization. |
| **Boundary (`tests/boundary/`)** | `SafeHttpClient`: injectable DNS returning public vs. private IPs, dual-homed DNS (1 public + 1 private → blocked), IP pinning verified (TCP connect receives validated IP while TLS SNI / `Host` keeps hostname), multi-hop redirects landing on `192.168.1.1` / `169.254.169.254` → blocked, >5 redirects → blocked, `asyncio.timeout` total-budget slow-drip abort, 5 MB wire cap, unsupported `Content-Encoding: br` → rejected, raw vs. zlib-wrapped `deflate` and multi-member gzip decoded under the same cap, and **single ~64 KB highly-compressible gzip bomb chunk** → aborted at `max_bytes` via streaming `zlib.decompressobj(max_length=...)`; Isolated extractor subprocess: clean HTML extraction, hostile/deeply-nested HTML bomb hitting CPU/RAM/timeout cap without crashing parent worker (and resolving to `fallback_rss` without retry), **child writing 1 MB to `stderr`** completing cleanly without deadlock, **process-group kill (`start_new_session=True` + `os.killpg`)** terminating a spawned grandchild on timeout, and confirmation that `DATABASE_URL` is absent from child env. |
| **Integration — Real PostgreSQL (`tests/integration/`)** | Alembic `upgrade head` → `downgrade base` → `upgrade head` cleanly on empty Postgres 16; **`SyncLogWriter` concurrent writers** proving `sync_changes.seq` is strictly monotonic and gap-free in commit order, plus `before_commit` ORM guard **and** `before_cursor_execute` Core-SQL guard catching unlogged synced writes; Auth & security middleware (missing/invalid/revoked bearer → `401`, expired/reused pairing code fails, global pairing rate-limit → `429`, disallowed `Host` → `400`, `GET /api/v1/health` Host exemption, disallowed CORS blocked, non-JSON `POST` → `415`); Job queue (`SKIP LOCKED` concurrency, `dedupe_key` uniqueness, manual poll `bump_existing` waking a backed-off queued job, lease heartbeat renewal, **fenced completion/heartbeat rejecting stale worker after lease reclaim**, stale-lease reclaim after simulated crash, dead-letter transition, dead-letter retry `409` on active-job collision, advisory-lock pool safety across dedicated connection & xact locks); Poller (`ETag`/`304` no-op emits 0 `feed` upserts while `degraded → healthy` emits `feed` upsert, `301` normal migration + `301` URL collision → `broken`, `410` → `gone`, **`PATCH /feeds/{id}` `feed_url` edit recovering `gone` → `healthy`** + `409` collision, `429` + `Retry-After`, unexpected exception advancing `next_fetch_at` and degrading health without infinite re-enqueue, backfill cap `N=10` marking older items `expired` with `read_state_updated_at = fetched_at`, **unsubscribe → re-subscribe reactivation** clearing `etag`/`last_modified` + deduping existing items without reverting `read`/`expired` → `unread`, **duplicate GUID & same-link entries within one fetch**, **same-link feed across two polls** (new entry sharing an existing item's link is inserted as new and the older item is not overwritten; genuine single-item GUID change still matches via guarded canonical-URL fallback), muted feed auto-marking `read`, unstable GUID canonical-URL fallback, **`entry_hash` vs. `content_hash`** proving extracted unread item is NOT re-extracted on next unchanged poll while feed-text edit DOES re-extract without reverting `read`, concurrent `PATCH` during poll preserved by column-scoped update, user-renamed `title` preserved by poller, `DELETE /categories/{id}` stamping `feeds.field_updated_at['category_id']`, OPML import/export); Extraction (exhausted transient retries transitioning `extraction_status = 'failed'` + emitting `item` upsert); Expiration sweeper (batch expiry, starred immunity, `0` override, `unexpired_at` grace reset, `sync_changes` emission); Retention cleanup (deleting 180d `pipeline_runs` with retained dead-letter `jobs` cleanly setting `run_id = NULL`); `smartrss settings set` emitting `app_settings` `upsert`; user-supplied subscribe/OPML titles preserved by poller; `worker run-once` (overlapping invocation exits `skipped_locked`, reaper runs first, `retention_cleanup` runs when due via DB timestamp, **exits cleanly reporting `jobs_deferred` when backed-off jobs exist**) & `stage_metrics`/`pipeline_runs` SQL `p50`/`p95` accuracy. |

### 12.2 Manual / Real-World Exit Checks
1. `make check` passes cleanly with zero warnings and meets coverage thresholds.
2. Import ~25 real RSS/Atom feeds via OPML, run both daemon mode and `smartrss worker run-once --json`, verify backfill cap (`N=10`), clean extraction + fallbacks, and populated `/api/v1/system/metrics`.
3. Run `make backup` with `BACKUP_RSYNC_TARGET` set and an empty local `BACKUP_DIR` simulated, confirming the remote copy is untouched.
4. Run `make backup && make restore-drill` and confirm `instance_epoch` rotation succeeds cleanly against the drill DB without touching production.

---

## 13. Accepted Residual Risks & Deferred Hardening (For Owner Sign-Off)

The following items are explicit security/operational trade-offs in `1a`, documented rather than silently assumed. **All four were accepted by the project owner on 2026-11-24** as part of approving `1a`:

1. **Extractor subprocess shares `worker` network namespace in `1a` (`A-14`, T-05)**:
   - *Risk*: While `smartrss.extract.runner` runs in a fresh `spawn` subprocess with zero environment secrets and strict `RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_FSIZE`/wall-clock limits, it inherits the `worker` container's network namespace. A memory-corruption bug in `lxml`/HTML parsing could probe `db:5432`, LAN hosts, or the host LLM port.
   - *Decision (accepted)*: **Accept for `1a`** (`trafilatura`/`lxml` on HTML only); **network isolation is a hard gate for the `1f` design** before adding `pymupdf` (a C library parsing untrusted binary PDFs)—either via a dedicated `extractor` Compose service with `network_mode: none` (already permitted by ADR 0006 §4) or `unshare --net` / seccomp in the child.
2. **Post-restore device revocation replay (`A-15`)**:
   - *Risk*: Restoring a `pg_dump` reverts the `devices` table to backup time, reactivating any device revoked *after* the backup was taken.
   - *Decision (accepted)*: **Manual checklist in the restore runbook (§11.2 step 4)**—run `smartrss devices list` and re-revoke before starting `api` (single-user homelab with a handful of devices). An out-of-DB revocation log was considered and rejected as not worth the added code at this scale.
3. **Client-resync drill step deferred (`A-15`)**:
   - *Decision (accepted)*: The architecture's "paired clients cleanly resync without losing pending outbox mutations" drill check moves to **`1b`/`1c`**, when sync endpoints and the client outbox exist (§11.3 step 5).
4. **SSRF hairpin NAT / host own global IP (`A-33`)**:
   - *Risk*: `SafeHttpClient` blocks all RFC1918, loopback, link-local, CGNAT, ULA, NAT64, 6to4, and Docker bridge CIDRs, but if a hostile feed's DNS resolves to the user's own WAN IPv4 (reaching router port-forwards via hairpin NAT) or the host's own global IPv6 address, it is not in a private range.
   - *Decision (accepted)*: **Accept as residual risk** given SmartRSS's non-port-forwarded home-LAN architecture. The root `README.md` documents adding the home WAN IPv4 and the host's global IPv6 prefix to `SSRF_BLOCKED_CIDRS` for users with router port-forwards. A dynamic self-IP probe (`SSRF_BLOCKED_HOSTS_SELF`) was not adopted.
