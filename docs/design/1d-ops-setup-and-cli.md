# Component Design 1d-ops: Guided Server Setup, Headless Feed CLI, Backup OPML & Sync Resync Fencing

> **Status:** **In Implementation Review** *(Design Approved by project owner 2026-11-27 after the [`1d-ops` design review](../reviews/2026-11-27-1d-ops-design-review.md) (`O-01`–`O-11`, follow-ups `O-01-F1` / `O-02-F1`) was `Verified`; implemented 2026-11-27 on branch `milestone/1d-ops` with full `1d-ops-T01`–`1d-ops-T13` verification; awaiting implementation review)*
> **Milestone:** `1d-ops` — Pre-`1d` Carry-Forward Slice ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-FEED-01`, `FR-SYNC-01`, `NFR-SEC-01`, `NFR-SEC-02`, `NFR-OPS-01`, `NFR-OPS-02` *(approved 2026-11-27)*
> **Related ADRs:** [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0005](../decisions/0005-phase1-client-origin-and-offline-shell.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)
> **Carry-Forwards Resolved:**
> - **`E-09`** (One-command first-time server setup) from [`1a`–`1c` User-Test Review](../reviews/2026-11-26-1a-1c-user-test-implementation-review.md)
> - **`E-11`** (Headless feed OPML export/management CLI & OPML portability copy alongside backups) from [`1a`–`1c` User-Test Review](../reviews/2026-11-26-1a-1c-user-test-implementation-review.md)
> - **`1c` `forceFullResync()` carry-forward** (Lock and fence `SyncEngine.forceFullResync()` under `withSyncLock`) from [`1c` Implementation Review](../reviews/2026-11-25-1c-implementation-review.md)

---

## 1. Scope & Boundaries

Sub-milestone `1d-ops` resolves the three operational and reliability carry-forwards scheduled alongside Milestone `1d` **before** introducing the local LLM pipeline (`1d-llm-adapter-and-enrichment.md`). Separating `1d-ops` keeps both design reviews focused and delivers improved server onboarding, headless feed administration, backup completeness, and race-free full-resync recovery immediately on top of `1a`–`1c`.

### In Scope for `1d-ops`
1. **Guided First-Time Server Setup (`E-09`)**:
   - Idempotent `scripts/setup-server.sh` exposed via `make setup` (and convenience `API_ORIGIN=...` alias on `make install-client`).
   - Shared backend image tag (`image: smartrss-backend:local`) in `docker-compose.yml` across `api` and `worker` + explicit image build before startup in both `setup-server.sh` and `make up`, eliminating stale worker images in cron mode (`O-01`).
   - Safe, atomically non-overwriting `.env` generation (`O-09`: `os.link` from a mode-`0600` temp file, never replacing an existing `.env`, consistent generated `POSTGRES_PASSWORD` in both `POSTGRES_PASSWORD` and `DATABASE_URL` with zero argv secret exposure, deterministic primary LAN host + optional mDNS `.local` detection for `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS`, `WORKER_MODE` / `COMPOSE_PROFILES` selection).
   - Clean ordered startup (`build` → `up -d --wait db api` → `db migrate` → `up -d --wait`, avoiding pre-migration worker tracebacks, `O-10`), end-to-end `/api/v1/health` verification via the `web` reverse proxy, optional `systemd` user timer installation (`deploy/systemd/smartrss-worker.{service,timer}`) with linger check, duplicate-crontab detection, shared lock path, and `flock -n -E 75` / `SuccessExitStatus=75` (`O-06`), optional `--opml` feed seeding, and final pairing code + exact laptop install command output.
2. **Headless Feed Administration CLI & Backup OPML Snapshot (`E-11`, §3a `Q-1`)**:
   - Extract shared transactional feed service (`backend/src/smartrss/feeds/service.py`) used by **both** FastAPI routes (`api/routes/feeds.py`) and the CLI (`cli.py`) so SSRF/syntax validation, category `FOR SHARE` lock ordering (`C8`), concurrent category-create `IntegrityError` handling via savepoint (`O-05`), monotonic online LWW timestamps (`C7`), reactivation field rules (`D3`), `SyncLogWriter` (`D2`), and poll job enqueueing never drift apart.
   - Add `smartrss feeds` Typer subcommand group (`list`, `add`, `import-opml`, `export-opml`), including opt-in `--create-category` (`O-05`), explicit status reporting & exit code `3` for `--if-not-exists` on `paused`/`muted` feeds (`O-11`), stdin (`-`) streaming with bounded 2 MiB read cap (`S5`), and pure XML stdout for zero-bind-mount `docker compose exec -T api` workflows.
   - Extend `scripts/backup.sh` to write a verified mode-`0600` OPML **portability snapshot** (`smartrss-feeds-<timestamp>.opml`, subscriptions & categories; `pg_dump` stays authoritative for feed status & per-feed settings, `O-04`) alongside each `pg_dump`, with independent local/remote retention pruning and best-effort-within-run ordering so an OPML export failure never blocks off-host `rsync` of a verified dump while still returning exit `1` at the end (`O-03`).
3. **Locked & Fenced `SyncEngine.forceFullResync()` (`1c` Carry-Forward)**:
   - Persist a dedicated resync request marker in `client_meta['full_resync_requested']` (separate key from `client_meta['sync']` so an in-flight pull's `setSyncMeta` can never overwrite it, `O-08`), notify peers via `BroadcastChannel`, re-trigger in `sync_complete` whenever either `outbox.count() > 0` or a resync request is pending, check for pending resync after lock release in the holder tab (`O-02`), and execute the reset (`bootstrap_meta`/`bootstrap_items` clear + `sync_cursor: null` + request consumption) strictly inside `executeSingleCycle(lock)`'s fenced Dexie `rw` transaction under `withSyncLock` (`assertAndRenewInTx()`).

### Explicitly Out of Scope for `1d-ops`
- **No database schema changes / no Alembic migrations**: all operations use existing `1a`–`1c` tables (`feeds`, `categories`, `jobs`, `sync_state`, `sync_changes`, `devices`, `pairing_codes`).
- **No LLM runtime, enrichment, prompt config, or `1d` UI editors**: deferred cleanly to `docs/design/1d-llm-adapter-and-enrichment.md`.
- **No `sudo` automation**: `setup-server.sh` never invokes `sudo`; it prints exact copy-pasteable commands when system packages, `docker` group membership, or `loginctl enable-linger` are missing.
- **No interactive OPML UI redesign**: the existing **Export OPML** button in `FeedManagerView.tsx` already satisfies interactive use (per owner amendment to `E-11`).

---

## 2. Key `1d-ops` Design Trade-offs & Decisions

| # | Decision Area | Chosen Approach | Alternative(s) Considered | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **1d-ops-D01** | **Split `1d-ops` into its own component spec** *(Owner decision 2026-11-27)* | Separate `docs/design/1d-ops-setup-and-cli.md` reviewed & implemented ahead of `1d-llm-adapter-and-enrichment.md`. | Fold into the `1d` LLM design doc. | Keeps both specs small, reviewable, and independently verifiable; no DB migration or LLM dependency is tangled with ops scripts and client sync fencing. |
| **1d-ops-D02** | **Shared `smartrss.feeds.service` module vs. CLI-local SQL** | Extract core `create/reactivate`, `import_opml`, `export_opml`, and `list_feeds` logic into `backend/src/smartrss/feeds/service.py` shared by `api/routes/feeds.py` and `cli.py`, and add it to `CRITICAL_MODULES` (`>= 90%`). | Duplicate ORM logic in `cli.py` or call HTTP API via bearer token. | Eliminates lock-order (`C8`), LWW (`C7`), SSRF/syntax (`E-10-F1`), and `SyncLogWriter` (`D2`) drift; works headlessly inside the container with zero pairing token required. |
| **1d-ops-D03** | **Host-to-container OPML I/O via stdin/stdout (`-`)** | `smartrss feeds import-opml -` reads UTF-8 OPML from stdin (capped at 2 MiB + 1 byte before decode) and `smartrss feeds export-opml` writes pure XML to stdout (default `-`). | Bind-mount a host directory into the `api` container. | Works out of the box via `docker compose exec -T api ... < file.opml` and `> file.opml` with zero `docker-compose.yml` bind-mount changes and zero host-UID/container-UID permission issues. |
| **1d-ops-D04** | **Secret-safe, race-free `.env` rendering (`S3`, `O-09`)** | Generate `POSTGRES_PASSWORD` (`secrets.token_urlsafe(32)`) and render `.env` inside `scripts/_render_env.py`, writing to a temp file created with `O_WRONLY \| O_CREAT \| O_EXCL` (`0o600`), fsyncing, and linking atomically via `os.link(tmp, output)` (failing cleanly with `EEXIST` if target appeared concurrently) before unlinking `tmp`. | (a) `openssl` + `sed` passing password on argv; (b) `os.replace()` (which overwrites unconditionally). | Password never appears in argv (`ps`), shell history, or stdout/stderr; URL-safe charset avoids URI escaping bugs; atomic `0600` + `os.link` eliminates both permission races and concurrent overwrite races (`O-09`). |
| **1d-ops-D05** | **Backup OPML snapshot ordering, failure policy & fidelity (`O-03`, `O-04`)** *(Owner confirmed `Q-2` 2026-11-27)* | Export OPML as a **portability snapshot** (subscriptions + categories only; `pg_dump` is authoritative for status/settings, `O-04` option (a)). Run OPML export after `pg_dump` verification, continue through independent pruning and off-host `rsync` even if OPML fails, and exit `1` at the end if OPML failed (`O-03`). | (a) Exit immediately before `rsync`; (b) silently ignore OPML failure; (c) add custom `xmlns:smartrss` OPML attributes. | Verified `pg_dump` always reaches the off-host `rsync` target even during a broken `api` upgrade, while cron/systemd still receives a non-zero alert when OPML export fails. Standard OPML 2.0 stays portable without custom XML extensions. |
| **1d-ops-D06** | **Separate `client_meta['full_resync_requested']` key for `forceFullResync()` (`O-02`, `O-08`)** | Store `{ request_id, requested_at }` in its own `client_meta` row (`key: "full_resync_requested"`), separate from `client_meta['sync']`; consume it only inside `executeSingleCycle(lock)`'s fenced Dexie `rw` transaction; re-trigger in `sync_complete` on pending resync and check after lock release in the holder tab (`O-02`). | (a) Put boolean on `SyncMeta` (`key: "sync"`); (b) block `forceFullResync()` directly on `withSyncLock` without a persisted request. | An in-flight pull calls `setSyncMeta()` (`key: "sync"`), which would clobber a flag inside `SyncMeta`. A separate `client_meta` row is immune to clobbering, survives lease steals/reloads, works identically across Web Locks and IDB lease fallback without stalling on `sync_complete`, and preserves `sync_cursor` when clicked while already detached. |
| **1d-ops-D07** | **Shared backend image for `api` and `worker` (`O-01`, `O-01-F1`, `Q-4`)** *(Owner confirmed `Q-4` 2026-11-27)* | Give **both** `api` and `worker` `image: smartrss-backend:local` AND `build: { context: ./backend }` in `docker-compose.yml`, and explicitly run `docker compose build api web` in both `setup-server.sh` and `make up`. | (a) Keep separate implicit images (`smartrss-api` / `smartrss-worker`); (b) omit `build:` on `worker` (which makes `docker compose run worker` attempt a registry pull when the image was pruned, `O-01-F1`). | Structurally eliminates version skew between `api` and cron-mode `worker` (one tag serves both services), `build api web` builds `./backend` once during setup/upgrade, and keeping `build:` on `worker` lets `docker compose run worker` self-heal via a local build if images were ever pruned (`O-01-F1`). |

---

## 3. Repository & File Layout

```text
Makefile                                       # Add `setup` target (SETUP_ARGS); update `up` to build api+web (O-01); add `API_ORIGIN` alias to `install-client`
docker-compose.yml                             # Share `image: smartrss-backend:local` with `build: { context: ./backend }` on both `api` and `worker` (O-01, O-01-F1)
README.md                                      # Document `make setup`, `smartrss feeds` CLI, backup OPML copy, worker systemd timer, DHCP/existing-.env host updates, and shared lock path
deploy/systemd/
├── smartrss-worker.service                    # New: oneshot cron-mode worker unit (flock -n -E 75 + SuccessExitStatus=75 + shared lock path + journald)
└── smartrss-worker.timer                      # New: 15-minute Persistent=true timer for cron-mode worker
scripts/
├── setup-server.sh                            # New: idempotent guided first-time server setup (E-09)
├── _render_env.py                             # New: secret-safe atomic 0600 os.link .env generator & non-secret .env inspector (S3, O-09)
├── backup.sh                                  # Updated: export + verify mode-0600 OPML snapshot, independent pruning, rsync before deferred failure exit (E-11, O-03)
└── check-critical-coverage.py                 # Updated: add `src/smartrss/feeds/service.py` to CRITICAL_MODULES (>= 90%)
backend/src/smartrss/
├── feeds/
│   ├── __init__.py                            # New package
│   └── service.py                             # New: shared transactional feed add/reactivate (with category IntegrityError savepoint, O-05), list, import_opml, export_opml
├── api/routes/feeds.py                        # Refactored to delegate create/import/export/list to `smartrss.feeds.service`
└── cli.py                                     # Add `feeds` subcommand group (`list`, `add`, `import-opml`, `export-opml`)
frontend/src/
├── db/clientMeta.ts                           # Add `FullResyncRequestMeta`, `getFullResyncRequest`, `setFullResyncRequest`, `clearFullResyncRequest`
└── sync/engine.ts                             # Lock & fence `forceFullResync()` via `full_resync_requested`, `sync_complete` re-check, post-release check & drain loop (O-02)
backend/tests/
├── unit/test_1d_ops_setup_and_backup.py       # New: `_render_env.py`, compose shared image check (O-01), `setup-server.sh` idempotency/prereqs/linger/crontab/flags, `backup.sh` OPML + rsync-on-failure + rotation
└── integration/test_1d_ops_feeds_cli.py       # New: real-Postgres `feeds` service & CLI (`add`, `list`, `import-opml`, `export-opml` round-trip, concurrent category race, muted/paused fidelity, lock/sync/SSRF)
frontend/src/test/unit/
└── review_regressions.test.ts                 # Add deterministic concurrency tests for `forceFullResync()` under Web Locks & IDB lease fallback (`T09`–`T12`)
```

---

## 4. Guided First-Time Server Setup (`E-09`)

### 4.1 Invocation & CLI Flags

- **Makefile targets**:
  ```makefile
  setup:
  	./scripts/setup-server.sh $(SETUP_ARGS)

  up:
  	./scripts/check-prereqs.sh --mode server
  	docker compose build api web
  	docker compose up -d --wait

  install-client:
  	./scripts/install-client.sh $(if $(API_ORIGIN),--api-origin "$(API_ORIGIN)") $(INSTALL_CLIENT_ARGS)
  ```
  - `make up` explicitly builds `api` (`smartrss-backend:local`, shared by `worker`) and `web` so cron-mode users running `make up` also upgrade the worker image (`O-01`).
  - `make install-client` preserves `INSTALL_CLIENT_ARGS="--api-origin ..."` while also supporting `make install-client API_ORIGIN=http://host:8080`.
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
   - Parse flags; validate `--worker-mode`, `--lan-port`, `--device-platform`, `--opml` (exists, readable, `<= 2 MiB`), AND every `--lan-host` / `--ssrf-blocked-cidrs` token via `python3 scripts/_render_env.py validate` on **all** runs (including re-runs with an existing `.env`, `G-01`).
2. **Prerequisite check (never runs `sudo`)**:
   - Execute `"$REPO_ROOT/scripts/check-prereqs.sh" --mode server` (verifies `docker`, `docker compose`, and `docker info` daemon access from `E-03`).
   - Verify `python3` is available (`>= 3.10`, stdlib only) for `_render_env.py` and health JSON verification.
   - On failure, exit `1` with actionable install / `usermod -aG docker "$USER"` instructions.
3. **Atomically non-destructive `.env` creation (`scripts/_render_env.py`, `O-07`, `O-09`)**:
   - **Primary LAN host selection (`O-07`)**:
     - If one or more `--lan-host` values are supplied: the **first** `--lan-host` is the primary LAN host, and all supplied hosts are added to `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS`.
     - Otherwise (auto-detection): detect primary outbound IPv4 via `ip -4 route get 1.1.1.1` (parsing `src <ip>`), falling back to `hostname -I`. That detected IPv4 is the **primary LAN host** (never a bare short hostname; falls back to `localhost` only if no non-loopback IPv4 is found). Additionally check `getent hosts "$(hostname -s).local"` — if and only if Avahi/mDNS resolves it, also add `<hostname>.local` to `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS`.
     - In interactive mode, show the detected primary host/IP (and any `.local` alias) and `WORKER_MODE` (`daemon` default) for confirmation or editing.
     - Validate every LAN host token strictly (no whitespace, scheme `://`, slashes, commas, or control chars).
   - **If `$ENV_FILE` already exists** (or if concurrent creation triggers `EEXIST` below):
     - **Never overwrite or mutate it.** Print `"Preserved existing .env."`
     - Check file mode — if group/world bits are set (`mode & 0o077 != 0`), print a warning recommending `chmod 600 .env`.
     - Inspect via `_render_env.py inspect` (pure key-value parser — **never `source`s `.env`** as shell code and **never prints secrets**, `S3`):
       - Warn loudly if `POSTGRES_PASSWORD` is still `change-me-to-a-strong-password`.
       - Extract non-secret display metadata (`LAN_PORT`, `WORKER_MODE`, primary non-loopback `ALLOWED_HOSTS` entry — preferring IPv4/first non-loopback host without port) for Steps 4–7.
     - Compare any **explicitly passed** `--worker-mode`, `--install-worker-timer`, `--lan-port`, `--lan-host`, and `--ssrf-blocked-cidrs` flags against the preserved `.env` via `_render_env.py check-conflicts` (`G-01`): print an explicit `WARNING` for every conflicting flag, and under `--non-interactive` exit `2` before building/starting containers so conflicting flags are never silently ignored.
   - **If `$ENV_FILE` does not exist**:
     - **Existing `pgdata` volume guard (`G-02`)**: derive the Compose `pgdata` volume name via `POSTGRES_PASSWORD=probe-only docker compose config --format json` (falling back to `${COMPOSE_PROJECT_NAME:-<repo-basename>}_pgdata`) and run `docker volume inspect`. If the volume already exists, refuse with exit `1` without creating `.env` or running `docker compose build`/`up`, and guide the operator to restore their previous `.env`, restore from `backups/`, or explicitly remove the volume (`docker volume rm <vol>`).
     - Invoke `python3 "$REPO_ROOT/scripts/_render_env.py" generate --template "$REPO_ROOT/.env.example" --output "$ENV_FILE" ...`:
       - Generates a 32-byte URL-safe password (`secrets.token_urlsafe(32)`) **in-process** (zero argv exposure, `S3`).
       - Writes the identical password into both `POSTGRES_PASSWORD=<pw>` and `DATABASE_URL=postgresql+psycopg://smartrss:<pw>@db:5432/smartrss`.
       - Sets `LAN_PORT`, `ALLOWED_HOSTS` (`localhost,localhost:<port>,127.0.0.1,127.0.0.1:<port>` + each LAN host & `<host>:<port>`, deduplicated), `CORS_ALLOW_ORIGINS` (`http://localhost:<port>,http://localhost:4180` + `http://<host>:<port>` for each LAN host, deduplicated, never `*`), `SSRF_BLOCKED_CIDRS`, `WORKER_MODE`, and `COMPOSE_PROFILES` (`daemon` when `WORKER_MODE=daemon`, empty when `WORKER_MODE=cron`).
       - Creates a sibling temp file `<output>.tmp.<pid>` using `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)`, writes & `fsync()`s, then links atomically via **`os.link(tmp_path, output_path)`** and unlinks `tmp_path` in `finally` (`O-09`). If `os.link` raises `FileExistsError`, `_render_env.py` exits with a dedicated status (e.g., `10` = preserved) and `setup-server.sh` falls cleanly into the `"Preserved existing .env"` inspect path without touching the existing file.
4. **Build shared images, start DB/API, migrate, then start remaining services (`O-01`, `O-10`)**:
   - `docker compose build api web` — builds `smartrss-backend:local` (used by **both** `api` and `worker` regardless of `COMPOSE_PROFILES`, `O-01`) and `web`.
   - `docker compose up -d --wait db api` — starts `db` and `api` first so the daemon `worker` does not log pre-migration `UndefinedTable` tracebacks (`O-10`).
   - `docker compose exec -T api smartrss db migrate` — applies Alembic migrations cleanly.
   - `docker compose up -d --wait` — starts `web` (and `worker` when `COMPOSE_PROFILES=daemon`), all running the freshly built images.
   - Poll `http://127.0.0.1:${LAN_PORT}/api/v1/health` (through `web` Nginx proxy → `api` → `db`) up to 30 seconds (1 s interval), verifying HTTP `200` and JSON subset `data.get("status") == "ok" and data.get("db") == "ok"` (tolerating additional fields such as `version`). Fail with exit `1` and `docker compose logs --tail=30 api web` hint if unreachable.
5. **Cron-mode `systemd` user timer (only when `WORKER_MODE=cron`, `O-06`, `O-10`)**:
   - Ship template units (using the same shared lock path `~/.local/state/smartrss/worker.lock` as the README crontab line, `flock -n -E 75` + `SuccessExitStatus=75` so skipped ticks don't fail the unit, and no no-op `After=network-online.target`):
     - `deploy/systemd/smartrss-worker.service`:
       ```ini
       [Unit]
       Description=SmartRSS one-shot background worker batch

       [Service]
       Type=oneshot
       WorkingDirectory=__SMARTRSS_REPO_ROOT__
       ExecStartPre=/usr/bin/mkdir -p %h/.local/state/smartrss
       ExecStart=/usr/bin/flock -n -E 75 %h/.local/state/smartrss/worker.lock /usr/bin/docker compose run --rm -T worker smartrss worker run-once --stage all --max-duration 840 --json
       SuccessExitStatus=75
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
     - **Duplicate crontab check (`O-06`)**: inspect `crontab -l 2>/dev/null | grep -F 'smartrss worker run-once' || true`. If a matching crontab line exists:
       - In interactive mode, warn and ask confirmation before installing the timer (reminding the user to remove the crontab entry so the server does not double-schedule).
       - In `--non-interactive` mode, print a prominent `WARNING` about the existing crontab entry (shared lock path prevents overlap, but both schedulers will fire) and proceed.
     - Validate `$REPO_ROOT` contains no newlines; render `__SMARTRSS_REPO_ROOT__` safely via Python into `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/smartrss-worker.service` and copy `smartrss-worker.timer`.
     - Run `systemctl --user daemon-reload && systemctl --user enable --now smartrss-worker.timer`.
     - **Linger check (`O-06`)**: run `loginctl show-user "$USER" -p Linger --value 2>/dev/null || true`. If the result is not `yes`, record a flag so Step 7 prints a prominent `WARNING` banner with the exact `sudo loginctl enable-linger "$USER"` command explaining that user timers will **not** fire after a reboot on a headless host until linger is enabled.
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
     - Prominent `WARNING` if cron-mode timer was installed without `Linger=yes` (`sudo loginctl enable-linger "$USER"`).
     - One-line operational note recommending a DHCP reservation for the server's LAN IP (referencing `README.md` for updating `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS` if the IP changes later, `O-07`).
     - Note that the manual path (`cp .env.example .env`, `make up`, `make migrate`) remains supported and documented.

### 4.3 Security Invariants for `setup-server.sh`
- **No `sudo`** invoked anywhere in `setup-server.sh` or `_render_env.py`.
- **No secrets in argv or logs (`S3`)**: `POSTGRES_PASSWORD` is generated and written solely inside `_render_env.py` and never echoed or passed on any CLI command line.
- **No shell evaluation of `.env`**: existing `.env` inspection uses a strict line parser in `_render_env.py`, never `source .env`.
- **Strict CORS/Host hygiene (ADR 0006)**: generated `CORS_ALLOW_ORIGINS` never includes `*`; generated `ALLOWED_HOSTS` contains only localhost + explicitly confirmed/detected LAN hosts.
- **Atomic non-overwriting creation (`O-09`)**: an existing `.env` is never overwritten, truncated, or modified, even under concurrent invocations (`os.link` → `EEXIST`).

---

## 5. Headless Feed Administration CLI & Backup OPML Snapshot (`E-11`)

### 5.1 Shared Transactional Service (`backend/src/smartrss/feeds/service.py`)

Extract the core feed management operations currently embedded in `backend/src/smartrss/api/routes/feeds.py` into `backend/src/smartrss/feeds/service.py` so the HTTP API and CLI share one audited implementation:

1. **Typed Domain Exceptions** (mapped cleanly to HTTP `422`/`409` in `api/routes/feeds.py` and exit codes `1`/`2`/`3` in `cli.py`):
   - `FeedValidationError(ValueError)` — invalid URL syntax, blocked SSRF target, or invalid OPML.
   - `FeedCategoryNotFoundError(ValueError)` — referenced `category_id` or `category_name` does not exist or is soft-deleted (`C8`).
   - `FeedAlreadySubscribedError(ValueError)` — active/paused/muted feed already owns `feed_url` (carries `existing_feed_id: uuid.UUID` and `existing_status: str`, `O-11`).
2. **`list_feeds_query(session, *, status_filter=None, health_state=None) -> list[Feed]`**
   - Shared ordering: `Feed.title.asc(), Feed.created_at.asc()`.
3. **`create_or_reactivate_feed(session, ssrf_policy, *, feed_url, title=None, category_id=None, category_name=None, create_category_if_missing=False, poll_interval_minutes=None, expiration_days_override=None, fetch_full_text=True, research_policy=None, curation_instructions="", fields_set=frozenset()) -> tuple[Feed, bool]`** (returns `(feed, reactivated)`):
   - Runs `validate_feed_url_syntax(cleaned)` + `ssrf_policy.validate_url(cleaned)` + `await ssrf_policy.resolve_and_validate(host, port)` **before** taking any DB row locks (`C1`, `E-10-F1`).
   - Acquires category lock **before** feed lock (`categories → feeds`, `C8`):
     - By `category_id`: `SELECT id FROM categories WHERE id = :id AND deleted_at IS NULL FOR SHARE` → missing raises `FeedCategoryNotFoundError`.
     - By `category_name` (CLI ergonomics): `SELECT * FROM categories WHERE deleted_at IS NULL ORDER BY id ASC FOR SHARE`, match case-insensitively (`lower(name)`). If absent and `create_category_if_missing=True`, insert inside a savepoint (`async with session.begin_nested():`) so a concurrent creator hitting `uq_categories_active_name` (`IntegrityError`, `O-05`) rolls back only the savepoint, re-selects the winning active category `WHERE LOWER(name) = LOWER(:name) AND deleted_at IS NULL FOR SHARE`, and proceeds cleanly without raising a traceback! Emits `SyncEvent("category", cat.id, "upsert")` only if this transaction created the row. If absent and `create_category_if_missing=False`, raises `FeedCategoryNotFoundError`.
   - Uses PostgreSQL `sa.func.now()` (`C5`), locks existing `Feed` `FOR UPDATE`, raises `FeedAlreadySubscribedError(existing.id, existing.status)` if `status != "unsubscribed"`, applies monotonic `_stamp_online_lww` (`C7`), applies only `fields_set` on reactivation (`D3`), wraps new `Feed` `INSERT` + `flush()` in a savepoint (`session.begin_nested()`) so a concurrent same-URL subscribe race (`G-06`, `uq_feeds_feed_url`) rolls back only the savepoint, re-selects the winning row `FOR UPDATE`, and raises `FeedAlreadySubscribedError` (API `409`, CLI exit `1`/`--if-not-exists`) or reactivates cleanly instead of raising an unhandled `IntegrityError`, enqueues `poll_feed` (`priority=10, bump_existing=True`), and records `SyncEvent("feed", feed.id, "upsert")` via `sync_log.record()` (`D2`).
   - *Implementation deviation note (`G-03`)*: To support `session.begin_nested()` savepoints (`O-05` category race and `G-06` feed-URL race) under `strict_sync_guard=True`, `backend/src/smartrss/db/sync_log.py` was extended with savepoint snapshot tracking (`after_transaction_create`, nested `before_commit` skip-and-mark-committed, and `after_transaction_end` rollback restore vs. committed-savepoint retention for outer root `commit()` enforcement; also documented in [`1a-backend-ingestion-and-security.md`](1a-backend-ingestion-and-security.md) §4).
   - Caller owns `await session.commit()` (`C6`).
4. **`import_opml_subscriptions(session, ssrf_policy, *, opml_xml, default_category_id=None) -> OpmlImportSummary`**:
   - Moves the existing verified OPML import implementation verbatim from `api/routes/feeds.py:148-300` (keeping `parse_opml` 2 MiB / depth caps `S5`, active category `FOR SHARE` lock + `default_category_id` validation `C8`/`K-01-F1`, savepoint-guarded new-category `INSERT` on `IntegrityError` matching `O-05`, `validate_feed_url_syntax` + `validate_url_and_literal_ip` `E3`/`E-10-F1`, `_sanitize_site_url` `S4`, reactivation LWW stamps, `enqueue_job`, and `sync_log.record` `D2`).
   - Caller owns `await session.commit()` (`C6`).
5. **`export_opml_xml(session) -> str`**:
   - Moves the existing verified OPML export query (`Feed.status IN ('active','paused','muted')`, `OUTER JOIN` active categories, `ORDER BY Category.sort_order ASC NULLS FIRST, Feed.title ASC`) verbatim from `api/routes/feeds.py:314-337`.
   - **Fidelity contract (`O-04`, `Q-2` option (a))**: Standard OPML 2.0 portability snapshot containing subscriptions (`xmlUrl`, `htmlUrl`, `title`) and categories only. `pg_dump` is authoritative for feed `status` (`paused`/`muted`), `poll_interval_minutes`, `fetch_full_text`, `expiration_days_override`, `research_policy`, and `curation_instructions`. Re-importing into the *same* DB skips existing active/paused/muted feeds without altering their status; importing into a *fresh* DB creates all exported subscriptions as `active` with default settings.

### 5.2 CLI Commands (`smartrss feeds ...`)

Register a new `feeds_app = typer.Typer(help="Headless feed management commands", no_args_is_help=True)` on `app` in `backend/src/smartrss/cli.py`. **None** of these commands call `configure_logging()` so stdout remains clean for `--json` and XML redirection:

| Command | Arguments / Options | Output & Exit Codes |
| :--- | :--- | :--- |
| `smartrss feeds list` | `--status <active\|paused\|muted\|unsubscribed>`, `--health <healthy\|degraded\|broken\|gone>`, `--json` | Text table (`id  status  health  title  url`) or JSON array. Exit `0`. |
| `smartrss feeds add <url>` | `--title <str>`, `--category <name-or-uuid>`, `--create-category` (default **`False`**, opt-in per `O-05`), `--poll-interval <int >= 5>`, `--no-full-text`, `--if-not-exists`, `--json` | Prints created/reactivated feed (or JSON including `"id"`, `"feed_url"`, `"status"`, `"created"`, `"reactivated"`). Exit `0` on created/reactivated, or on already-`active` with `--if-not-exists`; exit **`3`** on already-`paused`/`muted` with `--if-not-exists` (printing `Already subscribed (status=paused): ...` and `"created": false, "status": "paused"` in `--json`, `O-11`); exit `1` on duplicate non-unsubscribed feed without `--if-not-exists`; exit `2` on validation/SSRF/category error (hinting `--create-category` when category name is missing, `O-05`). |
| `smartrss feeds import-opml <source>` | `<source>`: file path or `-` (stdin); `--default-category <uuid>`; `--json` | Reads at most `MAX_OPML_BYTES + 1` bytes (`2,097,153` bytes, `S5`) before UTF-8 decode (`UnicodeDecodeError` → exit `2`). Prints counts (`imported`, `reactivated`, `skipped_duplicates`, `rejected_invalid`) or JSON. Exit `0` on success, `2` on parse/size/category error. |
| `smartrss feeds export-opml` | `--output <path-or-dash>` (default `-` = stdout) | Writes valid UTF-8 OPML 2.0 XML to stdout (`-`) or atomically (`<path>.tmp` mode `0600` → `os.replace`) to a file path. Exit `0` on success. |

Notes:
- Category option parsing on `smartrss feeds add --category <val>`: try `uuid.UUID(val)` first → treat as `category_id`; otherwise treat as `category_name` (and pass `create_category_if_missing=create_category`).
- All CLI write commands build the engine/sessionmaker with `strict_sync_guard=cfg.synclog_strict_guard` so the `SyncLogWriter` guard (`D2`) is enforced identically to the API and worker.

### 5.3 Backup OPML Snapshot (`scripts/backup.sh`, `O-03`, `O-04`)

Extend `scripts/backup.sh` without breaking any existing `.dump` behavior or `restore-drill.sh` (which strictly selects `smartrss-*.dump`):

1. **Snapshot path & permissions**:
   - Same UTC `$TIMESTAMP` as the dump:
     - `DUMP_FILE="${BACKUP_DIR}/smartrss-${TIMESTAMP}.dump"`
     - `OPML_FILE="${BACKUP_DIR}/smartrss-feeds-${TIMESTAMP}.opml"`
     - `TMP_OPML="${OPML_FILE}.tmp"`
   - `cleanup_tmp()` trap removes both `"$TMP_FILE"` and `"$TMP_OPML"`.
2. **Ordered execution (`O-03` — OPML failure NEVER blocks off-host `rsync` of a verified dump)**:
   - **Step 1 (Fatal on error)**: Run `pg_dump` → verify with `pg_restore --list` → atomic `mv "$TMP_FILE" "$DUMP_FILE"` + `chmod 600 "$DUMP_FILE"`.
   - **Step 2 (Best-effort within run; deferred exit `1`)**: Initialize `OPML_FAILED=0`. Because `backup.sh` runs under `set -euo pipefail`, wrap the export and verification commands in `if ! ...; then ... fi` so a failure sets `OPML_FAILED=1` instead of triggering `set -e` before `rsync` (`O-03`):
     - `BACKUP_USE_LOCAL_CLI=1`: `uv run --directory backend smartrss feeds export-opml > "$TMP_OPML"`
     - Else if `docker compose ps --status running --services | grep -qx "$API_SERVICE"`: `docker compose exec -T "$API_SERVICE" smartrss feeds export-opml > "$TMP_OPML"`
     - Else: `docker compose run --rm -T --no-deps "$API_SERVICE" smartrss feeds export-opml > "$TMP_OPML"`
     - Verify non-empty (`-s "$TMP_OPML"`) and contains `<opml` and `</opml>` (zero XML parser on host, `S5`).
     - On success: `mv "$TMP_OPML" "$OPML_FILE"` + `chmod 600 "$OPML_FILE"`.
     - On failure: `rm -f "$TMP_OPML"`, print clear error to stderr, set `OPML_FAILED=1`, and **continue to Steps 3–4**.
   - **Step 3 (Independent local pruning, `O-03`)**: Prune `smartrss-*.dump` and `smartrss-feeds-*.opml` older than `$LOCAL_RETENTION_DAYS` (default `14`) in two independent `find` calls.
   - **Step 4 (Off-host `rsync`, always runs if dump succeeded)**:
     - Require non-empty `smartrss-*.dump` list (existing check). Collect any non-empty `smartrss-feeds-*.opml` files separately and pass `("${local_dumps[@]}" "${local_opmls[@]}")` to `rsync -az` (still **never** `--delete`).
     - After `rsync` succeeds, prune remote `smartrss-*.dump` and `smartrss-feeds-*.opml` older than `$BACKUP_REMOTE_RETENTION_DAYS` (default `30`) independently.
   - **Step 5 (Deferred non-zero alert)**: If `OPML_FAILED -ne 0`, print `[backup] ERROR: Database dump completed (and synced if configured), but OPML snapshot export failed.` to stderr and `exit 1`.

---

## 6. Locked & Fenced `SyncEngine.forceFullResync()` (`1c` Carry-Forward)

### 6.1 Problem Recap (`1c` Implementation Review)

Currently in `frontend/src/sync/engine.ts:468-480`, `forceFullResync()` clears `bootstrap_meta` / `bootstrap_items` and writes `sync_cursor: null` directly to `client_meta['sync']` **without acquiring `withSyncLock` or fencing**. If a background `runPullLoop` (in the same tab or another tab) is mid-request when the user triggers `forceFullResync()`, the pull's commit overwrites `sync_cursor: null` with `next_seq`, silently losing the forced resync.

### 6.2 Durable Request Marker & Fenced Lock Protocol (`O-02`, `O-08`)

1. **Dedicated `client_meta` key (`frontend/src/db/clientMeta.ts`)**:
   - Store resync requests under a separate key `"full_resync_requested"` (never inside `client_meta['sync']`, which `setSyncMeta()` writes at `clientMeta.ts:38`, `O-08`):
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
   - Because `setSyncMeta()` writes only `key: "sync"`, an in-flight pull on any tab can **never** overwrite `"full_resync_requested"`.
   - `clearAllLocalData()` already clears `db.client_meta`, so unpairing/re-pairing automatically clears any stale request.

2. **`BroadcastChannel` messages (`frontend/src/sync/engine.ts`, `O-02`)**:
   - Extend `BroadcastMsg` union with `{ type: "full_resync_requested"; senderId: string }`.
   - On receiving `full_resync_requested` from another tab:
     - Set `this.dirty = true`; if `!this.running`, call `void this.triggerSync()`.
   - Update the existing `sync_complete` handler (`engine.ts:216-224`, `O-02`):
     ```ts
     } else if (msg.type === "sync_complete") {
       if (this.recheckOnSyncComplete) {
         this.recheckOnSyncComplete = false;
         void Promise.all([this.db.outbox.count(), getFullResyncRequest(this.db)]).then(
           ([cnt, resyncReq]) => {
             if (cnt > 0 || resyncReq !== null) {
               void this.triggerSync();
             }
           },
         );
       }
     }
     ```
     This ensures a waiting tab under the IDB-lease fallback immediately triggers a cycle on `sync_complete` even when the outbox is empty (`O-02`).

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
   - `forceFullResync()` **never** writes `client_meta['sync']`, `bootstrap_meta`, or `bootstrap_items` outside the lock.

4. **Fenced consumption inside `executeSingleCycle(lock)`**:
   - Runs **only** while holding `withSyncLock` (`web_locks` or `idb_lease`).
   - Placed immediately **after** the reachability probe (`engine.ts:587-603`) and **before** checking `syncMeta.sync_cursor === null`:
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
           // Dexie serializes rw transactions on client_meta: any request written before
           // this tx starts is satisfied by this reset; any request written after commits (G-05).
           await clearFullResyncRequest(this.db);
         },
       );
     }
     ```
   - Immediately afterward, `const syncMeta = await getSyncMeta(this.db)` sees `syncMeta.sync_cursor === null` and runs `runStreamingBootstrap(api, lock)` — which preserves all `items_base`/`items` and pending outbox until Phase 2 atomic swap.

5. **Drain-loop check & post-release holder follow-up in `tryRunLockedCycle` (`O-02`, `O-02-F1`)**:
   - Track `let lastCycleProgressed = false;` across the drain loop in `tryRunLockedCycle`:
     ```ts
     let loops = 0;
     let lastCycleProgressed = false;
     while (loops < MAX_DRAIN_LOOPS) {
       this.dirty = false;
       const progressed = await this.executeSingleCycle(lock);
       lastCycleProgressed = progressed;
       loops += 1;
       if (!progressed) {
         break;
       }
       const remaining = await this.db.outbox.count();
       const pendingResync = (await getFullResyncRequest(this.db)) !== null;
       if (!this.dirty && remaining === 0 && !pendingResync) {
         break;
       }
     }
     ```
   - **Post-release check in the holder tab (`O-02`, `O-02-F1`)**: after `withSyncLock(...)` returns `res.acquired === true` and broadcasts `sync_complete`, reschedule **only** when `lastCycleProgressed === true` and `(await getFullResyncRequest(this.db)) !== null`:
     ```ts
     if (res.acquired) {
       this.postBroadcast({ type: "sync_complete", senderId: this.tabId });
       if (lastCycleProgressed && (await getFullResyncRequest(this.db)) !== null) {
         this.scheduleDebouncedSync(0);
       }
     }
     ```
     - When `executeSingleCycle()` exited early with `progressed === false` (because the server is `detached`, `misconfigured`, `auth_revoked`, or in `sync_error`), `lastCycleProgressed` is `false`, so the post-release check does **not** spin-reschedule — recovery stays bounded to the existing 60 s poll timer and `online` / `visibilitychange` triggers (`O-02-F1`).
     - When `lastCycleProgressed === true` (either `MAX_DRAIN_LOOPS` was reached after progressing cycles or a new request arrived between the final drain check and lock release), the holder immediately schedules a follow-up cycle via `scheduleDebouncedSync(0)` (never setting `recheckOnSyncComplete` on the holder tab, which ignores its own broadcast).

### 6.3 Interleaving & Failure-Mode Matrix (`O-02`, `O-02-F1`, `O-08`)

| Scenario | Behavior |
| :--- | :--- |
| **Idle tab calls `forceFullResync()`** | Persists `full_resync_requested` → `triggerSync()` acquires `withSyncLock` → fenced tx clears `bootstrap_*`, sets `sync_cursor: null`, deletes `full_resync_requested` → runs `runStreamingBootstrap` → flush outbox → pull. |
| **Same tab mid-`sync/pull` calls `forceFullResync()`** | Persists `full_resync_requested`, sets `this.dirty = true`, `triggerSync()` returns via `if (this.running)`. In-flight pull commits `next_seq` cleanly (does not touch `full_resync_requested`). Drain loop sees `dirty \|\| pendingResync`, runs next iteration, consumes `full_resync_requested` in fenced tx, executes full bootstrap. If `MAX_DRAIN_LOOPS` is reached after progressing cycles, `lastCycleProgressed === true` causes the post-release check to call `scheduleDebouncedSync(0)` and run the resync immediately. |
| **Tab B holds lock mid-`sync/pull`; Tab A calls `forceFullResync()` (Web Locks or IDB lease + `BroadcastChannel`)** | Tab A persists `full_resync_requested`, broadcasts `full_resync_requested`, enters deduplicated waiter (`waiterPending` under Web Locks / `recheckOnSyncComplete` under IDB lease). Either Tab B's drain loop sees `pendingResync` before releasing, or upon Tab B's `sync_complete` broadcast Tab A's `sync_complete` handler sees `resyncReq !== null` (even with an empty outbox, `O-02`) and immediately calls `triggerSync()` to execute the fenced reset + bootstrap. |
| **Tab B holds lock; `BroadcastChannel` unavailable (IDB lease)** | If `full_resync_requested` is written before Tab B's last drain check, Tab B executes it before releasing; if written between a progressing cycle's final drain check and lock release, Tab B's post-release check (`lastCycleProgressed === true`) schedules `scheduleDebouncedSync(0)`; if Tab B ended non-progressing (`detached`/`misconfigured`) or closed, the durable marker is picked up on the next 60 s poll tick (or `online`/`visibilitychange`) in a visible tab — never lost and never spinning. |
| **IDB lease stolen before fenced reset commits** | `lock.assertAndRenewInTx()` throws `SyncLockLostError`, aborting the Dexie `rw` tx atomically. `sync_cursor`, `bootstrap_*`, and `full_resync_requested` remain untouched so the new lock holder executes the resync. |
| **Already `detached` or `misconfigured` when `forceFullResync()` clicked (`O-08`, `O-02-F1`)** | Reachability probe runs because snapshot is `detached`/`misconfigured` and returns `progressed === false` before the fenced reset runs, so `sync_cursor` stays intact while offline, `full_resync_requested` remains queued, and `lastCycleProgressed === false` prevents post-release spin; probes stay bounded (`<= 1` per 60 s poll tick) until reconnect, when the first online cycle automatically executes full bootstrap. *(If the snapshot still reads `online` when the network silently dropped, the probe is skipped, `sync_cursor` is set to `null`, bootstrap fails with `progressed === false` and transitions to `detached` while keeping all `items_base`/`items` intact, and the next online cycle completes bootstrap.)* |
| **Second `forceFullResync()` arrives concurrently (`G-05`)** | A request written *before* the fenced `rw` transaction starts is satisfied by that reset; a request written *after* it commits (e.g., during the subsequent `runStreamingBootstrap` fetch) persists in `client_meta['full_resync_requested']` and triggers a follow-up full bootstrap because Dexie serializes `rw` transactions on `client_meta`. |
| **Origin confirm / auth revoked / unpaired** | `forceFullResync()` returns early without persisting a request or triggering sync. |

---

## 7. Security & Review-Checklist Self-Check

Walked against [docs/review-checklist.md](../review-checklist.md):

| Check | How `1d-ops` Satisfies It |
| :--- | :--- |
| **S1 / S5** (Untrusted input & parser caps) | `smartrss feeds import-opml` reads at most `2 MiB + 1` bytes before UTF-8 decode and passes text through existing `defusedxml`-backed `parse_opml()` (`S5`). `scripts/backup.sh` never invokes an XML parser on the host. |
| **S2 / S4** (SSRF & scheme validation) | `smartrss.feeds.service` uses `validate_feed_url_syntax` + `SSRFPolicy` (`validate_url` + DNS on `add`; `validate_url_and_literal_ip` on bulk `import-opml` per `E3`) and `_sanitize_site_url` (`S4`). |
| **S3** (Secrets in argv/logs) | `scripts/_render_env.py` generates `POSTGRES_PASSWORD` in-process and creates `.env` atomically at mode `0600` via `O_EXCL` + `os.link`; password never appears in argv, shell env, or stdout. Existing `.env` inspection never prints secrets or `source`s `.env`. |
| **C1 / C5 / C6 / C7 / C8** (Concurrency & DB hygiene) | `create_or_reactivate_feed` resolves DNS before DB locks (`C1`), uses PostgreSQL `sa.func.now()` (`C5`), lets callers commit (`C6`), uses monotonic `_stamp_online_lww` (`C7`), locks active categories `FOR SHARE` (`ORDER BY id ASC`) strictly before locking `Feed` `FOR UPDATE` (`C8`), and guards concurrent category creation on `uq_categories_active_name` with a savepoint (`begin_nested()`, `O-05`). |
| **D2 / D3** (Sync log & reactivation defaults) | Shared `feeds.service` enforces `SyncLogWriter` (`strict_sync_guard=True` in CLI too) (`D2`) and applies only `fields_set` on feed reactivation (`D3`). |
| **B5 / B6 / T6** (Client locking, error survival & forced-overlap concurrency tests) | `forceFullResync()` never bypasses `withSyncLock` or re-enters an active tab cycle (`B5`), runs inside `tryRunLockedCycle`'s error-catching loop (`B6`), and is tested with deterministic barrier/hook interleavings on both Web Locks and IDB lease fallback (`T6`), including empty-outbox `sync_complete` and `MAX_DRAIN_LOOPS` (`O-02`). |
| **T3** (Critical coverage gate) | `src/smartrss/feeds/service.py` is added to `CRITICAL_MODULES` (`>= 90%`) in `scripts/check-critical-coverage.py` in the same commit as the implementation. |

---

## 8. Verification & Test Plan

Every row below maps to named automated tests added in the implementation commit and must pass under `make check`:

| Test ID | Layer | Description & Invariants Verified |
| :--- | :--- | :--- |
| **`1d-ops-T01`** | Unit (`pytest`) | `scripts/_render_env.py` `generate`: creates mode-`0600` `.env`, identical generated URL-safe `POSTGRES_PASSWORD` in `POSTGRES_PASSWORD` and `DATABASE_URL`, deduplicated `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS` (never `*`), primary LAN host selection (first `--lan-host`, else detected IPv4, optional `.local` only when `getent` resolves, `O-07`), `COMPOSE_PROFILES=daemon` vs empty for `cron`, valid `SSRF_BLOCKED_CIDRS`, rejects invalid CIDRs/hosts/ports, and atomically refuses to overwrite an existing `.env` via `os.link` even when the target is created between the pre-check and link (`O-09`). |
| **`1d-ops-T02`** | Unit (`pytest`) | `scripts/_render_env.py` `inspect`: reads non-secret metadata from an existing `.env` without executing shell metacharacters, flags default placeholder password, and never prints `POSTGRES_PASSWORD` or `DATABASE_URL`. |
| **`1d-ops-T03`** | Unit (`pytest`) | `scripts/setup-server.sh` prerequisite failure paths: mocked `PATH` missing `docker`, missing `docker compose`, and failing `docker info` each exit `1` with actionable hints and never invoke `sudo`. |
| **`1d-ops-T04`** | Unit (`pytest`) | `scripts/setup-server.sh` fresh + idempotent re-run & compose shared image (`O-01`, `O-01-F1`, `O-06`, `O-07`, `O-10`): (a) compose-config test asserts `api` and `worker` both resolve to the identical `image: smartrss-backend:local` tag AND `worker` retains `build.context` (`./backend`) so `docker compose run worker` builds locally rather than pulling if the image is absent (`O-01-F1`); (b) mocked `docker`/`systemctl`/`loginctl`/`crontab` + local HTTP `/api/v1/health` (returning `{"status":"ok","db":"ok","version":"0.1.0"}`) verifies ordered startup (`build api web` → `up -d --wait db api` → `db migrate` → `up -d --wait`), `0600` `.env`, stdin OPML import, pairing code, primary host in printed `make install-client API_ORIGIN=...` + DHCP reservation note; (c) second run preserves `.env` byte-for-byte and still runs `docker compose build api web` in cron mode; (d) `--worker-mode cron --install-worker-timer` renders `smartrss-worker.{service,timer}` with shared lock path and `flock -n -E 75` / `SuccessExitStatus=75`, warns when mocked `loginctl` returns `no`, and warns when mocked `crontab -l` contains `smartrss worker run-once`. |
| **`1d-ops-T05`** | Unit (`pytest`) | `scripts/backup.sh` OPML snapshot, off-host `rsync` resilience & rotation (`O-03`): (a) creates verified mode-`0600` `smartrss-feeds-<ts>.opml` alongside `.dump` (via `BACKUP_USE_LOCAL_CLI=1` and via mocked `docker compose exec`/`run` fallback when `api` is stopped); (b) prunes stale `.opml` and `.dump` independently; (c) includes `.opml` in mocked `rsync` + remote prune without `--delete`; (d) when OPML export fails with `BACKUP_RSYNC_TARGET` set, asserts `rsync` is STILL called with the verified `.dump`, no `.opml` or `.opml.tmp` is left behind, and `backup.sh` exits `1`. |
| **`1d-ops-T06`** | Integration (Real PG) | `smartrss feeds add` + `list` (text & `--json`) & concurrent category race (`O-05`, `O-11`): (a) missing category name without `--create-category` exits `2` with `--create-category` hint; with `--create-category` creates category + emits category & feed `sync_changes` upserts; (b) forced-overlap real-PG concurrency test (`T6` barrier) with two sessions calling `create_or_reactivate_feed` for the same new category name → 1 category created, both feeds reference it, both succeed; (c) enforces `validate_feed_url_syntax` & SSRF block (exit `2`); (d) duplicate without `--if-not-exists` exits `1`; `--if-not-exists` on `active` feed exits `0` (`"created": false, "status": "active"`), and on `paused`/`muted` feed prints status, includes `"status"`/`"created": false` in JSON, and exits `3`; (e) reactivates unsubscribed feed preserving fields (`D3`) and enqueues `poll_feed`. |
| **`1d-ops-T07`** | Integration (Real PG) | `smartrss feeds export-opml` & `import-opml` round-trip & portability fidelity (`O-04`): exports pure XML to stdout (zero log pollution) and mode-`0600` `--output` file; re-importing onto the same DB yields `imported=0, skipped_duplicates=N, rejected_invalid=0` with categories and existing `muted`/`paused` statuses preserved; importing into a fresh DB restores all exported subscriptions and categories as `active` with default settings (matching documented portability contract); oversized `> 2 MiB` stdin/file exits `2` (`S5`); invalid/soft-deleted `--default-category` exits `2` (`C8`). |
| **`1d-ops-T08`** | Integration (Real PG) | API route parity regression: verifies existing `POST /api/v1/feeds`, `PATCH /api/v1/feeds/{id}`, `POST /api/v1/feeds/import-opml`, and `GET /api/v1/feeds/export-opml` tests remain green after delegating to `smartrss.feeds.service` (including `C8` soft-deleted category rejection and `E-10-F1` URL checks). |
| **`1d-ops-T09`** | Frontend Unit (`vitest`) | `forceFullResync()` same-tab race (forced overlap, `T6`): hook `sync/pull` fetch with a deferred barrier; call `forceFullResync()` while pull is in flight; release pull returning `next_seq = 50`; assert pull commits cleanly without corruption, drain loop then runs fenced reset + `sync/bootstrap`, and final `sync_cursor` reflects bootstrap watermark (NOT stuck at `50`). Proved to fail when `forceFullResync()` writes unlocked. |
| **`1d-ops-T10`** | Frontend Unit (`vitest`) | `forceFullResync()` cross-tab race & `MAX_DRAIN_LOOPS` under **IDB lease fallback** (`forceIdbLockFallback: true`, `O-02`): (a) Tab B holds lease on its last drain iteration (after the drain check) with an **empty outbox**; Tab A calls `forceFullResync()` and receives `sync_complete` → assert Tab A starts a cycle immediately without advancing fake timers and runs full bootstrap; (b) `MAX_DRAIN_LOOPS` exhaustion in the holder tab while a resync request is pending → assert holder's post-release follow-up runs the resync after releasing the lock. Both must fail against the original draft design. |
| **`1d-ops-T11`** | Frontend Unit (`vitest`) | `forceFullResync()` IDB lease-stolen fencing: simulate lease stolen before the fenced reset transaction commits; assert `SyncLockLostError` aborts the reset atomically and `full_resync_requested` remains persisted so the winning tab runs the resync. |
| **`1d-ops-T12`** | Frontend Unit (`vitest`) | `forceFullResync()` offline / no-spin / rapid double-click / `auth_revoked` guards (`O-08`, `O-02-F1`, `G-05`): (a) under fake timers when snapshot is **already `detached`** (and separately when `misconfigured`) before clicking `forceFullResync()`, `sync_cursor` stays intact, reachability probes stay bounded (`<= 1` per 60 s poll tick as the clock advances — zero spin loop!), and full bootstrap runs once a probe succeeds; (b) a second `forceFullResync()` arriving *during* `runStreamingBootstrap` (after the first fenced reset committed) persists and triggers a second bootstrap (`G-05`); (c) `unpaired`/`origin_confirm`/`auth_revoked` are no-ops. |
| **`1d-ops-T13`** | Unit (`pytest`) | `make install-client API_ORIGIN=...` compatibility check: verifies `API_ORIGIN` make variable forwards `--api-origin` cleanly alongside `INSTALL_CLIENT_ARGS`. |
| **`G-01`–`G-06` Regressions** | Unit / Integration / Vitest | `test_g01_…` (existing `.env` upfront host/CIDR validation, conflict warnings/exit `2`, cron unscheduled summary), `test_g02_…` (refuse fresh `POSTGRES_PASSWORD` over existing `pgdata` volume), `test_g03_…` (`db/sync_log.py` committed, recorded, rolled-back, and 2-level nested savepoints), `test_g04_…` (`T10 (c)` no-`BroadcastChannel` drain-loop `pendingResync` pickup in single lock hold), and `test_g06_…` (forced-overlap concurrent same-URL subscribe race raising `FeedAlreadySubscribedError`). |

### Critical-Module Coverage Gate (`make check`)
- **Backend**: Add `src/smartrss/feeds/service.py` to `CRITICAL_MODULES` (`>= 90%` branch coverage) in `scripts/check-critical-coverage.py`. Overall backend coverage remains `>= 80%`.
- **Frontend**: `src/sync/engine.ts` (`>= 90%` lines & branches) and `src/db/clientMeta.ts` (`>= 80%` lines & branches) stay enforced by `scripts/check-frontend-coverage.mjs`.

---

## 9. Documentation Sync Plan (Same Commit as Implementation)

When `1d-ops` is implemented:
- `README.md`: Add "Guided First-Time Setup (`make setup`)" section ahead of the advanced manual path, document `smartrss feeds add|list|import-opml|export-opml`, document `deploy/systemd/smartrss-worker.{service,timer}` (using the same shared `~/.local/state/smartrss/worker.lock` path as the crontab example, `O-06`), document updating `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS` in an existing `.env` when a server IP changes (`O-07`), document the OPML portability snapshot in `make backup`, show `make install-client API_ORIGIN=...`, and add a one-line upgrade note (`docker image rm smartrss-api smartrss-worker`) for cleaning up legacy pre-`1d-ops` image tags.
- `docs/design/1a-backend-ingestion-and-security.md`: Add `smartrss feeds ...` commands to §10.2 CLI table and note OPML portability snapshot in backups.
- `docs/design/1c-client-ui-and-offline-shell.md`: Mark the `forceFullResync()` carry-forward note in §11 as resolved in `1d-ops`.
- `docs/design/README.md`, `docs/README.md`, and `AGENTS.md`: Update `1d-ops` status through `Implemented` → `In Implementation Review` → (owner) `Implementation Approved`.
