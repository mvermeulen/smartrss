# Code Review — Milestone `1a` Implementation

- **Date**: 2026-11-25
- **Scope**: Uncommitted working tree on `main` after `25d7bae`: `backend/` (application code, Alembic migration, tests), `deploy/`, `scripts/`, `docker-compose.yml`, `Makefile`, `.env*.example`, `README.md`, and the `AGENTS.md` status change. Checked against [`docs/design/1a-backend-ingestion-and-security.md`](../design/1a-backend-ingestion-and-security.md) (Approved), `data-model.md`, ADRs `0003`, `0004`, `0006`, and [`development-process.md`](../development-process.md).
- **Reviewer**: Claude Code (at the project owner's request)
- **Status**: Open. Findings `I-01`–`I-24` are not yet addressed.

## Review outcome

The implementation follows the approved design closely, and most of the hard parts are done correctly:

- **Sync log.** `SyncLogWriter` serializes on `sync_state … FOR UPDATE`. The ORM `before_commit` guard and the Core `before_cursor_execute` guard are both installed in tests.
- **Data handling.** `entry_hash` and `content_hash` are split as decided in `A-01`. Intra-batch GUID dedup and the guarded canonical-URL fallback (`N-01`) match §9.1. Writes to `feeds` are column-scoped and diff-gated.
- **Queue fencing.** The queue uses lease fencing on `(id, status, locked_by, attempts)` for heartbeat, complete, fail and release.
- **Outbound HTTP.** `SafeHttpClient` pins IPs at `connect_tcp()`. It decompresses with a cap on every `decompress()` call. It revalidates every redirect hop.
- **Extractor isolation.** The extractor subprocess uses a stripped env, `start_new_session`, rlimits and a process-group kill.

`make check` passes cleanly (details in §5).

However, there is one **security blocker** (`I-01`): `feedparser` treats an attacker-controlled response body as a local file path. Several **worker-robustness bugs** can also silently stop ingestion in daemon or cron mode (`I-02`, `I-03`), and one poller race overwrites a user edit (`I-04`). The ≥ 90% critical-module coverage gate from §12 is not met and not enforced (`I-08`). Docs were not updated alongside the code (`I-09`).

**Recommendation:** fix `I-01`–`I-04` with regression tests before committing `1a`. Fix `I-05`–`I-09` before declaring `1a` exit criteria met. The minor findings can be handled in the same pass or tracked.

| Severity | Findings |
| :--- | :--- |
| Blocker | `I-01` |
| Major | `I-02`, `I-03`, `I-04`, `I-05`, `I-06`, `I-07`, `I-08`, `I-09` |
| Minor | `I-10`–`I-19` |
| Nit | `I-20`–`I-24` |

---

## 1. Blocker

### I-01 — `feedparser.parse(body_bytes)` opens the response body as a local file path (arbitrary local file read and DoS)
`backend/src/smartrss/worker/stages/poll_feed.py:85` passes the raw `bytes` body straight to `feedparser.parse()`. `feedparser`'s `_open_resource()` first tries `open(url_file_stream_or_string, 'rb')`, and `open()` accepts `bytes` paths. If a hostile or compromised feed server returns a body that is a filesystem path, the worker reads that local file and parses it as the feed. The `feedparser` docstring itself warns: *"Wrap an untrusted string in `io.BytesIO`"*.

- **Verified in this review**: `feedparser.parse(b"/path/to/local.xml")` returned the local file's `<title>` and entries.
- **Impact**:
  - **DoS.** A body of `/dev/zero` or `/dev/urandom` reads without bound in the event-loop thread. The whole worker blocks: heartbeats stop, every lease expires, and memory grows to the 2 GB `mem_limit` and an OOM kill. Every retry repeats it.
  - **Data disclosure.** Any feed-shaped local file (XML/Atom under `/app`, `/usr`, …) gets ingested and synced to clients.
- This defeats the core assumption in design §7 that `feedparser.parse()` is "called only on bytes already fetched by `SafeHttpClient`".
- **Fix**: call `feedparser.parse(io.BytesIO(body_bytes), response_headers=…)` (see also `I-06`). Add a regression test that serves a body equal to a temp file path and asserts the poll ends as a parse error or zero entries, never the file's contents. Add a `ruff`/grep guard or a unit test that forbids `feedparser.parse(` on anything other than a `BytesIO`.

---

## 2. Major

### I-02 — A transient DB error permanently kills daemon I/O worker slots
`execute_claimed_job()` (`worker/runner.py:251-254`) awaits the heartbeat task in `finally` and suppresses only `CancelledError`. If any heartbeat `UPDATE` fails (for example, the DB restarts or a connection drops), the heartbeat task ends with that exception and `await hb_task` re-raises it. The same happens if `complete_job`/`fail_job` raises inside the `except Exception` handler.

In daemon mode, `_io_worker_loop` (`runner.py:562-581`) catches only `CancelledError` around `execute_claimed_job`, so the exception ends that slot's task for good. Nothing restarts it, and the process stays up because `run_worker_daemon` only awaits `stop`.

As a result, a single DB blip can leave a "healthy" `worker` container that schedules jobs but never executes them. `restart: unless-stopped` does not help because the process never exits. When the heartbeat dies, the lease also lapses, so the reaper requeues a job that is still running and the work is duplicated.
- **Fix**:
  - Make `_heartbeat_loop` catch and log `Exception` per iteration and keep retrying until the lease is lost.
  - In `_io_worker_loop`, wrap `execute_claimed_job` in `except Exception: logger.exception(...); await asyncio.sleep(backoff)` so the slot survives.
  - Optionally, have the supervisor exit non-zero if every I/O task has died, so Docker restarts the container.
  - Add a regression test that injects a failing heartbeat or completion session and asserts the slot keeps claiming jobs.

### I-03 — `worker run-once` spins until `--max-duration` when a running job from a killed previous run still holds a lease
`run_worker_once` runs the reaper once at startup (`runner.py:325`). It then treats any `status='running'` job of the requested types as work still in progress (`_count_ready_running_and_deferred`, `runner.py:268-280`). That count is not limited to jobs claimed by this invocation.

Suppose the previous `run-once` was killed while jobs were running: a cron `timeout`, OOM, or `docker compose run` stopped with `SIGTERM`, which `run-once` does not handle. If the next cron fires within the 120 s lease window, the startup reaper does not reclaim those jobs because their leases have not expired, and nothing reaps them later in the run. The worker loops then poll the DB every 50 ms per slot (`runner.py:369-378`) for the full default 30 minutes while holding `LOCK_RUN_ONCE`. The run ends `partial` (exit 2), and every cron in between exits `skipped_locked`.

This is the same failure class as `A-04`, which the design closed only for the `queued`/backoff case.
- **Fix**:
  - Count only `running` jobs whose `locked_by` starts with this invocation's `worker_id` prefix (or that carry this `run_id`). Report other in-flight jobs as deferred.
  - Optionally, rerun `reap_stale_leases` inside the idle wait.
  - Install a `SIGTERM` handler in `run-once` that cancels the worker tasks so `release_job_on_shutdown` runs.
  - Replace the 50 ms poll with an `asyncio.Event` set when a slot finishes a job.
  - Regression test: insert a `running` job with a future `lease_expires_at` and a foreign `locked_by`, then assert `run-once` returns promptly.

### I-04 — A poll that overlaps a `PATCH feed_url` writes the old URL's results onto the edited feed and can undo the user's edit
`run_poll_feed` reads `feed_url`/`etag` in the pre-check, fetches, then re-reads the row `FOR UPDATE` in the persist step (`poll_feed.py:468-518`). It never checks whether `feed_url` changed during the HTTP I/O. If the user edits `feed_url` while the poll is in flight:

1. **The user's URL is overwritten.** If the old URL answered with a permanent redirect, `effective_feed_url = new_feed_url` replaces the user's new URL (`poll_feed.py:498-518`, and the `304` path at `:323-344`). This breaks the invariant from §9.1 step 2 that a concurrent `PATCH` is never overwritten.
2. **The old feed's data is stored under the new URL.** Entries from the old URL are inserted for the new URL, and the old response's `etag`/`last_modified` are stored. Then `initial_backfill_done = TRUE` is set (`:743`), so the new URL's first real poll **skips the backfill cap** and inserts its whole archive as `unread`.
3. **The user's manual poll is dropped.** The `PATCH` handler's `enqueue_job(..., bump_existing=True)` is a no-op while the old job is `running` (`WHERE jobs.status = 'queued'`). The new URL is therefore not fetched until `next_fetch_at`, which the stale poll has just pushed out by a full interval.

The same pattern applies to `_record_feed_poll_error`: a failure on the old URL degrades the health of the newly edited feed.
- **Fix**:
  - Include `feed_url` in the persist and error-path re-reads. If it differs from `pre.feed_url`, write only bookkeeping (`last_fetched_at`), leave `etag`, `initial_backfill_done`, `next_fetch_at` and `feed_url` untouched, and re-enqueue `poll_feed` at priority 10.
  - Add an integration test that changes `feed_url` between the fetch and the persist step.

### I-05 — Large feeds exceed PostgreSQL's 65,535 bind-parameter limit and then fail on every poll
The new-item insert is one multi-row `INSERT … VALUES (…), (…)` with 24 bound columns per row (`poll_feed.py:693-698`). The PostgreSQL wire protocol caps a statement at 65,535 parameters, so any poll with more than ~2,730 new entries fails with a driver error. Full-archive podcast and blog feeds within the 5 MB cap can reach this. The error goes through the generic exception path on every poll, so the feed never ingests. It ends up `broken` while its job is dead-lettered and re-dispatched over and over.
- **Fix**: insert in chunks (for example 500 rows) inside the same transaction, collecting `RETURNING id` from each chunk. Add a test that inserts 3,000 entries in one poll.

### I-06 — The feed is parsed without HTTP context: relative entry links are dropped and the HTTP charset is ignored
`feedparser.parse(body_bytes)` gets no `response_headers` (`poll_feed.py:85`), which has two effects:
- **Relative links are lost.** Entry links like `/posts/42` cannot be resolved without a base URL (`Content-Location`). `canonicalize_url` then rejects them and the entries are **silently skipped** (`poll_feed.py:111-114`). A feed made of relative links ingests zero items but stays `healthy`.
- **The HTTP charset is ignored.** A charset given only in the `Content-Type` header is not used for decoding, so feedparser guesses (UTF-8, then windows-1252).

`extract_content._decode_html` (`extract_content.py:55-64`) has the same gap. It honors only the HTTP header charset and ignores `<meta charset>`, which causes mojibake for legacy-encoded pages. `trafilatura` can detect the encoding itself if it is given bytes.
- **Fix**:
  - Pass `response_headers={"content-location": fetch_result.final_url, "content-type": fetch_result.content_type or ""}` together with the `BytesIO` from `I-01`.
  - Log or count skipped entries so a feed with 0 parsed and N skipped entries shows up in Feed Health.
  - Pass raw bytes to the extractor, or sniff `<meta charset>`, when the header gives no charset.

### I-07 — Several §12.1 test-matrix cases are missing
These cases are promised in design §12.1 but have no test:
- **TLS/SNI pinning.** No boundary test covers HTTPS: connect to the pinned IP while SNI and certificate validation keep the hostname. The pinning test uses plain HTTP only.
- **Identity wire cap.** No test covers the 5 MB cap on an identity (uncompressed) body sent without `Content-Length`, so the streaming branch in `_read_bounded_body` that counts raw bytes is untested (`safe_http.py:338-344` is partly uncovered).
- **`smartrss settings set`.** No test checks that it emits an `app_settings` `upsert`. `cli.py` is at 38% coverage, and `update_app_setting` is never exercised.
- **Permanent extraction failures.** No test covers `extract_content` permanent outcomes (HTTP 403/404, SSRF-blocked, non-HTML `Content-Type`, `fetch_full_text = FALSE`). Lines `extract_content.py:134-157, 170-173` are uncovered.
- **Recorded fixtures.** The design calls for a `tests/fixtures/` directory of recorded RSS/Atom/OPML/hostile fixtures. It does not exist; fixtures are inline strings.
- **Property-based tests.** `hypothesis` is a dev dependency but no test uses it. URL canonicalization, the backoff math and the IP classifier are natural property-test targets (`development-process.md` §2).
- **Regression tests for this review.** Add tests for `I-01`–`I-05` as each is fixed.

### I-08 — The ≥ 90% critical-module coverage gate is neither met nor enforced
`make check` enforces only `--cov-fail-under=80` overall (84.1%). Design §12 and `development-process.md` require ≥ 90% on the critical modules. These currently fall short:

| Module | Coverage |
| :--- | :--- |
| `security/safe_http.py` | 84% |
| `db/sync_log.py` | 86% |
| `extract/client.py` | 88% |

`worker/runner.py` (70%) is not on the critical list but contains the `run-once`/daemon logic behind `I-02`/`I-03`, and its error and shutdown paths (`runner.py:224-250`, `:391-429`, `:570-618`) are untested. That gap is how `I-02` went unnoticed.
- **Fix**: add a per-module check, for example a small script over `coverage json`, or a separate `coverage report --include=<critical modules> --fail-under=90` step in `make test-cov`. Raise the three modules above 90%.

### I-09 — Docs were not updated in the same change
`AGENTS.md` says "Milestone `1a` implemented", but nothing else in `docs/` changed:
- `docs/README.md` and `docs/design/README.md` still list `1a` as design-approved only.
- The design doc has no implementation-status note.
- Where the code departs from the design, the design doc is not updated. Examples: `tests/fixtures/` is absent; `--stage maintenance` exists; `retention_cleanup` dispatch is gated on `app_settings`; and `SafeHttpClient` adds no auto-detected Docker bridge (only `172.17.0.0/16` is hard-coded, which works only because all RFC 1918 ranges are already blocked).

This violates the "Keep docs synchronized … in the **same commit**" rule.
- **Fix**: update `docs/README.md`, `docs/design/README.md` and the `1a` design header (implementation status), and reconcile those deviations in the design doc before committing. Correct the `.env.example` comment "Docker bridge subnet is auto-included" to match what the code does.

---

## 3. Minor

### I-10 — OPML import does live DNS resolution per feed inside the request transaction and silently drops feeds on transient DNS failure
`import_opml_endpoint` calls `_validate_feed_url` → `resolve_and_validate` for each outline (`feeds.py:135-139`). A transient DNS failure (`SSRFConnectionError`) counts the feed as `rejected_invalid` and drops it. A 200-feed OPML file resolves serially and can exceed Nginx's `proxy_read_timeout 60s` while holding a DB transaction, because categories are already flushed.
- **Fix**: at import time, check syntax and literal IPs only, and leave DNS/IP enforcement to `SafeHttpClient` at fetch time, where it is enforced anyway. Alternatively, resolve concurrently with a short timeout and report `unresolvable` separately from `rejected_invalid`.

### I-11 — OPML `htmlUrl` is stored as `site_url` without scheme validation
`feeds.py:183-184` and `:203` store `sub.site_url` verbatim, so `javascript:` and `data:` values are accepted, synced to clients and exported. The poller path validates `site_url` through `canonicalize_url`, but the import path does not. `1c` must sanitize anyway, but the server should not store and sync unsafe URLs in the first place.
- **Fix**: keep `site_url` only if it is `http(s)`; otherwise store `NULL`.

### I-12 — OPML parsing recursion is unbounded
`_walk` (`opml.py:44-64`) recurses once per nested `<outline>`. A 10 MB body can nest deeply enough to raise `RecursionError`, which `OpmlParseError` does not catch, so the request returns a 500. `defusedxml` does not cap depth.
- **Fix**: walk iteratively or cap the depth (for example 32) and raise `OpmlParseError` beyond it.

### I-13 — Reactivating a feed through `POST /feeds` resets `fetch_full_text` to the request default
`create_feed` always assigns `existing.fetch_full_text = body.fetch_full_text` and stamps it (`feeds.py:344-345`). `FeedCreateRequest.fetch_full_text` defaults to `True`, so re-subscribing silently re-enables full-text fetching that the user had turned off.
- **Fix**: apply it only when `"fetch_full_text" in body.model_fields_set`.

### I-14 — A manual poll during a running poll is silently dropped, but the API reports `queued: true`
`trigger_manual_poll` returns `queued=True` even when `enqueue_job` returns `None` because the existing job is `running` (`feeds.py:561-570`).
- **Fix**: return `queued=False` (or `"status": "already_running"`) when `job_id is None`, and/or set a "repoll requested" flag that the running job honors when it finishes.

### I-15 — The extractor's 500,000-character cap does not fit the 1 MiB stdout cap
`runner.py` truncates to 500,000 characters and emits JSON with `ensure_ascii=False`. CJK text uses 3 bytes per character in UTF-8, and JSON escaping adds more, so a long non-ASCII article (about 350k characters or more) breaches the parent's 1 MiB stdout cap (`client.py:207`). Extraction then falls back to `fallback_rss` instead of storing the truncated text.
- **Fix**: truncate by UTF-8 byte budget (for example 900 KB after encoding), or raise the stdout cap to fit the worst case.

### I-16 — When `pg_advisory_unlock` fails, the pooled connection still holds the session lock
`advisory_session_lock` (`jobs/locks.py:39-42`) logs and continues if the unlock raises, then returns the connection to the pool still holding `LOCK_RUN_ONCE`. An unlock interrupted by `CancelledError` (a `BaseException`) also skips the handler. Impact is limited today because `run-once` is a short-lived process, but the helper is documented as pool-safe.
- **Fix**: on unlock failure or cancellation, call `await conn.invalidate()` so the connection is discarded rather than reused.

### I-17 — The Docker image ignores `uv.lock`
`backend/Dockerfile` runs `pip install .`, which resolves dependencies fresh within the `pyproject.toml` ranges at build time. Production can therefore run different versions of `httpx`, `httpcore`, `feedparser` and `trafilatura` than the ones tested. That matters most for the SSRF transport, which depends on `httpcore` internals.
- **Fix**: install with `uv sync --frozen --no-dev` (or `uv export --frozen | pip install -r /dev/stdin`) in the builder stage. Consider pinning the base images by digest.

### I-18 — The restore drill prints row counts but does not verify them, and puts the DB password on the command line
`scripts/restore-drill.sh` only echoes `feeds`/`items`/`devices` counts. Design §11.3 step 3 says to *verify* them, for example against the counts in the source DB at dump time, or at least that they are non-zero when production is non-empty. `TARGET_URL`, which includes the password, is passed as a CLI argument and is visible in `ps`/`docker inspect` while the command runs.
- **Fix**: compare against the counts captured from the dump's source DB, and pass the URL through an environment variable (for example `--database-url-env`).

### I-19 — `feedparser` parsing runs synchronously on the event loop
`_parse_feed_bytes` runs in the worker's event loop. A 5 MB feed can take hundreds of milliseconds to seconds, which stalls the other three I/O slots, heartbeats and the scheduler (and see `I-01`).
- **Fix**: run it with `await asyncio.to_thread(_parse_feed_bytes, …)`.

---

## 4. Nits

- **I-20**: `run_worker_once` keeps an `in_flight_jobs` dict that is written but never read (`runner.py:350, 380-402`). Remove it or use it for `I-03`.
- **I-21**: `fail_job` and the reaper compute `run_after` from the worker's Python clock, while claim and other paths use the DB's `now()`. Use `sa.func.now() + make_interval(...)` so one clock is used throughout.
- **I-22**: In `SSRFPolicy.ip_block_reason`, the NAT64 well-known branch returns `None` early (`safe_http.py:139-144`), so user-configured `SSRF_BLOCKED_CIDRS` covering `64:ff9b::/96` are never checked. Fall through to the CIDR checks instead.
- **I-23**: `DomainRateLimiter._locks` and `_last_request` grow without bound (one entry per host ever seen). Prune idle entries occasionally.
- **I-24**: `authenticate_bearer_token` calls `session.commit()` on the shared request session from inside the auth dependency. It is harmless today, but it commits whatever the route has staged up to that point. Prefer a separate short session for the `last_seen_at` bump.

---

## 5. Verification performed

- `make check` (2026-11-25): `ruff check`, `ruff format --check` and `mypy --strict` are clean. All **110 tests pass** (unit, boundary and real-Postgres integration against `db-test`). Total coverage is **84.13%**. The per-module figures are in `I-08`.
- `I-01` was reproduced directly with the project venv: `feedparser.parse(<path>.encode())` returned the contents of a local XML file.
- `I-02`–`I-05` were found by code reading; they have no reproducing test yet. Each fix should land with the regression test described above, per the regression policy in `development-process.md`.
