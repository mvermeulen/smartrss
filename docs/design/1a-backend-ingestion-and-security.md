# Component Design 1a: Backend Foundation, Security, Polling & Extraction

> **Status:** Draft *(awaiting project owner review & approval)*
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
| **T-01** *(S-05)* | **Muted feed item state** *(Confirm)* | **Auto-mark `read` at ingest time** (`state='read'`, `read_at=fetched_at`, `read_state_updated_at=fetched_at`) while still archiving & extracting. | Pure client-side filter leaving items `unread` in DB. | Keeps server and multi-device unread counts 100% consistent without duplicating mute filters in every client query; unmuting later does not flood the inbox with weeks of old items; integrates cleanly with `expire_items`. *(Requires a 1-line alignment in [data-model.md](../data-model.md#31-item-state-machine-itemsstate--independent-itemsis_starred) §3.1–§3.2 upon approval.)* |
| **T-02** | **Alembic schema phasing** *(Confirm)* | **Milestone-scoped tables + early `sync_state`/`sync_changes`**: create `1a` tables + `sync_state`/`sync_changes` in `1a`; add `1b`/`1d`–`1g` tables in their own milestones. | (a) Create all 15+ tables upfront in `1a`; or (b) defer `sync_state`/`sync_changes` to `1b`. | Including `sync_state`/`sync_changes` in `1a` lets `1a`'s restore drill rotate `instance_epoch` and prevents retrofitting every `1a` write path in `1b`, while avoiding untested dead schema for `1d`–`1g`. |
| **T-03** | **Reverse proxy (`web`)** | **Nginx (`nginx:1.27-alpine`)** with explicit ~35-line config. | Caddy. | Plain-HTTP LAN entry point does not use auto-TLS (ADR 0002/0005/0006); Nginx avoids Caddy's auto-HTTPS friction, is tiny (~12 MB), and makes `Host` forwarding, CSP headers, and body caps completely explicit. Swapping to Caddy later if LAN TLS is ever enabled is a single-file change. |
| **T-04** | **SSRF IP pinning with HTTPS/SNI** | **Custom `httpcore.AsyncNetworkBackend`** wrapping `AnyIOBackend` inside `httpx.AsyncHTTPTransport`, intercepting `connect_tcp()`. | (a) Pre-flight DNS check before `httpx.get()` (vulnerable to DNS rebinding TOCTOU); (b) rewriting URL host to IP (breaks SNI/cert verification & IPv6). | Pinning at `connect_tcp()` validates all resolved A/AAAA IPs right before connecting and opens the socket to the validated IP while `httpcore` preserves the original hostname for HTTP `Host`, TLS SNI, and certificate validation. |
| **T-05** | **HTML parser isolation** | **Fresh per-job `spawn` subprocess** (`python -m smartrss.extract.runner`) with stripped env (no DB secrets), `RLIMIT_CPU=15s`, `RLIMIT_AS=512MB`, wall-clock timeout `20s`. | (a) `fork()` from async worker (deadlock & secret-leak risk); (b) long-lived worker pool (risk of leaked C-library state across hostile docs). | At ~200 items/day (~8/hr), ~100 ms subprocess startup is negligible and guarantees zero shared heap, zero credential exposure, and clean crash recovery when extended to `pymupdf` in `1f`. `512 MB` `RLIMIT_AS` avoids false Linux virtual-memory OOMs in Python/`lxml`. |
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
├── .env.example                           # Documented infrastructure environment variables
├── docs/                                  # Existing specs, ADRs, component designs
├── deploy/
│   └── nginx/
│       └── nginx.conf                     # Single LAN entry point (/api/v1/* proxy + security headers)
├── scripts/
│   ├── backup.sh                          # Nightly pg_dump -Fc + 14-day rotation + optional rsync
│   └── restore-drill.sh                   # Automated restore + instance_epoch rotation verification
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
    │   │   └── sync_log.py                # SyncLogWriter (sync_state FOR UPDATE + sync_changes)
    │   ├── security/
    │   │   ├── auth.py                    # Token/code hashing, pairing, bearer verification
    │   │   ├── middleware.py              # Host allow-list, JSON-only mutation guard
    │   │   └── safe_http.py               # SafeHttpClient, SafeNetworkBackend, DNS/IP validation, rate limiter
    │   ├── domain/
    │   │   ├── url_canon.py               # Pure URL canonicalization & tracking-param stripping
    │   │   ├── backoff.py                 # Pure HTTP 429/Cache-Control/error backoff + jitter math
    │   │   ├── expiration.py              # Pure effective_ttl_days calculation
    │   │   └── opml.py                    # Pure OPML 2.0 parser & serializer
    │   ├── extract/
    │   │   ├── client.py                  # Parent-side isolated subprocess launcher + timeout/output caps
    │   │   └── runner.py                  # Subprocess entrypoint (rlimits + trafilatura extraction)
    │   ├── jobs/
    │   │   ├── queue.py                   # Enqueue, SKIP LOCKED claim, heartbeat, complete/fail, reaper
    │   │   ├── locks.py                   # PostgreSQL advisory-lock helpers
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
        ├── conftest.py                    # Real-Postgres fixtures, schema isolation, factories
        ├── fixtures/                      # Sanitized RSS/Atom, OPML, hostile HTML/XSS/redirect fixtures
        ├── unit/                          # Pure domain, SSRF IP ranges, backoff, URL canon, OPML, auth
        ├── integration/                   # Real PostgreSQL: migrations, auth/Host/CORS, queue/leases, poller, expiration, metrics, run-once
        └── boundary/                      # SafeHttpClient IP-pinning/redirects/byte-caps, isolated subprocess crash/CPU/RAM limits
```

### Standard `Makefile` Targets
- `make check`: Runs `lint` (`ruff check`, `ruff format --check`) + `typecheck` (`mypy`) + `test-cov` (unit + boundary + real-Postgres integration tests with coverage gates).
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
| **`worker`** | `./backend` (`smartrss worker run`) | None | Depends on `db` healthy; non-root UID `1000`; `mem_limit: 2g`, `pids_limit: 128` (bounds subprocess forks); `extra_hosts: ["host.docker.internal:host-gateway"]` (prepares for `1d`). |
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
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173,http://localhost:8080` | Strict CORS allow-list for Phase 1 `localhost` client origin + LAN fallback. |
| `SSRF_BLOCKED_CIDRS` | *(auto-includes Docker bridge subnet)* | Additional CIDRs to block beyond standard private/loopback/link-local/CGNAT/ULA ranges. |
| `WORKER_IO_CONCURRENCY` | `4` | Concurrent I/O jobs (`poll_feed`, `extract_content`) per worker process. |
| `WORKER_POLL_INTERVAL_SECONDS` | `30` | Scheduler tick interval in daemon mode. |
| `DEFAULT_POLL_INTERVAL_MINUTES` | `60` | Default per-feed poll cadence. |
| `DEFAULT_BACKFILL_CAP` | `10` | Initial `unread` cap on newly subscribed feeds / OPML import. |
| `DEFAULT_EXPIRATION_DAYS` | `30` | Default unread TTL (`0` = never expire). |
| `BACKUP_DIR` | `./backups` | Host directory for nightly `pg_dump` files. |
| `BACKUP_RSYNC_TARGET` | *(empty / optional)* | Optional `user@lan-host:/path` for off-host LAN copy. |

---

## 5. Database Schema & Migration (`0001_milestone_1a_core.py`)

All timestamps are `TIMESTAMPTZ` stored in UTC. All UUID primary keys use `UUID` (`uuid4` generated in application code or `gen_random_uuid()` default).

### 5.1 Tables Created in `1a`

1. **`devices`** & **`pairing_codes`** (per [data-model.md §4.1](../data-model.md#41-devices--pairing_codes))
   - `devices`: `id (UUID PK)`, `name (TEXT NOT NULL)`, `platform (TEXT NOT NULL CHECK (platform IN ('ubuntu','android','web','cli')))`, `token_hash (TEXT UNIQUE NOT NULL)`, `created_at (TIMESTAMPTZ NOT NULL DEFAULT now())`, `last_seen_at (TIMESTAMPTZ NULL)`, `revoked_at (TIMESTAMPTZ NULL)`.
   - `pairing_codes`: `id (UUID PK)`, `code_hash (TEXT UNIQUE NOT NULL)`, `device_name (TEXT NOT NULL)`, `platform (TEXT NOT NULL DEFAULT 'ubuntu')`, `expires_at (TIMESTAMPTZ NOT NULL)`, `consumed_at (TIMESTAMPTZ NULL)`, `created_by_device_id (UUID NULL REFERENCES devices(id) ON DELETE SET NULL)`, `created_at (TIMESTAMPTZ NOT NULL DEFAULT now())`.

2. **`sync_state`** & **`sync_changes`** (per [data-model.md §4.2](../data-model.md#42-sync_state--sync_changes), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md))
   - `sync_state`: `id (SMALLINT PK CHECK (id = 1))`, `current_seq (BIGINT NOT NULL DEFAULT 0)`, `instance_epoch (UUID NOT NULL DEFAULT gen_random_uuid())`, `updated_at (TIMESTAMPTZ NOT NULL DEFAULT now())`. Seeded with 1 row in migration `0001`.
   - `sync_changes`: `seq (BIGINT PK)`, `entity_type (TEXT NOT NULL)`, `entity_id (UUID NOT NULL)`, `op (TEXT NOT NULL CHECK (op IN ('upsert','delete')))`, `changed_at (TIMESTAMPTZ NOT NULL DEFAULT now())`.
   - Index: `ix_sync_changes_entity (entity_type, entity_id)`, `ix_sync_changes_changed_at (changed_at)` (for 90-day pruning).
   - **`SyncLogWriter` invariant**: Every service method that mutates a synced entity (`category`, `feed`, `item`, `app_settings`) calls `await sync_log.record(session, events)` **as the final step right before `session.commit()`**, after all slow I/O (HTTP fetches, subprocess extraction) has already completed. `record()` executes `SELECT current_seq FROM sync_state WHERE id = 1 FOR UPDATE`, increments `current_seq` by `len(events)`, updates `sync_state`, and bulk-inserts the `sync_changes` rows.

3. **`categories`** (per [data-model.md §4.3](../data-model.md#43-categories))
   - `id (UUID PK)`, `name (TEXT NOT NULL)`, `sort_order (INT NOT NULL DEFAULT 0)`, `created_at`, `updated_at`, `deleted_at (TIMESTAMPTZ NULL)`.
   - Partial unique index: `uq_categories_active_name ON categories (LOWER(name)) WHERE deleted_at IS NULL`.

4. **`feeds`** (per [data-model.md §4.4](../data-model.md#44-feeds))
   - All columns from `data-model.md` §4.4: `id`, `category_id (FK → categories.id ON DELETE SET NULL)`, `feed_url (TEXT UNIQUE NOT NULL)`, `site_url`, `title`, `status (CHECK IN ('active','paused','muted','unsubscribed'))`, `health_state (CHECK IN ('healthy','degraded','broken','gone'))`, `poll_interval_minutes (INT NOT NULL DEFAULT 60 CHECK (poll_interval_minutes >= 5))`, `etag`, `last_modified`, `last_fetched_at`, `last_success_at`, `next_fetch_at`, `consecutive_errors (INT NOT NULL DEFAULT 0)`, `last_error`, `initial_backfill_done (BOOLEAN NOT NULL DEFAULT FALSE)`, `expiration_days_override (INT NULL CHECK (expiration_days_override >= 0))`, `fetch_full_text (BOOLEAN NOT NULL DEFAULT TRUE)`, `research_policy (JSONB NOT NULL DEFAULT '{"enabled":false,"fetch_pdfs":false,"guidance":"","max_hops":1}')`, `curation_instructions (TEXT NOT NULL DEFAULT '')`, `field_updated_at (JSONB NOT NULL DEFAULT '{}')`, `config_version (INT NOT NULL DEFAULT 1)`, `created_at`, `updated_at`.
   - Index: `ix_feeds_due_poll ON feeds (next_fetch_at) WHERE status IN ('active', 'muted') AND health_state <> 'gone'`.

5. **`items`** (per [data-model.md §4.5](../data-model.md#45-items))
   - Columns for `1a`: `id`, `feed_id (FK → feeds.id NOT NULL)`, `guid (TEXT NOT NULL)`, `raw_url (TEXT NOT NULL)`, `canonical_url (TEXT NOT NULL)`, `title (TEXT NOT NULL)`, `author`, `published_at (TIMESTAMPTZ NULL)`, `fetched_at (TIMESTAMPTZ NOT NULL)`, `raw_summary (TEXT NULL)`, `extracted_text (TEXT NULL)`, `extraction_status (TEXT NOT NULL DEFAULT 'pending' CHECK IN ('pending','ok','fallback_rss','failed'))`, `content_hash (TEXT NULL)`, `state (TEXT NOT NULL DEFAULT 'unread' CHECK IN ('unread','read','expired'))`, `is_starred (BOOLEAN NOT NULL DEFAULT FALSE)`, `read_at`, `starred_at`, `expired_at`, `read_state_updated_at (TIMESTAMPTZ NOT NULL)`, `starred_updated_at (TIMESTAMPTZ NOT NULL)`, `unexpired_at (TIMESTAMPTZ NULL)`, `created_at`, `updated_at`.
   - *(Note: `search_vector` is added in `1b` alongside FTS; `cluster_id` FK is added in `1e` alongside `clusters`.)*
   - Constraints & indexes:
     - `uq_items_feed_guid UNIQUE (feed_id, guid)`
     - `ix_items_feed_canonical_url (feed_id, canonical_url)` *(supports unstable-GUID fallback)*
     - `ix_items_canonical_url (canonical_url)` *(prepares for `1e` cross-feed exact dedup)*
     - `ix_items_expiration ON items (COALESCE(unexpired_at, fetched_at)) WHERE state = 'unread' AND is_starred = FALSE` *(fast expiration sweep)*
     - `ix_items_feed_fetched (feed_id, fetched_at DESC)`

6. **`jobs`**, **`pipeline_runs`**, **`stage_metrics`** (per [data-model.md §4.9](../data-model.md#49-prompt_configs-interest_profiles-sync_mutations-jobs-pipeline_runs-stage_metrics-app_settings))
   - `pipeline_runs`: exact schema from `data-model.md` §4.9 (`id`, `trigger`, `stages`, `status`, `started_at`, `finished_at`, `duration_ms`, counters, `total_llm_ms`, `notes`).
   - `jobs`: exact schema from `data-model.md` §4.9, with:
     - `uq_jobs_active_dedupe UNIQUE (job_type, dedupe_key) WHERE status IN ('queued', 'running')`
     - `ix_jobs_claim ON jobs (priority ASC, run_after ASC, id ASC) WHERE status = 'queued'`
     - `ix_jobs_running_lease ON jobs (lease_expires_at) WHERE status = 'running'`
     - `ix_jobs_done_finished ON jobs (finished_at) WHERE status = 'done'` *(30-day retention pruning)*
   - `stage_metrics`: exact schema from `data-model.md` §4.9, indexed on `(job_type, created_at)` and `(feed_id, created_at)`.

7. **`app_settings`**
   - Single-row configuration table (`id SMALLINT PK CHECK (id = 1)`), `default_expiration_days (INT NOT NULL DEFAULT 30)`, `backfill_cap (INT NOT NULL DEFAULT 10)`, `default_poll_interval_minutes (INT NOT NULL DEFAULT 60)`, `updated_at (TIMESTAMPTZ NOT NULL DEFAULT now())`. Seeded in migration `0001`. *(LLM/clustering/research settings columns added in `1d`–`1f`.)*

---

## 6. Security & Authentication Middleware (`NFR-SEC-01`)

### 6.1 Middleware Execution Pipeline (Order Matters)

```mermaid
flowchart LR
    Req["Incoming HTTP Request"] --> Host["1. HostAllowListMiddleware\n(blocks DNS rebinding)"]
    Host --> CORS["2. CORSMiddleware\n(strict origin allow-list)"]
    CORS --> JSON["3. JsonMutationGuardMiddleware\n(POST/PUT/PATCH/DELETE must be application/json)"]
    JSON --> Route["4. FastAPI Router"]
    Route --> Public["Public: /api/v1/health, /api/v1/auth/pair\n(pair is rate-limited)"]
    Route --> Auth["Protected: BearerAuthDep\n(SHA-256 token lookup, revoked check)"]
```

1. **`HostAllowListMiddleware`**:
   - Extracts `Host` header (lowercased, trimmed). Rejects missing or non-matching `Host` with `400 Bad Request` (`{"detail": "Invalid Host header"}`) before any route or CORS logic runs.
   - Matches strictly against `ALLOWED_HOSTS` (exact `host` or `host:port` entries; no wildcards).
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
     - Protected by an IP-keyed sliding-window rate limiter (**5 attempts per 5 minutes** per client IP → `429 Too Many Requests` with `Retry-After`).
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
            except OSError:
                continue
        raise SSRFConnectionError(...)
```

### 7.2 Validation Rules & Guardrails
- **Scheme check**: Only `http` and `https`. Rejects `file://`, `ftp://`, `gopher://`, `data://`, etc., and rejects embedded userinfo (`http://user:pass@host`) or non-standard control characters.
- **Port check**: Allow ports `80` and `443` by default (configurable allow-list in `SSRFPolicy` for test fixtures).
- **IP denylist (fail-closed)**: Every A and AAAA address returned for `host` is validated. If **any** resolved IP is forbidden (preventing dual-homed DNS trickery where one A record is public and another is `127.0.0.1`), the request is rejected with `SSRFBlockedError`:
  - IPv4-mapped IPv6 (`::ffff:0:0/96`): unwrapped via `ip.ipv4_mapped` and checked against all IPv4 rules.
  - Standard Python `ipaddress` checks: `is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved`, `is_unspecified`.
  - Explicit extra CIDRs:
    - CGNAT: `100.64.0.0/10`
    - IPv6 ULA: `fc00::/7`
    - Benchmark/test nets: `198.18.0.0/15`, `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`
    - Configured Docker bridge CIDRs (`SSRF_BLOCKED_CIDRS`).
- **Manual redirect loop**:
  - `httpx.AsyncClient(follow_redirects=False, transport=SafeTransport(...))`.
  - Follows `301`, `302`, `303`, `307`, `308` up to **5 hops** max, resolving relative `Location` headers via `urljoin`, and re-running scheme + DNS/IP validation on every hop.
  - Returns a `FetchResult(status_code, final_url, permanent_redirect_url, headers, body_bytes, content_type)` where `permanent_redirect_url` is set **only** if *every* redirect hop in the chain was permanent (`301`/`308`), matching RFC 9110.
- **Timeouts & size / decompression-bomb caps**:
  - Default timeouts: `connect=10s`, `read=20s`, `total=30s`.
  - Checks `Content-Length` before streaming **and** enforces a hard byte counter while reading `response.aiter_bytes()` (default **5 MB** for feeds/HTML; caller-configurable up to **20 MB** for PDFs in `1f`).
  - **Decompression bomb protection**: `httpx` transparently decompresses `gzip`/`br`/`deflate` on `aiter_bytes()`, so counting bytes emitted by `aiter_bytes()` caps the *decompressed* size directly (and checking raw `Content-Length` caps compressed wire bytes).
- **Per-Domain Politeness (`DomainRateLimiter`)**:
  - In-process `asyncio.Lock` + monotonic timestamp per normalized host: guarantees **concurrency = 1 per domain** and at least **1.5 seconds** between requests to the same domain across `extract_content` (and `mini_research` in `1f`).

---

## 8. Job Queue, Worker Modes & Metrics (`ADR 0003`)

### 8.1 PostgreSQL Job Queue (`smartrss/jobs/queue.py`)
- **Enqueue (`enqueue`)**:
  ```sql
  INSERT INTO jobs (job_type, dedupe_key, payload, feed_id, item_id, run_id, priority, run_after)
  VALUES (:job_type, :dedupe_key, :payload, :feed_id, :item_id, :run_id, :priority, :run_after)
  ON CONFLICT (job_type, dedupe_key) WHERE status IN ('queued', 'running')
  DO NOTHING
  RETURNING id;
  ```
  Dedupe keys in `1a`:
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
- **Complete / Retry / Dead-Letter**:
  - On success: `status = 'done'`, `finished_at = now()`, `queue_wait_ms = EXTRACT(EPOCH FROM (started_at - run_after))*1000` (clamped `>= 0`), `duration_ms`, plus inserts a `stage_metrics` row.
  - On transient error with `attempts < max_attempts` (default `5`): resets `status = 'queued'`, clears lock/lease, sets `run_after = now() + min(60 * 2^(attempts-1), 3600)s`, records `last_error`, inserts a `stage_metrics` row (`status='failed'`).
  - On exhausted attempts (`attempts >= max_attempts`) or permanent fatal error: `status = 'failed'` (dead-letter), `finished_at = now()`, `last_error`, visible in `/api/v1/system/jobs/dead-letter` for manual retry or dismiss.
- **Stale-Lease Reaper**:
  - Runs every 60s under advisory lock `LOCK_REAPER`: finds `status = 'running' AND lease_expires_at < now()`. Resets rows with `attempts < max_attempts` to `queued` (with backoff) and transitions rows with `attempts >= max_attempts` to `failed` (`last_error = 'lease expired (worker crash/timeout)'`).

### 8.2 Advisory Lock Keys (`smartrss/jobs/locks.py`)
Fixed 64-bit integer constants acquired via `pg_try_advisory_lock(key)` (session-scoped) or `pg_try_advisory_xact_lock(key)`:
- `LOCK_SCHEDULER = 0x5352_0001` — periodic daemon scheduler tick (prevents duplicate dispatch across replicas).
- `LOCK_REAPER = 0x5352_0002` — stale-lease reaper.
- `LOCK_RUN_ONCE = 0x5352_0003` — prevents overlapping `smartrss worker run-once` cron invocations from racing batch accounting (if lock is held, the overlapping invocation logs a clear message and exits cleanly with code `0` without double-processing).

### 8.3 Execution Modes (`smartrss worker run` & `smartrss worker run-once`)
- **Daemon Mode (`smartrss worker run`)**:
  - Runs an async supervisor managing:
    1. Scheduler task (every `WORKER_POLL_INTERVAL_SECONDS`): dispatches due `poll_feed` jobs (`next_fetch_at <= now()`), hourly `expire_items`, daily `retention_cleanup`.
    2. Reaper task (every 60s): reclaims stale leases.
    3. `WORKER_IO_CONCURRENCY` (default `4`) I/O worker tasks claiming `['poll_feed', 'extract_content', 'expire_items', 'retention_cleanup']`.
  - Handles `SIGTERM`/`SIGINT` gracefully: stops claiming new jobs, waits up to 25s for in-flight jobs to finish cleanly before exit.
- **One-Shot Batch Mode (`smartrss worker run-once`)**:
  - CLI options: `--stage all|poll|extract|expire` (default `all`), `--feed <uuid>` (optional single-feed filter), `--force-poll` (ignore `next_fetch_at`), `--max-duration <seconds>` (optional safety cap), `--json` (machine-readable summary for cron logs).
  - Workflow:
    1. Acquires `LOCK_RUN_ONCE` (if already held, exits `0` with `{"status": "skipped_locked"}`).
    2. Creates a `pipeline_runs` row (`trigger='cron_cli'`, `stages=[...]`, `status='running'`).
    3. Dispatches due jobs for the requested stages (tagging them with `run_id`).
    4. Runs the I/O worker pool until no queued/running jobs matching the requested stages remain (or `--max-duration` expires).
    5. Finalizes `pipeline_runs` (`status = 'completed' | 'partial' | 'failed'`, `duration_ms`, per-stage counts, `jobs_failed`), prints summary (human-readable table or JSON), releases lock, and exits (`0` on `completed`, `2` on `partial`, `1` on fatal error).

---

## 9. Ingestion & Lifecycle Stages

### 9.1 `poll_feed` (`smartrss/worker/stages/poll_feed.py`)
1. **Pre-check**: Load feed row. If `status IN ('paused', 'unsubscribed')` or `health_state == 'gone'`, mark job `done` (`stage_metrics.status = 'skipped'`).
2. **HTTP Fetch (outside DB write tx)**:
   - Call `SafeHttpClient.fetch(feed.feed_url, headers=conditional_headers, max_bytes=5_000_000)`.
   - `304 Not Modified`: reset `consecutive_errors = 0`, `health_state = 'healthy'`, compute `next_fetch_at` (respecting `Cache-Control: max-age` floor + ±10% jitter), commit.
   - `410 Gone`: set `health_state = 'gone'`, `last_error = 'HTTP 410 Gone'`, `next_fetch_at = NULL`, emit `feed` `upsert` in `sync_changes`, commit.
   - `429 Too Many Requests`: parse `Retry-After` (seconds or RFC 7231 HTTP-date, clamped `[60s, 24h]`), schedule `next_fetch_at = now() + retry_after + jitter`, increment `consecutive_errors`, update `health_state` (`degraded`/`broken`), commit.
   - Network / SSRF / 5xx / 4xx / XML parse error: increment `consecutive_errors`, set `health_state = 'broken'` if `>= 5` else `'degraded'`, `last_error`, exponential backoff `min(poll_interval * 2^(errors-1), 24h)` + ±10% jitter, commit.
   - Permanent redirect (`301`/`308` chain): check if another `feeds` row already owns `permanent_redirect_url`.
     - If **collision**: keep original `feed_url`, set `health_state = 'broken'`, `last_error = f"Permanent redirect collides with existing feed {other.id}: {new_url}"` (never crash on `UNIQUE`).
     - If **no collision**: update `feed.feed_url = permanent_redirect_url`.
3. **Parse & Normalize (outside DB write tx)**:
   - Parse bytes with `feedparser.parse(body_bytes)`.
   - For each entry:
     - Extract `raw_url` (reject/skip entries with non-HTTP(S) link or missing link+title), compute `canonical_url` via `domain/url_canon.py`.
     - Derive `guid`: entry `id` if non-empty, else `canonical_url`.
     - Parse `published_at` (prefer `published_parsed`, fallback `updated_parsed`), clamped to `min(parsed_dt, fetched_at)`.
     - Extract `raw_summary` (prefer `content[0].value` if longer than `summary`, else `summary`).
     - Compute `content_hash = sha256(normalize(title + "\n" + raw_summary))`.
4. **Persist Transaction (short DB tx + `SyncLogWriter` at end)**:
   - **Backfill cap (`FR-FEED-05`)**: If `feed.initial_backfill_done == FALSE`:
     - Sort entries descending by `(published_at or fetched_at)` (preserving feed order on ties).
     - Top `N` (`app_settings.backfill_cap`, default `10`) entries:
       - `state = 'read'` if `feed.status == 'muted'` else `'unread'` *(T-01 / `S-05`)*.
       - Enqueue `extract_content` job (priority `90` for initial backfill vs `50` steady-state, or `fallback_rss` if `feed.fetch_full_text == FALSE`).
     - Entries beyond `N`:
       - `state = 'expired'`, `expired_at = fetched_at`, `extraction_status = 'fallback_rss'` (no `extract_content` job enqueued).
     - Set `feed.initial_backfill_done = TRUE`.
   - **Steady-state dedup & updates**:
     - Lookup existing item by `(feed_id, guid)`, falling back to `(feed_id, canonical_url)`.
     - If **new**: insert with `state = 'read'` if `feed.status == 'muted'` else `'unread'`, enqueue `extract_content` (priority `50`) if `feed.fetch_full_text` else set `extraction_status = 'fallback_rss'`.
     - If **existing**: if `item.state == 'unread'` and `item.fetched_at >= now() - 7 days` and `new_content_hash != item.content_hash`, update `title`, `raw_summary`, `content_hash` and re-enqueue `extract_content` (never revert `read` → `unread`).
   - Update feed metadata (`etag`, `last_modified`, `last_fetched_at`, `last_success_at`, `next_fetch_at`, `consecutive_errors = 0`, `health_state = 'healthy'`, `last_error = NULL`).
   - Call `sync_log.record(session, [feed_upsert, *item_upserts])` and commit.

### 9.2 `extract_content` (`smartrss/worker/stages/extract_content.py` + `smartrss/extract/`)
1. Load item + parent feed. If `not feed.fetch_full_text`, set `extraction_status = 'fallback_rss'`, commit, done.
2. Acquire `DomainRateLimiter` slot for `urlparse(item.raw_url).hostname`.
3. Fetch `item.raw_url` via `SafeHttpClient` (`max_bytes = 5_000_000`).
   - If HTTP status >= 400 (e.g., 403/404/paywall), SSRF blocked, timeout, or `Content-Type` is not HTML/XHTML: mark `extraction_status = 'fallback_rss'`, commit (do **not** retry 403/404/SSRF errors; retry only transient 5xx/timeouts).
4. **Isolated Subprocess Extraction**:
   - Launch `asyncio.create_subprocess_exec(sys.executable, "-m", "smartrss.extract.runner", stdin=PIPE, stdout=PIPE, stderr=PIPE, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})` — **zero DB credentials or app secrets in child env**.
   - `runner.py` immediately sets POSIX limits (`resource.setrlimit(RLIMIT_CPU, (15, 15))`, `RLIMIT_AS` `(512 MiB, 512 MiB)`, `RLIMIT_FSIZE` `(0, 0)` — no disk writes), reads JSON `{url, html}` from `stdin`, calls `trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_precision=False)`, truncates extracted plain text to **500,000 chars**, and writes JSON `{ok, text, error}` to `stdout`.
   - Parent enforces **20-second wall-clock timeout** (killing child process group on timeout) and caps `stdout` read to **1 MB**.
5. **Persist (short DB tx)**:
   - If `ok` and `len(text.strip()) >= 100`: set `extracted_text = text`, `extraction_status = 'ok'`, recompute `content_hash = sha256(normalize(title + "\n" + text))`.
   - Otherwise: set `extraction_status = 'fallback_rss'` (keeping `raw_summary` intact).
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
- **`retention_cleanup`** (daily):
  - Deletes `jobs` where `status = 'done' AND finished_at < now() - interval '30 days'`.
  - Deletes `sync_changes` where `changed_at < now() - interval '90 days'`.
  - Deletes `stage_metrics` and `pipeline_runs` where `created_at / started_at < now() - interval '180 days'`.

---

## 10. Milestone `1a` API & CLI Surface

### 10.1 REST API (`/api/v1`)
All routes except `/health` and `/auth/pair` require `Authorization: Bearer <token>`.

| Method & Path | Purpose | Notes |
| :--- | :--- | :--- |
| `GET /api/v1/health` | Unauthenticated liveness/readiness | Returns `{status: "ok", db: "ok", version}` (no secrets). |
| `POST /api/v1/auth/pair` | Exchange one-time pairing code for bearer token | Rate-limited (5 / 5m / IP); JSON body `{code, device_name, platform}`. |
| `POST /api/v1/auth/pairing-codes` | Create a new 5-minute pairing code | Authenticated; returns `{code, expires_at}`. |
| `GET /api/v1/devices` | List paired/revoked devices | Returns metadata only (never `token_hash`). |
| `POST /api/v1/devices/{id}/revoke` | Revoke a device's bearer token | Immediate `401` on next request from that device. |
| `GET /api/v1/categories` | List active categories | Ordered by `sort_order, name`. |
| `POST /api/v1/categories` | Create category | Emits `category` `upsert` in `sync_changes`. |
| `PATCH /api/v1/categories/{id}` | Rename / reorder category | Emits `category` `upsert`. |
| `DELETE /api/v1/categories/{id}` | Soft-delete category (`deleted_at`) | Sets `feeds.category_id = NULL` on children; emits `category` `delete` + affected `feed` `upsert`s. |
| `GET /api/v1/feeds` | List feeds (optional `?status=&health_state=`) | Includes health & config fields. |
| `POST /api/v1/feeds` | Subscribe to a feed URL | Validates URL via `SSRFPolicy` before saving; if `feed_url` exists as `unsubscribed`, reactivates (`status='active'`, `initial_backfill_done=FALSE`); enqueues immediate `poll_feed` (priority `10`). |
| `GET /api/v1/feeds/health` | Feed Health dashboard summary | Returns feeds sorted by severity (`gone`, `broken`, `degraded`, `healthy`) with error counts & staleness. |
| `POST /api/v1/feeds/import-opml` | Import OPML 2.0 subscriptions | JSON body `{opml_xml}`; creates missing categories, subscribes/reactivates feeds (all starting with `initial_backfill_done=FALSE`), enqueues `poll_feed` jobs; returns `{imported, reactivated, skipped_duplicates, rejected_invalid}`. |
| `GET /api/v1/feeds/export-opml` | Export active/paused/muted subscriptions | Returns `application/xml` OPML 2.0 grouped by category. |
| `GET /api/v1/feeds/{id}` | Get single feed | |
| `PATCH /api/v1/feeds/{id}` | Update feed config (`title`, `status`, `category_id`, `poll_interval_minutes`, `expiration_days_override`, `fetch_full_text`, `research_policy`, `curation_instructions`) | Updates per-field `field_updated_at` for structured fields; enforces optimistic `expected_config_version` (`409 Conflict`) when editing `curation_instructions`; emits `feed` `upsert`. |
| `POST /api/v1/feeds/{id}/poll` | Trigger immediate poll | Enqueues `poll_feed` at priority `10` (deduped). |
| `GET /api/v1/items` | Paginated item inspection (`1a` verification) | Filters: `feed_id`, `state`, `is_starred`, `limit` (max 100), `offset`. |
| `GET /api/v1/items/{id}` | Single item detail | |
| `GET /api/v1/system/health` | System health summary | DB status, queue counts by status/type, dead-letter count, last 10 `pipeline_runs`, `instance_epoch`. |
| `GET /api/v1/system/metrics?window=24h\|7d\|30d` | Pipeline timing metrics | Exact SQL `percentile_cont(0.5/0.95)`, `avg`, `max`, failure rate per `job_type`, plus per-feed cost rollup. |
| `GET /api/v1/system/jobs/dead-letter` | List `failed` jobs | Paginated with `last_error`, `attempts`, `payload`. |
| `POST /api/v1/system/jobs/{id}/retry` | Requeue a dead-letter job | Resets `status='queued'`, `attempts=0`, `run_after=now()`. |
| `POST /api/v1/system/jobs/{id}/dismiss` | Delete a dead-letter job | Removes acknowledged failure from dead-letter queue. |

### 10.2 CLI Entrypoint (`smartrss`)
- `smartrss pair create --name <name> [--platform ubuntu|android|web]`
- `smartrss devices list` / `smartrss devices revoke <device_id>`
- `smartrss worker run` (continuous daemon)
- `smartrss worker run-once [--stage all|poll|extract|expire] [--feed <id>] [--force-poll] [--max-duration <sec>] [--json]`
- `smartrss db migrate` (runs `alembic upgrade head`)
- `smartrss db rotate-epoch` (generates new `sync_state.instance_epoch` UUID — invoked by `scripts/restore-drill.sh` and real restores)

---

## 11. Backups & Restore Drill (`NFR-OPS-01`)

- **`scripts/backup.sh`**:
  1. Runs `docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc` writing atomically via `.tmp` → `${BACKUP_DIR}/smartrss-${TIMESTAMP}.dump`.
  2. Verifies non-empty archive with `pg_restore --list`.
  3. Prunes local dumps older than **14 days** (`find "$BACKUP_DIR" -name 'smartrss-*.dump' -mtime +14 -delete`).
  4. If `BACKUP_RSYNC_TARGET` is set, runs `rsync -az --remove-source-files=false` to mirror backups to the secondary LAN host.
- **`scripts/restore-drill.sh`** (`make restore-drill`):
  1. Takes a fresh dump (or uses latest in `BACKUP_DIR`).
  2. Creates a clean temporary database `smartrss_restore_drill` inside `db` (or `db-test`), runs `pg_restore --no-owner --clean --if-exists`.
  3. Captures `old_epoch` from `sync_state`, invokes `smartrss db rotate-epoch` against the restored DB, asserts `new_epoch != old_epoch`, verifies Alembic head matches, and verifies expected table row counts (`feeds`, `items`, `devices`).
  4. Drops the temporary drill database and exits `0`.

---

## 12. Verification & Test Plan (`1a` Exit Criteria)

Per [development-process.md §2](../development-process.md#2-testing-standards), target **>= 80%** overall backend coverage and **>= 90%** line/branch coverage on `security/safe_http.py`, `security/auth.py`, `security/middleware.py`, `domain/url_canon.py`, `domain/backoff.py`, `domain/expiration.py`, `jobs/queue.py`, `worker/stages/poll_feed.py`, and `worker/stages/expire_items.py`.

### 12.1 Automated Test Matrix
| Test Suite | Key Cases Verified |
| :--- | :--- |
| **Unit (`tests/unit/`)** | URL canonicalization (tracking params, fragments, case, default ports); backoff & `Retry-After` (delta-seconds, HTTP-date, `Cache-Control: max-age`, jitter bounds); `effective_ttl_days` (global default, feed override, `0` = never, starred immunity, `unexpired_at` reset, clamped `published_at`); SSRF IP classifier (RFC1918, loopback, link-local, CGNAT, IPv6 ULA, IPv4-mapped IPv6 `::ffff:127.0.0.1`, Docker CIDR, non-HTTP schemes); OPML 2.0 round-trip & malformed XML; pairing code format/normalization. |
| **Boundary (`tests/boundary/`)** | `SafeHttpClient`: injectable DNS returning public vs. private IPs, dual-homed DNS (1 public + 1 private → blocked), IP pinning verified (TCP connect receives validated IP while TLS SNI / `Host` keeps hostname), multi-hop redirects landing on `192.168.1.1` / `169.254.169.254` → blocked, >5 redirects → blocked, 5 MB wire cap & gzip decompression bomb → blocked; Isolated extractor subprocess: clean HTML extraction, hostile/deeply-nested HTML bomb hitting CPU/RAM/timeout cap without crashing parent worker, confirmation that `DATABASE_URL` is absent from child env. |
| **Integration — Real PostgreSQL (`tests/integration/`)** | Alembic `upgrade head` → `downgrade base` → `upgrade head` cleanly on empty Postgres 16; Auth & security middleware (missing/invalid/revoked bearer → `401`, expired/reused pairing code fails, pairing rate-limit → `429`, disallowed `Host` → `400`, disallowed CORS blocked, non-JSON `POST` → `415`); Job queue (`SKIP LOCKED` concurrency, `dedupe_key` uniqueness, lease heartbeat renewal, stale-lease reclaim after simulated crash, dead-letter transition & manual retry); Poller (`ETag`/`304`, `301` normal migration + `301` URL collision → `broken`, `410` → `gone`, `429` + `Retry-After`, backfill cap `N=10` marking older items `expired`, muted feed auto-marking `read`, unstable GUID canonical-URL fallback, `content_hash` update on `unread` without reverting `read`, OPML import/export); Expiration sweeper (batch expiry, starred immunity, `0` override, `unexpired_at` grace reset, `sync_changes` emission); `worker run-once` idempotency (overlapping invocation exits cleanly without double-processing) & `stage_metrics`/`pipeline_runs` SQL `p50`/`p95` accuracy. |

### 12.2 Manual / Real-World Exit Checks
1. `make check` passes cleanly with zero warnings and meets coverage thresholds.
2. Import ~25 real RSS/Atom feeds via OPML, run both daemon mode and `smartrss worker run-once --json`, verify backfill cap (`N=10`), clean extraction + fallbacks, and populated `/api/v1/system/metrics`.
3. Run `make backup && make restore-drill` and confirm `instance_epoch` rotation succeeds cleanly.
