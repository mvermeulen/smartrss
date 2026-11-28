# Implementation Review — Milestone `1d-ops` (Guided Setup, Headless Feed CLI, Backup OPML & Fenced `forceFullResync()`)

- **Date**: 2026-11-27
- **Scope**: `git diff main...milestone/1d-ops`. The base is `main` at `1229a77`, and the head is `ba5c53e` ("Implement milestone 1d-ops…"), which sits on top of the approved design at `d0abbfa`. That is 28 files, +4699 / −411.
  - *Process note:* `ba5c53e` was committed locally but **not pushed** when this review started (`milestone/1d-ops` was 1 commit ahead of `origin`).
- **Checked against**:
  - [`1d-ops-setup-and-cli.md`](../design/1d-ops-setup-and-cli.md) (design `Approved` 2026-11-27);
  - [`1d-ops` design review](2026-11-27-1d-ops-design-review.md) (`O-01`–`O-11` and the follow-ups);
  - `requirements.md` (`NFR-OPS-01`, `NFR-OPS-02`) and `1a` §§ (sync-log guards, backup, the CLI table);
  - `1c` §6 / §11, [`review-checklist.md`](../review-checklist.md) and `development-process.md` §2 / §7.
- **Reviewer**: Claude Code (Claude Opus 5.5)
- **Finding prefix**: `G` (`G-01`–`G-06`)
- **Status**: `Addressed (2026-11-27) — awaiting verification`

## Review outcome

The implementation follows the approved design closely and the tests are strong. Every row of the `1d-ops-T01`–`T13` test matrix maps to a named test.

**What's solid:**

- **Refactor parity.** `feeds/service.py` reproduces the old `POST /feeds` / OPML import and export logic line for line (compared against `main:api/routes/feeds.py`). The routes now map typed domain errors to `422` / `409`.
- **Category race.** The `O-05` savepoint (`service.py:150-175`) works. The `T6` concurrency test is genuinely forced: with the `IntegrityError` handler removed, it fails 3 of 3 runs.
- **`backup.sh` ordering.** The `O-03` ordering is correct. Calling the export inside `if` suspends `set -e` for that call, and the explicit `|| return 1` lines propagate failures, so `rsync` still runs after an OPML failure.
- **`.env` creation.** The `os.link` no-overwrite rule (`O-09`) holds.
- **Setup order.** Build → db + api → migrate → up (`O-01`, `O-10`), the linger warning and the shared lock file all match the design.
- **The fenced resync** in `engine.ts` matches §6.2. Of the 18 per-path reverts I ran, 16 fail the cited test.

**Main risks:**

1. **Re-running setup ignores flags** (`G-01`, *reproduced*). With an existing `.env`, `setup-server.sh` never validates `--ssrf-blocked-cidrs` or `--lan-host`, and silently ignores `--worker-mode cron --install-worker-timer`. It exits `0` with no timer installed and no warning.
2. **A new password over an old database** (`G-02`). If `.env` is missing but the `pgdata` volume isn't, setup generates a password the existing database will never accept. Setup then fails after 30 s with a generic health-check error.
3. **Undocumented, partly untested change to a critical module** (`G-03`). The new savepoint tracking in `db/sync_log.py` isn't in any design doc, and the committed-savepoint branch is untested: reverting it leaves all 214 tests green.

| Severity | Count | IDs |
| :--- | :--- | :--- |
| Blocker | 0 | — |
| Major | 0 | — |
| Minor | 4 | `G-01`–`G-04` |
| Nit | 1 | `G-05` |
| Pre-existing | 1 | `G-06` |

**Recommendation:** fix `G-01`–`G-04` before `Implementation Approved`. None of them is a data-loss or security issue, but `G-01` and `G-02` are exactly the first-run and re-run paths `NFR-OPS-02` exists to make reliable, and `G-03` concerns the sync-log guard. `G-05` and `G-06` can be accepted or fixed alongside.

---

## 1. Minor

### G-01 — Re-running setup with an existing `.env` silently ignores or skips validating `--worker-mode`, `--install-worker-timer`, `--lan-host`, `--lan-port` and `--ssrf-blocked-cidrs` *(verified by reproduction)*

**Where:** `scripts/setup-server.sh:114-137` (fail-fast validation covers only `--worker-mode` syntax, `--lan-port`, `--device-platform` and `--opml`), `:198-199` (an existing `.env` → `inspect_existing_env`, which never passes the flags to `_render_env.py`), and `:177`/`:184` (`EFFECTIVE_WORKER_MODE` is overwritten from `.env`); design §4.2 step 1 ("validate … `--ssrf-blocked-cidrs` … before touching disk or Docker").

**Problem:**

- Host tokens and CIDRs are validated only inside `_render_env.py generate`, which runs only when `.env` is absent.
- On a re-run, any flag that would have shaped the `.env` is dropped without a message.
- `--worker-mode cron --install-worker-timer` is ignored whenever the preserved `.env` says `daemon`.
- In cron mode with `--non-interactive` and no `--install-worker-timer`, the summary never says that nothing is scheduling the worker.

**Reproduction** (stub `docker` / `systemctl` / `loginctl` on `PATH`, a fake health endpoint, and a pre-existing `.env` with `WORKER_MODE=daemon`):

```
$ setup-server.sh --non-interactive --env-file $T/env --worker-mode cron --install-worker-timer \
    --ssrf-blocked-cidrs "not-a-cidr" --lan-host "http://bad host:9" --skip-pair
setup exit=0
[setup] Preserved existing …/env (edit it manually to reconfigure).
docker calls: compose version, info, compose build api web, compose up -d --wait db api,
              compose exec -T api smartrss db migrate, compose up -d --wait
systemd units installed: (none)
```

**Failure scenario:** the owner already has a daemon-mode `.env` from the user test. They decide to switch to the timer and run `make setup SETUP_ARGS="--worker-mode cron --install-worker-timer"`. Setup reports success, but the worker stays in daemon mode and no timer is installed. The owner believes they're on the timer.

**Fix:**

1. Validate every host token and CIDR in step 1 for all runs, for example with a `_render_env.py validate` subcommand, and exit `2` on invalid input.
2. When `.env` exists, compare the explicitly passed `--worker-mode`, `--lan-port`, `--lan-host` and `--ssrf-blocked-cidrs` with the inspected values. Print a prominent `WARNING: --worker-mode cron ignored: preserved .env has WORKER_MODE=daemon (edit .env to change)` for each conflict, or exit `2` under `--non-interactive`. The no-mutation rule stays intact.
3. In cron mode, print a summary line when no scheduler is installed and no SmartRSS crontab line was found.

**Regression tests:** extend `T04` with an existing `.env` combined with (a) invalid CIDR and host flags, asserting exit `2` before any `docker` call; (b) `--worker-mode cron --install-worker-timer` against a daemon `.env`, asserting the warning or exit; and (c) cron mode with no timer and no crontab, asserting the "worker not scheduled" line.

### G-02 — Setup generates a fresh `POSTGRES_PASSWORD` even when an existing `pgdata` volume was initialized with a different one *(found by code reading; Postgres image semantics)*

**Where:** `scripts/setup-server.sh:198-246` (an absent `.env` → generate with `secrets.token_urlsafe(32)`); `docker-compose.yml:14,18` (`POSTGRES_PASSWORD` plus the named volume `pgdata`). The official `postgres` / `pgvector` image reads `POSTGRES_PASSWORD` **only when the data directory is empty** (first `initdb`).

**Failure scenario:**

1. The owner deletes or moves `.env`, for example to "start over", or re-clones into a directory with the same name, so the Compose project name and volume (`smartrss_pgdata`) stay the same.
2. They run `make setup`.
3. A new password is written to `.env` and `DATABASE_URL`. The existing database still has the old one.
4. `api` fails authentication. `up --wait db api` either times out or `db migrate` fails, and the operator sees a generic error or the 30 s health failure. The tempting "fix" is `docker volume rm`, which destroys all data.

This contradicts the `NFR-OPS-02` promise that re-running is safe and preserves data.

**Fix:** before calling `generate`, check for an existing database volume: `docker volume inspect "${COMPOSE_PROJECT_NAME:-$(basename "$REPO_ROOT")}_pgdata"`. Better still, derive the volume name from `docker compose config --volumes` plus the project name. If it exists, refuse with exit `1`, and explain the choices: restore the previous `.env` (or its `POSTGRES_PASSWORD`), restore from `backups/`, or, as an explicit destructive choice, remove the volume. Don't generate a password the database will reject.

**Regression test:** in `T04`, with no `.env` and a stub `docker volume inspect` that reports an existing volume, assert exit `1`, assert that no `.env` was created and that `compose build` / `up` were never called, and assert the guidance text.

### G-03 — The `db/sync_log.py` savepoint tracking is undocumented, and its committed-savepoint branch is untested *(verified by revert)*

**Where:** `backend/src/smartrss/db/sync_log.py`:
- `:271-275`: `_before_commit` returns early for a nested transaction and marks the stack entry committed;
- `:291-303`: `_after_transaction_create` pushes a snapshot;
- `:319-329`: `_after_transaction_end` restores the snapshot on a savepoint rollback;
- `:370-371` and `:392`: listener install and removal.

`sync_log.py` is a `1a` critical module, and the change appears in no design doc: `grep -n savepoint` finds nothing in `1a`, `1b` or `1d-ops` §5.1.

**Problem:**

- **Docs.** The change is needed: without it, the strict guard would charge an `IntegrityError`-rolled-back category insert against the outer commit. I confirmed SQLAlchemy 2.1.9 dispatches `before_commit` for savepoint commits (`orm/session.py:1373-1374`), which this design relies on. But it changes the guard's semantics for every nested transaction in the codebase, and the design must record that (spec-first, §4.2 docs-in-the-same-change).
- **The rollback branch is tested.** Forcing it off (`if not committed:` → `if False:`) fails `test_1d_ops_t06…`.
- **The committed branch isn't.** Forcing every savepoint to be treated as rolled back (`if not committed:` → `if True:`) leaves **all 214 tests green**. In that broken state, requirements collected inside a *successfully released* savepoint are discarded. The strict guard would then silently accept an unlogged synced write made inside a savepoint, which is the exact class of sync gap the guard exists to catch (`D2`).
- Coverage also shows the partial branches `273->275` (an empty stack in nested `before_commit`) and `321->exit` (a stack-top mismatch).

**Fix:** document the savepoint semantics in `1a` (the sync-log guard section) and in `1d-ops` §5.1, with a one-line deviation note. Add real-Postgres unit tests in `tests/integration/test_sync_log*` for:

- (a) a synced ORM write inside a **released** savepoint with no `sync_log.record` → the outer commit raises the strict-guard error;
- (b) the same write recorded *inside* the savepoint → the commit succeeds;
- (c) a write inside a **rolled-back** savepoint → the requirement is discarded and the commit succeeds;
- (d) nested savepoints (two levels), covering the stack-top handling.

(a) must fail with `if not committed:` forced to `True`.

### G-04 — The `engine.ts` drain-loop `pendingResync` exit condition is untested *(verified by revert)*

**Where:** `frontend/src/sync/engine.ts` (in `tryRunLockedCycle`: `if (!this.dirty && remaining === 0 && !pendingResync) break;`); design §6.2 step 5 and §6.3 "Tab B holds lock; `BroadcastChannel` unavailable" ("If `full_resync_requested` is written before Tab B's last drain check, Tab B executes it before releasing").

**Problem:** removing `&& !pendingResync` leaves the targeted `T09` / `T10` tests and the **full frontend suite (61 tests)** green.

- In `T09`, `forceFullResync()` in the same tab sets `this.dirty = true`, so the loop continues on `dirty` alone.
- In `T10`, the post-release reschedule covers the case.

The no-`BroadcastChannel` path where the holder picks up the request *inside* its drain loop is not exercised. `engine.ts` is a critical frontend module (≥ 90%), and the design names this guard as part of the protocol.

**Fix (tests only):** add a `T10` (c) case. Tab B holds the IDB lease with `BroadcastChannel` disabled, and Tab A writes `full_resync_requested` while B's cycle is in flight, before B's drain check. Assert that B runs the fenced reset and bootstrap **within the same lock hold**: one `withSyncLock` acquisition, and no `scheduleDebouncedSync` call. This must fail with `&& !pendingResync` removed.

---

## 2. Nit

### G-05 — The `request_id` guard in the fenced reset is redundant, and `T12` (b) can't fail

**Where:** `engine.ts`, the fenced transaction in `executeSingleCycle` (`if (latestReq?.request_id === resyncReq.request_id) clear…`); design §6.3 "Second `forceFullResync()` arrives while first reset is queued"; `T12` (b).

**Problem:** removing the guard (clearing any request) leaves the full suite green, and on analysis it can't lose a resync:

- A request written **before** the fenced `rw` transaction starts is covered by the reset that transaction performs.
- A request written **after** it commits is never cleared.
- Dexie serializes `rw` transactions on `client_meta`.

What the guard actually does is keep the newer request, which costs a **second, redundant** full bootstrap in the next cycle. So `T12` (b)'s "not swallowed" assertion holds either way.

**Fix:** either drop the guard and correct the §6.3 row ("a request written before the fenced reset is satisfied by it"), or keep it as defence-in-depth and say in §6.3 that it trades an extra bootstrap for simplicity. Adjust the `T12` (b) wording so it doesn't claim to test a loss it can't detect.

---

## 3. Pre-existing

### G-06 — Concurrent subscription of the same `feed_url` raises an unhandled `IntegrityError` (API `500`, CLI traceback)

**Where:** `feeds/service.py:279-352`, which locks the existing row `FOR UPDATE` but **inserts** when no row exists, with no savepoint and no `IntegrityError` handling around the `feeds.feed_url` `UNIQUE` (`data-model.md` `feeds.feed_url`). The same pattern was in `main:api/routes/feeds.py` before the refactor, so this isn't new. `1d-ops` adds a second entry path (`smartrss feeds add`) that scripts may call in parallel.

**Failure scenario:** two `smartrss feeds add URL` calls (or a CLI call and a UI add) race. Neither sees a row, both insert, and the second commit raises `IntegrityError`. The CLI prints a Python traceback, not exit `1` / `--if-not-exists` semantics, and the API returns `500`, not `409`.

**Fix:** catch `IntegrityError` on the insert flush (inside a savepoint), re-select the row and raise `FeedAlreadySubscribedError` (or apply the reactivation path). Add a barrier-forced real-PG test for two concurrent `create_or_reactivate_feed` calls with the same URL: one creates, and the other gets `FeedAlreadySubscribedError`.

---

## 4. Verification performed

**`make check`** at `ba5c53e` (exit `0`):

- **Backend:** `ruff` and `mypy` are clean. **214 passed** (up from 205 on `main`), **92.95%** total coverage, and all 17 critical modules are ≥ 90%. `feeds/service.py` is at 97.89% (newly gated); `db/sync_log.py` is at 95.79%.
- **Client runner:** 32 passed, 94.87%.
- **Frontend:** `eslint`, `prettier` and `tsc` are clean. **61 passed**. `engine.ts` is at 96.89% lines / 90.44% branches, and `clientMeta.ts` at 100% / 87.80%. All gates pass.

**Per-path revert checks.** Each fixed path was disabled on its own with a minimal edit, the cited test run, and the file restored and confirmed byte for byte (`git status` clean afterwards):

| Path | Test | Result |
| :--- | :--- | :--- |
| `O-05` savepoint `IntegrityError` handler (`service.py:161`) | `test_1d_ops_t06…` | ✅ fails (3/3 runs, so the overlap is genuinely forced) |
| `sync_log` restore on savepoint rollback | `T06` / full suite | ✅ fails |
| `sync_log` **committed** savepoint keeps tracking | full suite | ❌ **stays green** → `G-03` |
| `O-11` exit `3` for `paused` / `muted` | `T06` | ✅ fails |
| `--create-category` opt-in | `T06` | ✅ fails |
| `O-09` `os.link` (vs. `os.replace`) | `T01` | ✅ fails |
| `O-03` no early exit on OPML failure; deferred `exit 1` | `T05` | ✅ fails (both) |
| `O-01` `build api web` before `up`; `O-10` `db api` before migrate | `T04` | ✅ fails (both) |
| `O-06` linger warning | `T04` | ✅ fails |
| Fenced consumption of the request | `T09` | ✅ fails |
| `sync_complete` handler `resyncReq !== null` | `T10` | ✅ fails |
| Post-release reschedule; `lastCycleProgressed` gate (`O-02-F1`) | `T10` / `T12` | ✅ fails (both) |
| `assertAndRenewInTx()` in the fenced transaction | `T11` | ✅ fails |
| Drain-loop `&& !pendingResync` | `T09` / `T10` / full frontend suite | ❌ **stays green** → `G-04` |
| `request_id` guard | `T12` / full frontend suite | ❌ stays green → `G-05` (redundant by analysis) |

**Reproductions:**

- `G-01`: stub-`PATH` run of `setup-server.sh` against a pre-existing `.env` (shown above). Exit `0`, no validation, no timer, no warning.
- `G-03`: SQLAlchemy 2.1.9 source confirms `before_commit` is dispatched for nested transactions (`orm/session.py:1373-1374`). The committed-branch revert stays green.

**Checklist passes walked:**

- **S1:** the OPML is read bounded to 2 MiB + 1 byte before decoding, then passes through the existing `defusedxml` `parse_opml`.
- **S3:** the password never appears in argv. `.env` is parsed, never `source`d, by `_render_env.py`. `backup.sh`'s existing `source .env` is unchanged and outside this scope.
- **C1:** DNS is resolved before any locks in `create_or_reactivate_feed`.
- **C8:** categories are locked before feeds.
- **C6:** callers commit.
- **D2:** the strict guard is used for CLI writes; see `G-03`.
- **B5 / B6:** there is no re-entrant cycle, and errors are caught in the drain loop.
- **T6:** see the race result above.
- **T3:** `feeds/service.py` was added to `CRITICAL_MODULES`.

**Docs:**

- The design header, `docs/design/README.md` item 4 and `AGENTS.md` all say `In Implementation Review`, so they agree.
- `1a` (the CLI table, backup steps 3a–5 and the critical-modules list) and `1c` §11 (carry-forward resolved) are updated.
- `README.md` is updated (+47 lines).
- **Missing:** the `sync_log.py` savepoint semantics (`G-03`).

---

## Resolutions (2026-11-27)

- **G-01** — Fixed. Added `validate` and `check-conflicts` subcommands to `scripts/_render_env.py` (`cmd_validate`, `cmd_check_conflicts`, and `allowed_hosts`/`ssrf_blocked_cidrs` metadata in `inspect_env_file`) and updated `scripts/setup-server.sh` to: (a) validate all `--lan-host`, `--lan-port`, `--worker-mode`, and `--ssrf-blocked-cidrs` flags upfront in Step 1 before any disk or Docker call on every run; (b) compare explicitly passed flags against preserved `.env` values in `inspect_existing_env`, printing prominent `WARNING`s for each conflict and exiting `2` under `--non-interactive` before building/starting containers; and (c) print an explicit `WARNING: Worker is NOT scheduled` summary line in cron mode when neither `smartrss-worker.timer` nor a SmartRSS crontab entry is configured. Synced `docs/design/1d-ops-setup-and-cli.md` §4.2.
  - Test: `test_g01_setup_existing_env_validates_flags_warns_conflicts_and_cron_summary` in `backend/tests/unit/test_1d_ops_setup_and_backup.py`.
  - Evidence (per-path revert proofs):
    - Disabled upfront `python3 "${val_args[@]}"` → failed (`assert r_bad_cidr.returncode == 2` got `1`); restored → passed.
    - Disabled `check-conflicts` conflict handling → failed on conflict assertions; restored → passed.
    - Disabled unscheduled cron summary warning → failed on `"Worker is NOT scheduled"` assertion; restored → passed.
- **G-02** — Fixed. Added `check_existing_pgdata_volume` in `scripts/setup-server.sh` before generating a new `.env`: derives the Compose `pgdata` volume name via `POSTGRES_PASSWORD=probe-only docker compose config --format json` (falling back to `${COMPOSE_PROJECT_NAME:-<repo-basename>}_pgdata`), runs `docker volume inspect`, and if the volume already exists refuses with exit `1` without creating `.env` or running `docker compose build`/`up`, printing recovery choices (restore `.env`, restore from `backups/`, or explicit `docker volume rm`). Synced `docs/design/1d-ops-setup-and-cli.md` §4.2.
  - Test: `test_g02_setup_refuses_new_password_over_existing_pgdata_volume` in `backend/tests/unit/test_1d_ops_setup_and_backup.py`.
  - Evidence: disabled `check_existing_pgdata_volume` → failed (`assert not env_file.exists()` got `True`, `.env` was erroneously created over the existing volume); restored → passed.
- **G-03** — Fixed. Documented the `db/sync_log.py` savepoint-aware strict guard semantics (`after_transaction_create` snapshot, nested `before_commit` skip-and-mark-committed, `after_transaction_end` retention on release vs. restore on rollback) in both `docs/design/1a-backend-ingestion-and-security.md` §4 and `docs/design/1d-ops-setup-and-cli.md` §5.1 (implementation deviation note). Added real-PostgreSQL integration tests covering (a) unlogged synced ORM write inside a released savepoint failing outer commit, (b) write recorded inside a released savepoint succeeding, (c) explicit and `IntegrityError` rolled-back savepoints discarding requirements, and (d) two-level nested savepoints in both directions.
  - Test: `test_g03_synclog_savepoint_committed_recorded_rolled_back_and_nested` in `backend/tests/integration/test_migrations_and_synclog.py`.
  - Evidence (per-path revert proofs):
    - Forced `if not committed:` → `if True:` (committed branch broken) → failed (`Failed: DID NOT RAISE MissingSyncEventError`); restored → passed.
    - Forced `if not committed:` → `if False:` (rollback branch broken) → failed (`MissingSyncEventError` raised on rolled-back category); restored → passed.
    - Fixed lines covered (`src/smartrss/db/sync_log.py` at **95.79%**).
- **G-04** — Fixed (test added). Added `test_g04_force_full_resync_cross_tab_no_broadcast_drain_loop_picks_up_in_same_lock_hold` (`T10 (c)`) in `frontend/src/test/unit/review_regressions.test.ts` disabling `BroadcastChannel`, writing `full_resync_requested` from Tab A while Tab B is mid-pull with empty outbox and `dirty=false`, and asserting Tab B runs the fenced reset and bootstrap within the exact same `withSyncLock` hold (identical `fencing_token`, `scheduleDebouncedSync` called `0` times).
  - Evidence: removed `&& !pendingResync` from `frontend/src/sync/engine.ts` → `test_g04_…` failed (`AssertionError: expected +0 to be 1`); restored → passed. Covered (`src/sync/engine.ts` at **96.88%** lines / **90.41%** branches).
- **G-05** — Fixed. Dropped the redundant `latestReq?.request_id === resyncReq.request_id` check in `frontend/src/sync/engine.ts` (`executeSingleCycle`) so the fenced Dexie `rw` transaction unconditionally calls `clearFullResyncRequest(this.db)` (Dexie serializes `rw` transactions on `client_meta`: requests written before the tx starts are satisfied by that reset; requests written after it commits persist). Synced `docs/design/1d-ops-setup-and-cli.md` §6.2, §6.3, and §8, and simplified `T12 (b)` in `frontend/src/test/unit/review_regressions.test.ts` to assert cleanly that a second `forceFullResync()` arriving *during* `runStreamingBootstrap` (after the first reset committed) triggers a second bootstrap (`doubleClickBootstraps === 2`).
- **G-06** — Fixed. Extracted `_reactivate_or_raise()` in `backend/src/smartrss/feeds/service.py` and wrapped the new `Feed` insert + `flush()` in `create_or_reactivate_feed()` inside a savepoint (`async with session.begin_nested():`), catching `IntegrityError`, re-selecting the winning `Feed` `FOR UPDATE`, and raising `FeedAlreadySubscribedError` (API `409`, CLI exit `1`/`--if-not-exists` semantics) or reactivating cleanly instead of leaking an unhandled `IntegrityError`. Synced `docs/design/1d-ops-setup-and-cli.md` §5.1.
  - Test: `test_g06_concurrent_same_url_subscribe_raises_already_subscribed` in `backend/tests/integration/test_1d_ops_feeds_cli.py` (forced-overlap barrier across two sessions subscribing the same new URL).
  - Evidence: disabled `except IntegrityError:` in `create_or_reactivate_feed` → failed with `IntegrityError` (`UniqueViolation: duplicate key value violates unique constraint "uq_feeds_feed_url"`); restored → passed. Fixed path covered (`src/smartrss/feeds/service.py` at **97.09%**).

### Verification (`make check` after `G-01`–`G-06` fixes)
- **Backend**: `ruff check` + `ruff format --check` (93 files + `scripts/_render_env.py`), `bash -n scripts/setup-server.sh scripts/backup.sh`, `mypy src` (56 files) — all clean. **218 passed**, **92.92%** total branch+line coverage (`>= 80%`). All 17 critical modules pass `>= 90%` (`src/smartrss/feeds/service.py` **97.09%**, `src/smartrss/db/sync_log.py` **95.79%**, lowest critical module `src/smartrss/worker/stages/poll_feed.py` **91.98%**).
- **Client runner**: **32 passed**, **94.87%** coverage (`>= 90%`).
- **Frontend**: `eslint` + `prettier --check` + `tsc --noEmit` clean; **62 passed** (`vitest`), overall **93.30%** lines / **89.21%** branches; critical gates (`>= 90%`): `src/sync/engine.ts` **96.88%** lines / **90.41%** branches (lowest critical branch: `src/security/sanitizeHtml.ts` **90.00%**); core gates (`>= 80%`): `src/db/clientMeta.ts` **100.00%** lines / **87.80%** branches.
