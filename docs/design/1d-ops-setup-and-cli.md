# Component Design 1d-ops: Guided Server Setup, Headless Feed CLI, Backup OPML & Sync Resync Fencing

> **Status:** **In Design Review** *(Drafted 2026-11-27 on branch `milestone/1d-ops`; awaiting Claude Code design review and project owner approval before implementation)*
> **Milestone:** `1d-ops` — Pre-`1d` Carry-Forward Slice ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-FEED-01`, `FR-SYNC-01`, `NFR-SEC-01`, `NFR-SEC-02`, `NFR-OPS-01`, `NFR-OPS-02` *(proposed alongside this design)*
> **Related ADRs:** [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0005](../decisions/0005-phase1-client-origin-and-offline-shell.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)
> **Carry-Forwards Resolved:**
> - **`E-09`** (One-command first-time server setup) from [`1a`–`1c` User-Test Review](../reviews/2026-11-26-1a-1c-user-test-implementation-review.md)
> - **`E-11`** (Headless feed OPML export/management CLI & OPML copy alongside backups) from [`1a`–`1c` User-Test Review](../reviews/2026-11-26-1a-1c-user-test-implementation-review.md)
> - **`1c` `forceFullResync()` carry-forward** (Lock and fence `SyncEngine.forceFullResync()` under `withSyncLock`) from [`1c` Implementation Review](../reviews/2026-11-25-1c-implementation-review.md)

---

## 1. Scope & Boundaries

Sub-milestone `1d-ops` resolves the three operational and reliability carry-forwards scheduled alongside Milestone `1d` **before** introducing the local LLM pipeline (`1d-llm-adapter-and-enrichment.md`). Separating `1d-ops` keeps both design reviews focused and delivers improved server onboarding, headless feed administration, backup completeness, and race-free full-resync recovery immediately on top of `1a`–`1c`.

### In Scope for `1d-ops`
1. **Guided First-Time Server Setup (`E-09`)**:
   - Idempotent `scripts/setup-server.sh` exposed via `make setup` (and convenience `API_ORIGIN=...` alias on `make install-client`).
   - Safe, non-destructive `.env` generation (only when absent, mode `0600`, consistent generated `POSTGRES_PASSWORD` in both `POSTGRES_PASSWORD` and `DATABASE_URL` with zero argv secret exposure, LAN IP/hostname auto-detection for `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS`, `WORKER_MODE` / `COMPOSE_PROFILES` selection).
   - Automated `docker compose up -d --build --wait`, `smartrss db migrate`, end-to-end `/api/v1/health` verification via the `web` reverse proxy, optional `systemd` user timer installation (`deploy/systemd/smartrss-worker.{service,timer}`) for cron mode, optional `--opml` feed seeding, and final pairing code + exact laptop install command output.
2. **Headless Feed Administration CLI & Backup OPML Snapshot (`E-11`, §3a `Q-1`)**:
   - Extract shared transactional feed service (`backend/src/smartrss/feeds/service.py`) used by **both** FastAPI routes (`api/routes/feeds.py`) and the CLI (`cli.py`) so SSRF/syntax validation, category `FOR SHARE` lock ordering (`C8`), monotonic online LWW timestamps (`C7`), reactivation field rules (`D3`), `SyncLogWriter` (`D2`), and poll job enqueueing never drift apart.
   - Add `smartrss feeds` Typer subcommand group (`list`, `add`, `import-opml`, `export-opml`), including stdin (`-`) streaming with bounded 2 MiB read cap (`S5`) and pure XML stdout for zero-bind-mount `docker compose exec -T api` workflows.
   - Extend `scripts/backup.sh` to write a verified mode-`0600` OPML snapshot (`smartrss-feeds-<timestamp>.opml`) alongside each `pg_dump`, with matching local (14-day) and optional off-host `rsync` (30-day, no `--delete`) retention rotation.
3. **Locked & Fenced `SyncEngine.forceFullResync()` (`1c` Carry-Forward)**:
   - Persist a dedicated resync request marker in `client_meta['full_resync_requested']` (separate key from `sync_meta` so an in-flight pull's `setSyncMeta` can never overwrite it), notify peers via `BroadcastChannel`, and execute the reset (`bootstrap_meta`/`bootstrap_items` clear + `sync_cursor: null` + request consumption) strictly inside `executeSingleCycle(lock)`'s fenced Dexie `rw` transaction under `withSyncLock` (`assertAndRenewInTx()`).
   - Ensure the drain loop in `tryRunLockedCycle` checks for a pending resync request before releasing the lock.

### Explicitly Out of Scope for `1d-ops`
- **No database schema changes / no Alembic migrations**: all operations use existing `1a`–`1c` tables (`feeds`, `categories`, `jobs`, `sync_state`, `sync_changes`, `devices`, `pairing_codes`).
- **No LLM runtime, enrichment, prompt config, or `1d` UI editors**: deferred cleanly to `docs/design/1d-llm-adapter-and-enrichment.md`.
- **No `sudo` automation**: `setup-server.sh` never invokes `sudo`; it prints exact copy-pasteable commands when system packages or `docker` group membership are missing.
- **No interactive OPML UI redesign**: the existing **Export OPML** button in `FeedManagerView.tsx` already satisfies interactive use (per owner amendment to `E-11`).

---

## 2. Key `1d-ops` Design Trade-offs & Decisions

| # | Decision Area | Chosen Approach | Alternative(s) Considered | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **1d-ops-D01** | **Split `1d-ops` into its own component spec** | Separate `docs/design/1d-ops-setup-and-cli.md` reviewed & implemented ahead of `1d-llm-adapter-and-enrichment.md`. | Fold into the `1d` LLM design doc. | Keeps both specs small, reviewable, and independently verifiable; no DB migration or LLM dependency is tangled with ops scripts and client sync fencing. |
| **1d-ops-D02** | **Shared `smartrss.feeds.service` module vs. CLI-local SQL** | Extract core `create/reactivate`, `import_opml`, `export_opml`, and `list_feeds` logic into `backend/src/smartrss/feeds/service.py` shared by `api/routes/feeds.py` and `cli.py`, and add it to `CRITICAL_MODULES` (`>= 90%`). | Duplicate ORM logic in `cli.py` or call HTTP API via bearer token. | Eliminates lock-order (`C8`), LWW (`C7`), SSRF/syntax (`E-10-F1`), and `SyncLogWriter` (`D2`) drift; works headlessly inside the container with zero pairing token required. |
| **1d-ops-D03** | **Host-to-container OPML I/O via stdin/stdout (`-`)** | `smartrss feeds import-opml -` reads UTF-8 OPML from stdin (capped at 2 MiB + 1 byte before decode) and `smartrss feeds export-opml` writes pure XML to stdout (default `-`). | Bind-mount a host directory into the `api` container. | Works out of the box via `docker compose exec -T api ... < file.opml` and `> file.opml` with zero `docker-compose.yml` bind-mount changes and zero host-UID/container-UID permission issues. |
| **1d-ops-D04** | **Secret-safe `.env` rendering (`S3`)** | Generate `POSTGRES_PASSWORD` (`secrets.token_urlsafe(32)`) and render `.env` entirely inside a dedicated helper script (`scripts/_render_env.py`, invoked by `setup-server.sh`), writing atomically with `os.open(..., 0o600)` + `os.replace()`. | `openssl rand` + `sed` in bash passing password on argv. | Password never appears in any process argv (`ps`), shell history, or stdout/stderr; URL-safe charset avoids PostgreSQL URI escaping bugs; atomic `0600` creation avoids world-readable window; unit-testable directly in Python. |
| **1d-ops-D05** | **Backup OPML export failure policy** | `scripts/backup.sh` runs `docker compose exec -T "$API_SERVICE" smartrss feeds export-opml` (falling back to `docker compose run --rm -T --no-deps "$API_SERVICE"` if `api` is stopped; or local CLI under `BACKUP_USE_LOCAL_CLI=1`), verifies non-empty `<opml`…`</opml>` output without running an XML parser on the host, writes mode `0600`, and rotates alongside `.dump`. | Silently ignore OPML export failure, or run `pg_dump` only. | Fulfils `NFR-OPS-01`/`NFR-OPS-02` reliably; `run --rm --no-deps` fallback handles maintenance windows where `db` is up and `api` is down; failure exits non-zero so cron/systemd alerts rather than silently skipping OPML backups. |
| **1d-ops-D06** | **Separate `client_meta['full_resync_requested']` key for `forceFullResync()`** | Store `{ request_id, requested_at }` in its own `client_meta` row (`full_resync_requested`), never inside `sync_meta`; consume it only inside `executeSingleCycle(lock)`'s fenced Dexie `rw` transaction. | (a) Put boolean on `SyncMeta`; (b) block `forceFullResync()` directly on `withSyncLock` without a persisted request. | An in-flight pull in another tab calls `setSyncMeta({ ...staleMeta, sync_cursor: next_seq })`, which would clobber a flag stored on `SyncMeta`. A separate `client_meta` row is immune to clobbering, survives lease steals/reloads, works identically across Web Locks and IDB lease fallback, and avoids wiping `sync_cursor` while offline. |

---

## 3. Repository & File Layout

```text
Makefile                                       # Add `setup` target (SETUP_ARGS); add `API_ORIGIN` alias to `install-client`
README.md                                      # Document `make setup`, `smartrss feeds` CLI, backup OPML copy, and worker systemd timer
deploy/systemd/
├── smartrss-worker.service                    # New: oneshot cron-mode worker unit (flock + docker compose run --rm -T + journald)
└── smartrss-worker.timer                      # New: 15-minute Persistent=true timer for cron-mode worker
scripts/
├── setup-server.sh                            # New: idempotent guided first-time server setup (E-09)
├── _render_env.py                             # New: secret-safe atomic 0600 .env generator & non-secret .env inspector (S3)
├── backup.sh                                  # Updated: export + verify mode-0600 OPML snapshot + local/remote rotation (E-11)
└── check-critical-coverage.py                 # Updated: add `src/smartrss/feeds/service.py` to CRITICAL_MODULES (>= 90%)
backend/src/smartrss/
├── feeds/
│   ├── __init__.py                            # New package
│   └── service.py                             # New: shared transactional feed add/reactivate, list, import_opml, export_opml
├── api/routes/feeds.py                        # Refactored to delegate create/import/export/list to `smartrss.feeds.service`
└── cli.py                                     # Add `feeds` subcommand group (`list`, `add`, `import-opml`, `export-opml`)
frontend/src/
├── db/clientMeta.ts                           # Add `FullResyncRequestMeta`, `getFullResyncRequest`, `setFullResyncRequest`, `clearFullResyncRequest`
└── sync/engine.ts                             # Lock & fence `forceFullResync()` via `full_resync_requested` + `BroadcastChannel` + drain loop
backend/tests/
├── unit/test_1d_ops_setup_and_backup.py       # New: `_render_env.py`, `setup-server.sh` idempotency/prereqs/flags, `backup.sh` OPML + rotation
└── integration/test_1d_ops_feeds_cli.py       # New: real-Postgres `feeds` service & CLI (`add`, `list`, `import-opml`, `export-opml` round-trip, lock/sync/SSRF)
frontend/src/test/unit/
└── review_regressions.test.ts                 # Add deterministic concurrency tests for `forceFullResync()` under Web Locks & IDB lease fallback
```

---

## 4. Guided First-Time Server Setup (`E-09`)

### 4.1 Invocation & CLI Flags

- **Makefile target**:
  ```makefile
  setup:
  	./scripts/setup-server.sh $(SETUP_ARGS)
  ```
- **Client installer alias** (`Makefile`):
  ```makefile
  install-client:
  	./scripts/install-client.sh $(if $(API_ORIGIN),--api-origin "$(API_ORIGIN)") $(INSTALL_CLIENT_ARGS)
  ```
  Preserves existing `make install-client INSTALL_CLIENT_ARGS="--api-origin ..."` while also supporting `make install-client API_ORIGIN=http://host:8080`.
- **`scripts/setup-server.sh` options**:
  - `--non-interactive`: Never prompt on stdin; use explicit flags and auto-detected LAN defaults. Fails with exit `2` on invalid flag values.
  - `--worker-mode <daemon|cron>`: Default `daemon` (prompted interactively if omitted).
  - `--lan-host <host-or-ip>`: Repeatable (or comma-separated) LAN hostname/IPv4 to add to `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS`. Overrides auto-detection when provided.
  - `--lan-port <1..65535>`: Default `8080`.
  - `--ssrf-blocked-cidrs <cidrs>`: Optional comma-separated CIDRs (e.g., home WAN `/32`) written to `SSRF_BLOCKED_CIDRS`; validated via Python `ipaddress.ip_network(..., strict=False)` before writing.
  - `--opml <path>`: Optional host OPML file to import after migration. Validated for existence, readability, and `<= 2 MiB` **before** running Docker Compose.
  - `--device-name <name>`: Default `"Primary Laptop"` (for initial pairing code).
  - `--device-platform <ubuntu|android|web>`: Default `ubuntu`.
  - `--install-worker-timer` / `--no-worker-timer`: Controls installing `deploy/systemd/smartrss-worker.{service,timer}` in `cron` mode (interactive mode asks; `--non-interactive` defaults to skipping unless `--install-worker-timer` is explicitly passed so CI/containers without systemd user sessions work cleanly).
  - `--skip-pair`: Skip generating a pairing code (useful on scripted re-runs).
  - `--env-file <path>`: Default `$REPO_ROOT/.env` (used primarily for automated tests; Docker Compose reads `.env` in `$REPO_ROOT`).
  - `-h` / `--help`: Print usage and exit `0`.

### 4.2 Execution Flow & Idempotency Rules

Every step is safe to re-run on an already-configured host:

1. **Argument validation & fail-fast input checks** (exit `2` on error, before touching disk or Docker):
   - Parse flags; validate `--worker-mode`, `--lan-port`, `--device-platform`, `--ssrf-blocked-cidrs`, and `--opml` (exists, regular file or readable, `<= 2 MiB`).
2. **Prerequisite check (never runs `sudo`)**:
   - Execute `"$REPO_ROOT/scripts/check-prereqs.sh" --mode server` (verifies `docker`, `docker compose`, and `docker info` daemon access from `E-03`).
   - Verify `python3` is available (`>= 3.10`, stdlib only) for `_render_env.py` and health JSON verification.
   - On failure, exit `1` with actionable install / `usermod -aG docker "$USER"` instructions.
3. **Non-destructive `.env` creation (`scripts/_render_env.py`)**:
   - **If `$ENV_FILE` already exists**:
     - **Never overwrite or mutate it.** Print `"Preserved existing .env."`
     - Check file mode via `stat` — if group/world bits are set (`mode & 0o077 != 0`), print a warning recommending `chmod 600 .env`.
     - Inspect via `_render_env.py inspect` (pure key-value parser — **never `source`s `.env`** as shell code and **never prints secrets**, `S3`):
       - Warn loudly if `POSTGRES_PASSWORD` is still `change-me-to-a-strong-password`.
       - Extract non-secret display metadata (`LAN_PORT`, `WORKER_MODE`, primary non-loopback `ALLOWED_HOSTS` entry) for Steps 4–7.
   - **If `$ENV_FILE` does not exist**:
     - Auto-detect LAN IPv4 (via `ip -4 route get 1.1.1.1` parsing `src <ip>`, falling back to `hostname -I`) and short hostname (`hostname -s`, if non-localhost).
     - In interactive mode, show detected LAN host/IP and `WORKER_MODE` (`daemon` default) and ask for confirmation or edits.
     - Validate every LAN host token strictly (no whitespace, scheme `://`, slashes, commas, or control chars; if user accidentally types `http://host:8080`, strip/reject cleanly with clear message).
     - Invoke `python3 "$REPO_ROOT/scripts/_render_env.py" generate --template "$REPO_ROOT/.env.example" --output "$ENV_FILE" ...`:
       - Generates a 32-byte URL-safe password (`secrets.token_urlsafe(32)`) **in-process** (zero argv exposure, `S3`).
       - Writes the identical password into both `POSTGRES_PASSWORD=<pw>` and `DATABASE_URL=postgresql+psycopg://smartrss:<pw>@db:5432/smartrss`.
       - Sets `LAN_PORT`, `ALLOWED_HOSTS` (`localhost,localhost:<port>,127.0.0.1,127.0.0.1:<port>` + each LAN host & `<host>:<port>`, deduplicated), `CORS_ALLOW_ORIGINS` (`http://localhost:<port>,http://localhost:4180` + `http://<host>:<port>` for each LAN host, deduplicated, never `*`), `SSRF_BLOCKED_CIDRS`, `WORKER_MODE`, and `COMPOSE_PROFILES` (`daemon` when `WORKER_MODE=daemon`, empty when `WORKER_MODE=cron`).
       - Creates `<output>.tmp` with `os.open(..., O_WRONLY | O_CREAT | O_TRUNC, 0o600)` (atomic `0600` from creation, no race window), fsyncs, and `os.replace()`s to `$ENV_FILE`.
4. **Start stack, migrate, and health-check**:
   - `docker compose up -d --build --wait`
   - `docker compose exec -T api smartrss db migrate`
   - Poll `http://127.0.0.1:${LAN_PORT}/api/v1/health` (through `web` Nginx proxy → `api` → `db`) up to 30 seconds (1 s interval), verifying HTTP `200` and JSON `{"status": "ok", "db": "ok"}`. Fail with exit `1` and `docker compose logs --tail=30 api web` hint if unreachable.
5. **Cron-mode `systemd` user timer (only when `WORKER_MODE=cron`)**:
   - Ship template units:
     - `deploy/systemd/smartrss-worker.service`:
       ```ini
       [Unit]
       Description=SmartRSS one-shot background worker batch
       After=network-online.target

       [Service]
       Type=oneshot
       WorkingDirectory=__SMARTRSS_REPO_ROOT__
       ExecStart=/usr/bin/flock -n %t/smartrss-worker.lock /usr/bin/docker compose run --rm -T worker smartrss worker run-once --stage all --max-duration 840 --json
       ```
     - `deploy/systemd/smartrss-worker.timer`:
       ```ini
       [Unit]
       Description=Run SmartRSS worker batch every 15 minutes

       [Timer]
       OnCalendar=*:0/15
       Persistent=true
       RandomizedDelaySec=15s

       [Install]
       WantedBy=timers.target
       ```
   - When `--install-worker-timer` (or user confirms in interactive mode):
     - Validate `$REPO_ROOT` contains no newlines; render `__SMARTRSS_REPO_ROOT__` safely (via Python, never `sed` delimiter collisions) into `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/smartrss-worker.service` and copy `smartrss-worker.timer`.
     - Run `systemctl --user daemon-reload && systemctl --user enable --now smartrss-worker.timer`.
     - Print a reminder note: for headless servers without an active user login session, run `sudo loginctl enable-linger "$USER"` so user timers fire after reboot.
6. **Optional OPML import (`--opml <path>`)**:
   - Run `docker compose exec -T api smartrss feeds import-opml - < "$OPML_FILE"` and print the summary counts (`imported`, `reactivated`, `skipped_duplicates`, `rejected_invalid`).
   - Re-running with the same file skips existing feeds without creating duplicates.
7. **Pairing code & final summary**:
   - Unless `--skip-pair`, run `docker compose exec -T api smartrss pair create --name "$DEVICE_NAME" --platform "$DEVICE_PLATFORM"`.
   - Print a clean summary block:
     - Pairing code + 5-minute expiry (from `pair create`).
     - Connected LAN Web UI URL (`http://<primary-lan-host>:<LAN_PORT>` and `http://localhost:<LAN_PORT>`).
     - Exact copy-pasteable laptop install command:
       ```bash
       make install-client API_ORIGIN=http://<primary-lan-host>:<LAN_PORT>
       ```
     - Note that the manual path (`cp .env.example .env`, `make up`, `make migrate`) remains supported and documented.

### 4.3 Security Invariants for `setup-server.sh`
- **No `sudo`** invoked anywhere in `setup-server.sh` or `_render_env.py`.
- **No secrets in argv or logs (`S3`)**: `POSTGRES_PASSWORD` is generated and written solely inside `_render_env.py` and never echoed or passed on any CLI command line.
- **No shell evaluation of `.env`**: existing `.env` inspection uses a strict line parser in `_render_env.py`, never `source .env`.
- **Strict CORS/Host hygiene (ADR 0006)**: generated `CORS_ALLOW_ORIGINS` never includes `*`; generated `ALLOWED_HOSTS` contains only localhost + explicitly confirmed/detected LAN hosts.
- **No destructive overwrite**: an existing `.env` is never overwritten, truncated, or modified.

---

## 5. Headless Feed Administration CLI & Backup OPML Snapshot (`E-11`)

### 5.1 Shared Transactional Service (`backend/src/smartrss/feeds/service.py`)

Extract the core feed management operations currently embedded in `backend/src/smartrss/api/routes/feeds.py` into `backend/src/smartrss/feeds/service.py` so the HTTP API and CLI share one audited implementation:

1. **Typed Domain Exceptions** (mapped cleanly to HTTP `422`/`409` in `api/routes/feeds.py` and exit codes `1`/`2` in `cli.py`):
   - `FeedValidationError(ValueError)` — invalid URL syntax, blocked SSRF target, or invalid OPML.
   - `FeedCategoryNotFoundError(ValueError)` — referenced `category_id` or `category_name` does not exist or is soft-deleted (`C8`).
   - `FeedAlreadySubscribedError(ValueError)` — active/paused/muted feed already owns `feed_url` (carries `existing_feed_id: uuid.UUID`).
2. **`list_feeds_query(session, *, status_filter=None, health_state=None) -> list[Feed]`**
   - Shared ordering: `Feed.title.asc(), Feed.created_at.asc()`.
3. **`create_or_reactivate_feed(session, ssrf_policy, *, feed_url, title=None, category_id=None, category_name=None, create_category_if_missing=False, poll_interval_minutes=None, expiration_days_override=None, fetch_full_text=True, research_policy=None, curation_instructions="", fields_set=frozenset()) -> tuple[Feed, bool]`** (returns `(feed, reactivated)`):
   - Runs `validate_feed_url_syntax(cleaned)` + `ssrf_policy.validate_url(cleaned)` + `await ssrf_policy.resolve_and_validate(host, port)` **before** taking any DB row locks (`C1`, `E-10-F1`).
   - Acquires category lock **before** feed lock (`categories → feeds`, `C8`):
     - By `category_id`: `SELECT id FROM categories WHERE id = :id AND deleted_at IS NULL FOR SHARE` → missing raises `FeedCategoryNotFoundError`.
     - By `category_name` (CLI ergonomics): `SELECT * FROM categories WHERE deleted_at IS NULL ORDER BY id ASC FOR SHARE`, match case-insensitively (`lower(name)`). If absent and `create_category_if_missing=True`, insert `Category`, flush, and emit `SyncEvent("category", cat.id, "upsert")`; else raise `FeedCategoryNotFoundError`.
   - Uses PostgreSQL `sa.func.now()` (`C5`), locks existing `Feed` `FOR UPDATE`, applies monotonic `_stamp_online_lww` (`C7`), applies only `fields_set` on reactivation (`D3`), enqueues `poll_feed` (`priority=10, bump_existing=True`), and records `SyncEvent("feed", feed.id, "upsert")` via `sync_log.record()` (`D2`).
   - Caller owns `await session.commit()` (`C6`).
4. **`import_opml_subscriptions(session, ssrf_policy, *, opml_xml, default_category_id=None) -> OpmlImportSummary`**:
   - Moves the existing verified OPML import implementation verbatim from `api/routes/feeds.py:148-300` (keeping `parse_opml` 2 MiB / depth caps `S5`, active category `FOR SHARE` lock + `default_category_id` validation `C8`/`K-01-F1`, `validate_feed_url_syntax` + `validate_url_and_literal_ip` `E3`/`E-10-F1`, `_sanitize_site_url` `S4`, reactivation LWW stamps, `enqueue_job`, and `sync_log.record` `D2`).
   - Caller owns `await session.commit()` (`C6`).
5. **`export_opml_xml(session) -> str`**:
   - Moves the existing verified OPML export query (`Feed.status IN ('active','paused','muted')`, `OUTER JOIN` active categories, `ORDER BY Category.sort_order ASC NULLS FIRST, Feed.title ASC`) verbatim from `api/routes/feeds.py:314-337`.

### 5.2 CLI Commands (`smartrss feeds ...`)

Register a new `feeds_app = typer.Typer(help="Headless feed management commands", no_args_is_help=True)` on `app` in `backend/src/smartrss/cli.py`. **None** of these commands call `configure_logging()` so stdout remains clean for `--json` and XML redirection:

| Command | Arguments / Options | Output & Exit Codes |
| :--- | :--- | :--- |
| `smartrss feeds list` | `--status <active\|paused\|muted\|unsubscribed>`, `--health <healthy\|degraded\|broken\|gone>`, `--json` | Text table (`id  status  health  title  url`) or JSON array. Exit `0`. |
| `smartrss feeds add <url>` | `--title <str>`, `--category <name-or-uuid>`, `--create-category` (default `True` for name, ignored for UUID), `--poll-interval <int >= 5>`, `--no-full-text`, `--if-not-exists`, `--json` | Prints created/reactivated feed (or JSON). Exit `0` on created/reactivated (or already-active with `--if-not-exists`); exit `1` on duplicate active feed without `--if-not-exists`; exit `2` on validation/SSRF/category error (details to stderr). |
| `smartrss feeds import-opml <source>` | `<source>`: file path or `-` (stdin); `--default-category <uuid>`; `--json` | Reads at most `MAX_OPML_BYTES + 1` bytes (`2,097,153` bytes, `S5`) before UTF-8 decode (`UnicodeDecodeError` → exit `2`). Prints counts (`imported`, `reactivated`, `skipped_duplicates`, `rejected_invalid`) or JSON. Exit `0` on success, `2` on parse/size/category error. |
| `smartrss feeds export-opml` | `--output <path-or-dash>` (default `-` = stdout) | Writes valid UTF-8 OPML 2.0 XML to stdout (`-`) or atomically (`<path>.tmp` mode `0600` → `os.replace`) to a file path. Exit `0` on success. |

Notes:
- Category option parsing on `smartrss feeds add --category <val>`: try `uuid.UUID(val)` first → treat as `category_id`; otherwise treat as `category_name`.
- All CLI write commands build the engine/sessionmaker with `strict_sync_guard=cfg.synclog_strict_guard` so the `SyncLogWriter` guard (`D2`) is enforced identically to the API and worker.

### 5.3 Backup OPML Snapshot (`scripts/backup.sh`)

Extend `scripts/backup.sh` without breaking any existing `.dump` behavior or `restore-drill.sh`:

1. **Snapshot path & permissions**:
   - Same UTC `$TIMESTAMP` as the dump:
     - `DUMP_FILE="${BACKUP_DIR}/smartrss-${TIMESTAMP}.dump"`
     - `OPML_FILE="${BACKUP_DIR}/smartrss-feeds-${TIMESTAMP}.opml"`
     - `TMP_OPML="${OPML_FILE}.tmp"`
   - `cleanup_tmp()` trap removes both `"$TMP_FILE"` and `"$TMP_OPML"`.
2. **Export invocation** (after `pg_dump` + `pg_restore --list` succeed):
   - Configurable `API_SERVICE="${BACKUP_API_SERVICE:-api}"`.
   - If `BACKUP_USE_LOCAL_CLI=1` (test hook matching `DRILL_USE_LOCAL_CLI=1` in `restore-drill.sh`):
     `uv run --directory backend smartrss feeds export-opml > "$TMP_OPML"`
   - Else try `docker compose exec -T "$API_SERVICE" smartrss feeds export-opml > "$TMP_OPML"`; if `exec` fails because `$API_SERVICE` is not running (checked via `docker compose ps --status running --services | grep -qx "$API_SERVICE"`), invoke `docker compose run --rm -T --no-deps "$API_SERVICE" smartrss feeds export-opml > "$TMP_OPML"`.
3. **Host-side verification (zero untrusted XML parser on host, `S5`)**:
   - Assert `-s "$TMP_OPML"`, `grep -q '<opml' "$TMP_OPML"`, and `grep -q '</opml>' "$TMP_OPML"`. If any check fails, print an explicit error to stderr and exit `1`.
   - Atomically move `mv "$TMP_OPML" "$OPML_FILE"` and enforce `chmod 600 "$OPML_FILE"`.
4. **Retention & off-host `rsync`**:
   - **Local pruning** (`LOCAL_RETENTION_DAYS`, default `14`):
     ```bash
     find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'smartrss-*.dump' -o -name 'smartrss-feeds-*.opml' \) -mtime +"$LOCAL_RETENTION_DAYS" -delete
     ```
   - **Off-host `rsync`** (when `BACKUP_RSYNC_TARGET` is set; still **never** `--delete`):
     - Collect both non-empty `smartrss-*.dump` and `smartrss-feeds-*.opml` files (`local_dumps` still required non-empty; OPML files synced in the same `rsync -az` call).
     - After `rsync` succeeds, prune both `smartrss-*.dump` and `smartrss-feeds-*.opml` older than `BACKUP_REMOTE_RETENTION_DAYS` (default `30`) on the remote host.

---

## 6. Locked & Fenced `SyncEngine.forceFullResync()` (`1c` Carry-Forward)

### 6.1 Problem Recap (`1c` Implementation Review)

Currently in `frontend/src/sync/engine.ts:468-480`, `forceFullResync()` clears `bootstrap_meta` / `bootstrap_items` and writes `sync_cursor: null` directly to `client_meta` **without acquiring `withSyncLock` or fencing**. If a background `runPullLoop` (in the same tab or another tab) is mid-request when the user triggers `forceFullResync()`, the pull's commit overwrites `sync_cursor: null` with `next_seq`, silently losing the forced resync.

### 6.2 Durable Request Marker & Fenced Lock Protocol

1. **Dedicated `client_meta` key (`frontend/src/db/clientMeta.ts`)**:
   - Store resync requests under a separate key `"full_resync_requested"` (never inside `sync_meta`!):
     ```ts
     export interface FullResyncRequestMeta {
       request_id: string;
       requested_at: number;
     }
     ```
   - Add pure helpers:
     - `getFullResyncRequest(db): Promise<FullResyncRequestMeta | null>`
     - `setFullResyncRequest(db, req: FullResyncRequestMeta): Promise<void>`
     - `clearFullResyncRequest(db): Promise<void>`
   - Because `setSyncMeta()` only writes `key: "sync_meta"`, an in-flight pull on any tab can **never** overwrite `"full_resync_requested"`.
   - `clearAllLocalData()` already clears `db.client_meta`, so unpairing/re-pairing automatically clears any stale request.

2. **`BroadcastChannel` message (`frontend/src/sync/engine.ts`)**:
   - Extend `BroadcastMsg` union with `{ type: "full_resync_requested"; senderId: string }`.
   - On receiving `full_resync_requested` from another tab:
     - If `this.running`, set `this.dirty = true` so the active drain loop picks it up before releasing the lock.
     - Otherwise call `void this.triggerSync()`.

3. **Updated `SyncEngine.forceFullResync()`**:
   ```ts
   async forceFullResync(): Promise<void> {
     if (
       this.snapshot.connectionState === "origin_confirm" ||
       this.snapshot.connectionState === "auth_revoked"
     ) {
       return;
     }
     const auth = await getAuthMeta(this.db);
     if (!auth) {
       return;
     }
     // Persist request in its own client_meta row (never touches sync_cursor or bootstrap_* unlocked)
     await setFullResyncRequest(this.db, {
       request_id: uuidv4(),
       requested_at: this.now(),
     });
     if (this.running) {
       this.dirty = true;
     }
     this.postBroadcast({ type: "full_resync_requested", senderId: this.tabId });
     await this.triggerSync();
   }
   ```
   - Notice: `forceFullResync()` **never** writes `sync_cursor`, `bootstrap_meta`, or `bootstrap_items` outside the lock.

4. **Fenced consumption inside `executeSingleCycle(lock)`**:
   - Runs **only** while holding `withSyncLock` (`web_locks` or `idb_lease`).
   - Placed immediately **after** the reachability probe (lines 587–603) and **before** checking `syncMeta.sync_cursor === null`:
     ```ts
     const resyncReq = await getFullResyncRequest(this.db);
     if (resyncReq !== null) {
       await this.db.transaction(
         "rw",
         [this.db.client_meta, this.db.bootstrap_meta, this.db.bootstrap_items],
         async () => {
           await lock.assertAndRenewInTx();
           await this.db.bootstrap_meta.clear();
           await this.db.bootstrap_items.clear();
           const currentMeta = await getSyncMeta(this.db);
           await setSyncMeta(this.db, { ...currentMeta, sync_cursor: null });
           // Clear only if no newer forceFullResync() arrived after resyncReq was read
           const latestReq = await getFullResyncRequest(this.db);
           if (latestReq?.request_id === resyncReq.request_id) {
             await clearFullResyncRequest(this.db);
           }
         },
       );
     }
     ```
   - Immediately afterward, `const syncMeta = await getSyncMeta(this.db)` sees `syncMeta.sync_cursor === null` and runs `runStreamingBootstrap(api, lock)` — which preserves all `items_base`/`items` and pending outbox until Phase 2 atomic swap!

5. **Drain-loop check in `tryRunLockedCycle(ifAvailable)`**:
   - Update the exit condition at `engine.ts:548-551`:
     ```ts
     const remaining = await this.db.outbox.count();
     const pendingResync = (await getFullResyncRequest(this.db)) !== null;
     if (!this.dirty && remaining === 0 && !pendingResync) {
       break;
     }
     ```
   - Also check `MAX_DRAIN_LOOPS`: if `pendingResync` is still true when `loops === MAX_DRAIN_LOOPS` (edge case: 5 busy loops before resync ran), allow one extra drain or schedule `triggerSync` via `recheckOnSyncComplete` so a resync request is never stranded after a busy burst. Specifically: if `loops >= MAX_DRAIN_LOOPS && (await getFullResyncRequest(this.db)) !== null`, set `this.recheckOnSyncComplete = true` (which schedules a follow-up `triggerSync()` once the lock releases).

### 6.3 Interleaving & Failure-Mode Matrix

| Scenario | Behavior |
| :--- | :--- |
| **Idle tab calls `forceFullResync()`** | Persists `full_resync_requested` → `triggerSync()` acquires `withSyncLock` → fenced tx clears `bootstrap_*`, sets `sync_cursor: null`, deletes `full_resync_requested` → runs `runStreamingBootstrap` → flush outbox → pull. |
| **Same tab mid-`sync/pull` calls `forceFullResync()`** | Persists `full_resync_requested`, sets `this.dirty = true`, `triggerSync()` returns via `if (this.running)`. In-flight pull commits `next_seq` cleanly (does not touch `full_resync_requested`). Drain loop sees `dirty \|\| pendingResync`, runs next iteration, consumes `full_resync_requested` in fenced tx, executes full bootstrap. |
| **Tab B holds lock mid-`sync/pull`; Tab A calls `forceFullResync()` (Web Locks or IDB lease)** | Tab A persists `full_resync_requested`, broadcasts `full_resync_requested`, enters deduplicated waiter (`waiterPending` under Web Locks / `recheckOnSyncComplete` under IDB lease). Either Tab B's drain loop sees `pendingResync` before releasing, or Tab A acquires lock right after `sync_complete` and executes the fenced reset + bootstrap. Zero lost resyncs even if `BroadcastChannel` is unavailable. |
| **IDB lease stolen before fenced reset commits** | `lock.assertAndRenewInTx()` throws `SyncLockLostError`, aborting the Dexie `rw` tx atomically. `sync_cursor`, `bootstrap_*`, and `full_resync_requested` remain untouched so the new lock holder executes the resync. |
| **Detached (offline) when `forceFullResync()` clicked** | Reachability probe returns `detached` before the fenced reset runs, so `sync_cursor` stays intact while offline and `full_resync_requested` remains queued; on reconnect, the first online cycle automatically executes the full bootstrap. |
| **Second `forceFullResync()` arrives while first reset is queued** | Distinct `request_id` prevents the first fenced tx from clearing the newer request, guaranteeing the newer resync also runs. |
| **Origin confirm / auth revoked / unpaired** | `forceFullResync()` returns early without persisting a request or triggering sync. |

---

## 7. Security & Review-Checklist Self-Check

Walked against [docs/review-checklist.md](../review-checklist.md):

| Check | How `1d-ops` Satisfies It |
| :--- | :--- |
| **S1 / S5** (Untrusted input & parser caps) | `smartrss feeds import-opml` reads at most `2 MiB + 1` bytes before UTF-8 decode and passes text through existing `defusedxml`-backed `parse_opml()` (`S5`). `scripts/backup.sh` never invokes an XML parser on the host. |
| **S2 / S4** (SSRF & scheme validation) | `smartrss.feeds.service` uses `validate_feed_url_syntax` + `SSRFPolicy` (`validate_url` + DNS on `add`; `validate_url_and_literal_ip` on bulk `import-opml` per `E3`) and `_sanitize_site_url` (`S4`). |
| **S3** (Secrets in argv/logs) | `scripts/_render_env.py` generates `POSTGRES_PASSWORD` in-process and writes `.env` atomically at mode `0600`; password never appears in argv, shell env, or stdout. Existing `.env` inspection never prints secrets or `source`s `.env`. |
| **C1 / C5 / C6 / C7 / C8** (Concurrency & DB hygiene) | `create_or_reactivate_feed` resolves DNS before DB locks (`C1`), uses PostgreSQL `sa.func.now()` (`C5`), lets callers commit (`C6`), uses monotonic `_stamp_online_lww` (`C7`), and locks active categories `FOR SHARE` (`ORDER BY id ASC`) strictly before locking `Feed` `FOR UPDATE` (`C8`). |
| **D2 / D3** (Sync log & reactivation defaults) | Shared `feeds.service` enforces `SyncLogWriter` (`strict_sync_guard=True` in CLI too) (`D2`) and applies only `fields_set` on feed reactivation (`D3`). |
| **B5 / B6 / T6** (Client locking, error survival & forced-overlap concurrency tests) | `forceFullResync()` never bypasses `withSyncLock` or re-enters an active tab cycle (`B5`), runs inside `tryRunLockedCycle`'s error-catching loop (`B6`), and is tested with deterministic barrier/hook interleavings on both Web Locks and IDB lease fallback (`T6`). |
| **T3** (Critical coverage gate) | `src/smartrss/feeds/service.py` is added to `CRITICAL_MODULES` (`>= 90%`) in `scripts/check-critical-coverage.py` in the same commit as the implementation. |

---

## 8. Verification & Test Plan

Every row below maps to named automated tests added in the implementation commit and must pass under `make check`:

| Test ID | Layer | Description & Invariants Verified |
| :--- | :--- | :--- |
| **`1d-ops-T01`** | Unit (`pytest`) | `scripts/_render_env.py` `generate`: creates mode-`0600` `.env`, identical generated URL-safe `POSTGRES_PASSWORD` in `POSTGRES_PASSWORD` and `DATABASE_URL`, deduplicated `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS` (never `*`), `COMPOSE_PROFILES=daemon` vs empty for `cron`, valid `SSRF_BLOCKED_CIDRS`, rejects invalid CIDRs/hosts/ports, and refuses to overwrite an existing `.env`. |
| **`1d-ops-T02`** | Unit (`pytest`) | `scripts/_render_env.py` `inspect`: reads non-secret metadata from an existing `.env` without executing shell metacharacters, flags default placeholder password, and never prints `POSTGRES_PASSWORD` or `DATABASE_URL`. |
| **`1d-ops-T03`** | Unit (`pytest`) | `scripts/setup-server.sh` prerequisite failure paths: mocked `PATH` missing `docker`, missing `docker compose`, and failing `docker info` each exit `1` with actionable hints and never invoke `sudo`. |
| **`1d-ops-T04`** | Unit (`pytest`) | `scripts/setup-server.sh` fresh + idempotent re-run (with mocked `docker`/`systemctl` + local Python HTTP `/api/v1/health` server): first run generates `0600` `.env`, runs compose up/migrate/health/OPML import (stdin)/pair create, prints client install command; second run preserves `.env` byte-for-byte, passes `--opml` cleanly, and succeeds with `--skip-pair`. Also covers `--worker-mode cron --install-worker-timer` rendering `smartrss-worker.{service,timer}` into `$XDG_CONFIG_HOME/systemd/user/`. |
| **`1d-ops-T05`** | Unit (`pytest`) | `scripts/backup.sh` OPML snapshot & rotation: creates verified mode-`0600` `smartrss-feeds-<ts>.opml` alongside `.dump` (tested via `BACKUP_USE_LOCAL_CLI=1` and via mocked `docker compose exec`/`run` fallback when `api` is stopped), fails non-zero if OPML output is empty/malformed (leaving no `.tmp`), prunes stale `.opml` + `.dump` older than retention locally, and includes `.opml` in mocked `rsync` + remote prune without `--delete`. |
| **`1d-ops-T06`** | Integration (Real PG) | `smartrss feeds add` + `list` (text & `--json`): creates feed with category by name (creating category + emitting category & feed `sync_changes` upserts) and by UUID, enforces `validate_feed_url_syntax` & SSRF block (exit `2`), enforces duplicate check (exit `1`) and `--if-not-exists` (exit `0`), reactivates unsubscribed feed preserving fields (`D3`), and enqueues `poll_feed`. |
| **`1d-ops-T07`** | Integration (Real PG) | `smartrss feeds export-opml` & `import-opml` round-trip (file path & stdin `-`): exports pure XML to stdout (zero log pollution) and mode-`0600` `--output` file; re-importing yields `imported=0, skipped_duplicates=N, rejected_invalid=0` with categories preserved; importing into a fresh DB restores all feeds and categories; oversized `> 2 MiB` stdin/file is rejected cleanly with exit `2` (`S5`); invalid/soft-deleted `--default-category` fails with exit `2` (`C8`). |
| **`1d-ops-T08`** | Integration (Real PG) | API route parity regression: verifies existing `POST /api/v1/feeds`, `PATCH /api/v1/feeds/{id}`, `POST /api/v1/feeds/import-opml`, and `GET /api/v1/feeds/export-opml` tests remain green after delegating to `smartrss.feeds.service` (including `C8` soft-deleted category rejection and `E-10-F1` URL checks). |
| **`1d-ops-T09`** | Frontend Unit (`vitest`) | `forceFullResync()` same-tab race (forced overlap, `T6`): hook `sync/pull` fetch with a deferred barrier; call `forceFullResync()` while pull is in flight; release pull returning `next_seq = 50`; assert pull commits cleanly without corruption, drain loop then runs fenced reset + `sync/bootstrap`, and final `sync_cursor` reflects bootstrap watermark (NOT stuck at `50`). Proved to fail when `forceFullResync()` writes unlocked. |
| **`1d-ops-T10`** | Frontend Unit (`vitest`) | `forceFullResync()` cross-tab race under **IDB lease fallback** (`forceIdbLockFallback: true`): Tab B holds lease mid-pull; Tab A calls `forceFullResync()` (with `BroadcastChannel` both enabled and disabled); assert Tab B's pull never clobbers `full_resync_requested` and full bootstrap executes once lock transfers/drains. |
| **`1d-ops-T11`** | Frontend Unit (`vitest`) | `forceFullResync()` IDB lease-stolen fencing: simulate lease stolen before the fenced reset transaction commits; assert `SyncLockLostError` aborts the reset atomically and `full_resync_requested` remains persisted so the winning tab runs the resync. |
| **`1d-ops-T12`** | Frontend Unit (`vitest`) | `forceFullResync()` offline / rapid double-click / `auth_revoked` guards: offline click preserves `sync_cursor` while detached and executes bootstrap on reconnect; rapid second `forceFullResync()` with a new `request_id` is not swallowed by the first reset; `unpaired`/`origin_confirm`/`auth_revoked` are no-ops. |
| **`1d-ops-T13`** | Unit (`pytest`) | `make install-client API_ORIGIN=...` compatibility check: verifies `API_ORIGIN` env/make variable forwards `--api-origin` cleanly alongside `INSTALL_CLIENT_ARGS`. |

### Critical-Module Coverage Gate (`make check`)
- **Backend**: Add `src/smartrss/feeds/service.py` to `CRITICAL_MODULES` (`>= 90%` branch coverage) in `scripts/check-critical-coverage.py`. Overall backend coverage remains `>= 80%`.
- **Frontend**: `src/sync/engine.ts` (`>= 90%` lines & branches) and `src/db/clientMeta.ts` (`>= 80%` lines & branches) stay enforced by `scripts/check-frontend-coverage.mjs`.

---

## 9. Documentation Sync Plan (Same Commit as Implementation)

When `1d-ops` is implemented:
- `README.md`: Add "Guided First-Time Setup (`make setup`)" section ahead of the advanced manual path, document `smartrss feeds add|list|import-opml|export-opml`, document `deploy/systemd/smartrss-worker.{service,timer}`, document the OPML snapshot in `make backup`, and show `make install-client API_ORIGIN=...`.
- `docs/design/1a-backend-ingestion-and-security.md`: Add `smartrss feeds ...` commands to §10.2 CLI table and note OPML backup snapshot.
- `docs/design/1c-client-ui-and-offline-shell.md`: Mark the `forceFullResync()` carry-forward note in §11 as resolved in `1d-ops`.
- `docs/design/README.md`, `docs/README.md`, and `AGENTS.md`: Update `1d-ops` status through `Implemented` → `In Implementation Review` → (owner) `Implementation Approved`.
