# Design Review — Milestone `1a` Component Design

- **Date**: 2026-11-24
- **Scope**: [`docs/design/1a-backend-ingestion-and-security.md`](../design/1a-backend-ingestion-and-security.md) (Draft, as of commit `aa3e6da`), checked against `requirements.md`, `data-model.md`, `architecture.md`, and ADRs `0003`, `0004`, `0006`
- **Reviewer**: Claude Code (at the project owner's request)
- **Status**: Addressed & verified — all 33 findings (`A-01`–`A-33`) and `T-01`–`T-08` resolved; the verification pass (§7) found and fixed `N-01`–`N-11`; owner decisions on `A-14`, `A-15` and `A-33` recorded (§6). `1a` marked **`Approved`** on 2026-11-24.

## Review outcome

Overall, the design holds up. The milestone boundaries are clean. The transaction boundaries match ADR 0004: slow I/O always finishes before `SyncLogWriter.record()`. Pinning the IP in `connect_tcp()` is the right way to do SSRF pinning without breaking SNI. Using JSON for OPML import keeps the CSRF rule free of exceptions, and the test matrix targets real invariants.

The problems are in the details of the ingestion and queue state machines. The main ones:

- one value (`content_hash`) is used for two different purposes;
- the backfill path skips deduplication;
- `run-once` cannot finish while backed-off jobs are waiting;
- a dead-lettered poll job causes an endless re-enqueue loop;
- the design claims decompression-bomb protection that `httpx` does not actually provide.

**Trade-off confirmations requested in §2 of the design:**

- **T-01 (muted feeds → auto-`read`)**: Agree. Also set `read_state_updated_at = fetched_at` explicitly (see `A-17`). Add a sentence on whether muted items will be enriched in `1d`; a likely default is "extract yes, enrich no" to save GPU time. Land the `data-model.md` §3.1 transition in the same commit that approves `1a`.
- **T-02 (milestone-scoped schema plus early `sync_changes`)**: Agree. Fix `A-16` first.
- **T-03 to T-08**: Agree. For T-05, see the residual-risk note in `A-14`.

---

## 1. Blockers (fix before `Approved`)

### A-01 — `content_hash` is overwritten by extraction, so every unread item is re-extracted on every poll
§9.1 step 3 computes `content_hash = sha256(title + raw_summary)` from the feed entry. §9.2 step 5 overwrites it with `sha256(title + extracted_text)`. The steady-state update check in §9.1 step 4 then compares the new feed-entry hash against the stored extraction hash. After every successful extraction these differ, so each hourly poll of an unread item less than 7 days old does three things:
- rewrites `title`/`raw_summary`;
- re-enqueues `extract_content`;
- emits an `item` upsert.

The cycle repeats every hour. Once `1d` exists, each cycle would also trigger a re-enrichment on the GPU.
**Fix**: Store two hashes. `entry_hash` comes from the feed entry and drives update detection. `content_hash` comes from the extracted or fallback text and is used for `1e` dedup. Extraction must never touch `entry_hash`.

### A-02 — The backfill branch skips the dedup lookup (breaks on re-subscribe)
In §9.1 step 4, the backfill branch (`initial_backfill_done = FALSE`) and the steady-state dedup branch are mutually exclusive. When an `unsubscribed` feed is reactivated, `POST /feeds` resets `initial_backfill_done = FALSE`. The next 200 response then runs backfill over entries that already exist in `items`. Depending on how the insert is written, one of two things happens:
- the insert hits `uq_items_feed_guid` and fails the whole poll transaction; or
- an upsert resets existing `read`/`expired` items to `unread`, which breaks the "never `read` → `unread`" invariant.

Reactivation also keeps the old `etag`/`last_modified`, so the server can answer `304` indefinitely and backfill never happens.
**Fix**:
- Always run the `(feed_id, guid)` / `(feed_id, canonical_url)` lookup first, and apply the cap only to entries that are actually new.
- Clear `etag`/`last_modified` on reactivation.

### A-03 — A duplicate GUID within one fetch permanently breaks the feed
`guid` falls back to `canonical_url`. Real feeds sometimes repeat an entry `id`, or every entry links to the same page (comic and podcast feeds, for example). Two entries in one response that derive the same `guid` are both treated as new, and the second insert violates `uq_items_feed_guid`. The persist transaction then aborts on every poll from then on, so the feed never ingests again (and see `A-05`).
**Fix**:
- Dedupe entries by `guid` inside the batch before persisting, keeping the first entry.
- When an entry has no `id`, derive the fallback `guid` from `sha256(link + title)` instead of the bare URL.
- Use `INSERT … ON CONFLICT (feed_id, guid) DO NOTHING` as a backstop.

### A-04 — `worker run-once` never finishes while backed-off jobs are queued
§8.3 step 4 drains "until no queued/running jobs matching the requested stages remain". A job that is waiting out a retry backoff is `queued` with `run_after` up to an hour in the future. In that case `run-once` keeps spinning until `--max-duration`, which is optional and has no default. A host cron job then hangs indefinitely while holding `LOCK_RUN_ONCE`, so every later cron invocation exits with `skipped_locked`. Cron-only installs would silently stop polling.
**Fix**:
- Count only jobs with `run_after <= now()`, or `<= run start + small epsilon`, toward the "remaining" condition.
- Give `--max-duration` a default, for example 30 minutes.
- Report deferred jobs in the summary.

### A-05 — A dead-lettered `poll_feed` job causes an endless re-enqueue loop
`poll_feed` handles HTTP failures itself, but an unexpected exception (for example `A-03`, a parser crash, or a bug) goes through queue retries and ends in `failed`. Because `feeds.next_fetch_at` was never advanced, the scheduler enqueues a fresh job 30 seconds later. The dedupe index only covers `queued`/`running`, so it does not stop this. The result is a new dead-letter row about every 15 minutes per bad feed, forever, with nothing shown in Feed Health.
**Fix**: In the job-failure path for `poll_feed`, also advance `next_fetch_at` using the error backoff, increment `consecutive_errors`, and set `last_error`, all in one feed-column update. That way an unexpected exception degrades the feed's health the same way an HTTP error does.

### A-06 — The decompression-bomb protection doesn't work as described
§7.2 says counting the bytes from `aiter_bytes()` "caps the decompressed size directly". It does cap the total, but `httpx` decompresses each network chunk in full before yielding it (`zlib`/`brotli` `decompress()` is called with no `max_length`). A single ~64 KB gzip chunk can expand to about 64 MB, and a brotli chunk can expand to far more. That allocation happens before the counter runs. The server picks `Content-Encoding` regardless of what was requested, so a hostile feed can push the worker past its 2 GB `mem_limit`. That OOM kills every in-flight job, and the retries then repeat it.
**Fix**:
- Read with `aiter_raw()` and decode yourself with `zlib.decompressobj().decompress(chunk, max_length=remaining)`.
- Advertise only `Accept-Encoding: gzip, deflate`, and reject any other `Content-Encoding`.
- Add a boundary test that sends one highly compressible chunk.

---

## 2. Major

### A-07 — The poller's feed update can overwrite concurrent user edits
`poll_feed` loads the feed row, does seconds of I/O, and then "updates feed metadata". If that update goes through the ORM's dirty-tracking on the instance it loaded at the start, a `PATCH` that arrives in the meantime (pause, mute, category change, `curation_instructions`) can be reverted. The same applies to `410` and `301` handling.
**Fix**: Spell out that the stage writes only poll-owned columns through an explicit column-scoped `UPDATE … WHERE id = :id`, and re-reads `status` inside the persist transaction. Use the re-read `status` value, not the initial load, to decide `read` vs. `unread`.

### A-08 — Job completion isn't fenced against a reclaimed lease
If a worker loses its DB connection, heartbeats stop. The reaper requeues the job and another worker claims it. The first worker then reconnects and marks the job `done`/`failed`, overwriting the second attempt's state and metrics.
**Fix**:
- Make complete, fail, and heartbeat conditional: `WHERE id = :id AND status = 'running' AND locked_by = :worker_id AND attempts = :claimed_attempt`.
- If zero rows match, treat it as "lost lease" and log it.

### A-09 — Session advisory locks and a connection pool don't mix
A session-level `pg_try_advisory_lock` belongs to the physical connection that ran it. With SQLAlchemy pooling, `LOCK_RUN_ONCE`/`LOCK_SCHEDULER` can be:
- taken on one pooled connection and released on another, in which case the unlock silently returns `false` and the lock leaks until that pooled connection closes; or
- held by a connection that some unrelated request later reuses.

**Fix**:
- Hold session locks on a dedicated `AsyncConnection` that is checked out for the lock's whole lifetime.
- Use `pg_try_advisory_xact_lock` for the per-tick scheduler and reaper.

### A-10 — Cron-only mode never runs the reaper or retention cleanup
The `run-once` stages are `all|poll|extract|expire`. Users who choose external cron and don't run the daemon therefore never get stale-lease reclaim (for example after a `run-once` that was killed) or `retention_cleanup`. As a result, `jobs`, `sync_changes`, and `stage_metrics` grow without limit.
**Fix**:
- Have `run-once` always run the reaper first.
- Include `retention_cleanup` in `--stage all` when it is due, with the last run recorded in the DB rather than in memory. Add a `maintenance` stage.
- State whether running daemon and cron mode at the same time is supported. If it isn't, make it a `WORKER_MODE` setting.

### A-11 — Server-side sync emission is incomplete and relies on discipline
- The `304`, `429`, and network-error paths change client-visible `health_state` and `last_error`, but only `410` and the success path say they emit a `feed` upsert. Data model §5.1 requires an upsert on any poll health change.
- The success path emits a `feed` upsert on every poll even when nothing visible changed (~600 per day of no-op aggregates).
- Category soft-delete clears `feeds.category_id` without stamping `field_updated_at['category_id']`. In `1b`, a stale offline category change would then win LWW and re-point the feed at the deleted category.

**Fix**:
- Emit a `feed` upsert only when a synced column actually changes.
- Have every server-side write to an LWW field stamp `field_updated_at`.
- Add a structural guard instead of relying on "every service method remembers": a session `before_flush`/`before_commit` hook that fails in tests when a synced entity is dirty but no event was recorded.

### A-12 — Extraction that exhausts its retries leaves items `pending` forever
§9.2 retries only transient 5xx errors and timeouts. Once attempts run out, the job dead-letters, but `items.extraction_status` stays `pending`. The `failed` enum value is never set anywhere in the design, and downstream stages in `1d`/`1e` will be waiting on extraction.
**Fix**: On the final attempt, set `extraction_status = 'failed'` (or `fallback_rss`) and emit the item upsert, so the pipeline continues on `raw_summary`.

### A-13 — Subprocess I/O can deadlock, and the process-group kill doesn't work as written
- `stdout`, `stdin`, and `stderr` are all `PIPE`, but only `stdout` is described as being read. If `trafilatura`/`lxml` writes more than about 64 KB of warnings to `stderr`, the child blocks and is then killed by the 20-second timeout. Extraction still appears to work, but much more slowly.
- Killing the process group requires `start_new_session=True`, which isn't specified.

**Fix**:
- Use `proc.communicate(input=…)` inside `asyncio.wait_for`, with `stderr` sent to `DEVNULL` or a capped drain.
- Spawn with `start_new_session=True` and kill with `os.killpg`.
- Add a boundary test where the child writes 1 MB to `stderr`.

### A-14 — The extractor subprocess still has network access (residual risk to record)
The child process has no secrets in its environment, but it shares the worker's network namespace. A parser exploit could reach `db:5432` (unauthenticated probing), the LAN, or the host LLM. That's acceptable for `trafilatura` in `1a`, but `1f` puts `pymupdf`, a C library with a history of CVEs, in the same subprocess.
**Fix**: Record this as an accepted residual risk now. For `1f`, either move to a separate extractor service with `network_mode: none` (ADR 0006 already permits this) or use `unshare --net` / seccomp in the child.

### A-15 — Restore procedure: wrong target DB, revoked devices come back, no production runbook
- `smartrss db rotate-epoch` reads `DATABASE_URL`. As written, the drill could rotate production's epoch instead of the drill database's. Add a required `--database-url` (or `--dbname`) flag, and have the drill pass it explicitly.
- Restoring a dump brings back every `devices` row as it was at backup time. A device revoked after the backup is valid again. The restore runbook should re-apply revocations (keep a small out-of-DB revocation list, or print a "review devices" step with `smartrss devices list`).
- There is only a drill, with no real-restore runbook. The steps should be: stop `api`/`worker` → restore → rotate the epoch **before** `api` serves traffic → start. NFR-OPS-01 requires a documented restore, not only a drill.
- The architecture's drill step "confirms paired clients cleanly resync without losing pending outbox mutations" can't be tested in `1a`. Say that it moves to `1b`/`1c`.

### A-16 — `sync_changes.entity_id UUID` can't represent `app_settings`
`app_settings` uses `id SMALLINT = 1`, but it is listed as a synced `entity_type` with a `UUID NOT NULL` `entity_id`. Use a fixed sentinel UUID constant for singleton entities and document it in data-model §4.2. `1b`'s `interest_profile` has the same issue.

### A-17 — Initial values for LWW timestamps aren't specified on insert
`read_state_updated_at` and `starred_updated_at` are `NOT NULL`, but the insert paths don't say what they're set to. This matters for `1b` LWW: backfilled items set to `expired` should get `read_state_updated_at = fetched_at`, and new items should get `fetched_at` as well. Say so explicitly, and say whether `expired_at` for backfill is `fetched_at`; the design says yes, and that's fine.

### A-18 — There is no way to change a feed's URL, so `gone` is a dead end
The data model's health state machine has `gone → healthy : User edits URL & poll succeeds`, but `PATCH /feeds/{id}` doesn't list `feed_url`. Add it. On change:
- validate the new URL;
- check uniqueness and return `409` on collision;
- clear `etag`/`last_modified`;
- set `health_state = 'healthy'`, `consecutive_errors = 0`, `next_fetch_at = now()`;
- enqueue a poll.

Decide whether the backfill cap applies again; the recommendation is yes.

---

## 3. Minor

| ID | Finding | Suggested fix |
| :--- | :--- | :--- |
| A-19 | A manual `POST /feeds/{id}/poll` (priority 10) is silently ignored when a `poll_feed` job for that feed is already `queued` with a backoff `run_after` (`ON CONFLICT DO NOTHING`). | Use `ON CONFLICT … DO UPDATE SET priority = LEAST(...), run_after = LEAST(...) WHERE jobs.status = 'queued'`. |
| A-20 | Retrying a dead-letter job raises a unique-index violation if the scheduler has already enqueued a live job with the same `dedupe_key`. | Catch the conflict and return `409`, or dismiss the dead-letter row and bump the live job. |
| A-21 | `retention_cleanup` deletes `pipeline_runs` older than 180 days, but dead-letter `jobs` (kept indefinitely) and `stage_metrics` reference `pipeline_runs.id`, so the delete fails with an FK violation. | Declare `run_id` FKs `ON DELETE SET NULL`, and delete `stage_metrics` before `pipeline_runs`. |
| A-22 | Docker's default `stop_grace_period` is 10 seconds, but shutdown waits up to 25 seconds, and one extraction can take about 50 seconds (30-second fetch plus 20-second subprocess). | Set `stop_grace_period: 40s` on `worker`. On shutdown, release unfinished jobs back to `queued` instead of waiting for the lease to expire. |
| A-23 | `httpx` has no "total" timeout, so a server that drips bytes slowly can get around `read=20s`. | Wrap each fetch in `asyncio.timeout(total)`. |
| A-24 | OPML is parsed in the `api` process; the parser library isn't specified. | Use `defusedxml` (or `lxml` with `resolve_entities=False, no_network=True`). Also state that `feedparser` runs in-process on untrusted XML up to 5 MB (relying on expat's entity-expansion protection) as an explicit decision. |
| A-25 | The pairing rate limiter is keyed by client IP, but behind nginx `request.client.host` is the nginx container IP (uvicorn only trusts `X-Forwarded-For` from `127.0.0.1` by default). As a result, all clients share one bucket. | Either accept a single global bucket (fine at 80-bit code entropy) and say so, or set `--forwarded-allow-ips` to the nginx address. |
| A-26 | The fallback `guid` equals `canonical_url`, so any later change to the `url_canon` rules re-ingests every id-less entry as new. | Derive the fallback `guid` from the raw link (or a hash of link and title, see `A-03`). Use `canonical_url` only for matching. |
| A-27 | It's unclear who owns `feeds.title`/`site_url`. `title` is `NOT NULL` at subscribe time, before the feed has been fetched, and the poller doesn't say whether it updates `title` from feed metadata or how it avoids overwriting a title the user renamed. | Initialize `title` from the URL. The poller fills it in from the feed only while `field_updated_at['title']` is unset. |
| A-28 | `config_version` scope (S-04) is pushed to `1b`, but `1a` already implements `PATCH /feeds` with `expected_config_version`. | State it now: `config_version` increments only on `curation_instructions` writes. |
| A-29 | Env vars `DEFAULT_BACKFILL_CAP`/`DEFAULT_EXPIRATION_DAYS`/`DEFAULT_POLL_INTERVAL_MINUTES` duplicate `app_settings` columns, and `feeds.poll_interval_minutes` hard-codes `DEFAULT 60`. Two sources of truth. | Env vars seed `app_settings` only on first migration; after that, the DB is authoritative. Inserts read the default from `app_settings`. |
| A-30 | The ≥90% coverage list leaves out `db/sync_log.py` (sync watermark is listed as critical core logic in AGENTS.md), `extract/client.py`, and `jobs/locks.py`. | Add them. Also add a small concurrent-writers test showing `sync_changes.seq` is gap-free in commit order, since `1a` writes through it. |
| A-31 | `rsync -az --remove-source-files=false` isn't valid; the flag takes no value. Retention on the remote copy isn't specified, and dump file permissions aren't stated. | Drop the flag, document remote retention, and write dumps with `umask 077`. Also ship an example host cron entry or systemd timer for `make backup`. |
| A-32 | `Host` allow-list: internal callers such as a compose healthcheck to `api:8000` get `400`. The CORS default includes the Vite dev origin `localhost:5173` in production. `devices.platform` allows `cli`, but the CLI and data model list only `ubuntu\|android\|web`. | Exempt `/api/v1/health` from the Host check or add `api:8000`. Move `5173` to a dev-only `.env`. Align the platform enum. |
| A-33 | Gaps in the SSRF denylist and residual risk: there are no explicit entries for `64:ff9b::/96` (NAT64) and `2002::/16` (6to4, which embeds private IPv4). A feed whose DNS resolves to the user's own WAN IP can reach router-forwarded ports through hairpin NAT. If the host has global IPv6, its own address isn't private. | Add the two prefixes, unwrapping the embedded IPv4 as is already done for `::ffff:`. Record the hairpin/own-IP case as accepted residual risk, or add an optional `SSRF_BLOCKED_HOSTS_SELF` that resolves the host's public IPs at startup. |

---

## 4. Things done well

- Pinning the IP in `connect_tcp()` (T-04) is the correct design. Rejecting a host when *any* of its resolved IPs is blocked handles dual-homed DNS cleanly.
- `permanent_redirect_url` is set only when every hop was a `301`/`308`, which is correct per RFC 9110. The `301` collision case is handled without crashing.
- Slow I/O always happens outside the `SyncLogWriter` transaction, which honors ADR 0004's critical clarification.
- The pairing-code redemption is an atomic `UPDATE … RETURNING`, the code has about 80 bits of entropy, and tokens are stored as hashes. The `last_seen_at` write throttle is a nice touch.
- The expiration SQL matches data-model §6.1 exactly and uses `SKIP LOCKED` batching.
- The test matrix targets real invariants (dual-homed DNS, redirect to `169.254.169.254`, env-secret absence in the child) rather than mocks.

## 5. Suggested next step

1. Fold `A-01`–`A-06` into the design. Each is a paragraph-level change.
2. Fix the Major items, or record an explicit "accepted / deferred to `1b`/`1f`" note for each. At minimum, `A-14`, `A-15` (revocations), and `A-33` need an owner decision.
3. Land the T-01 transition and `A-16` sentinel UUID in `data-model.md` in the same commit that marks `1a` `Approved`.

---

## 6. Resolution Tracker (2026-11-24)

All 33 findings (`A-01`–`A-33`) and trade-off alignments (`T-01`–`T-08`) have been addressed across [`docs/design/1a-backend-ingestion-and-security.md`](../design/1a-backend-ingestion-and-security.md) and [`docs/data-model.md`](../data-model.md).

### Trade-offs (`T-01`–`T-08`)
| ID | Disposition | Resolution | Where Recorded |
| :--- | :--- | :--- | :--- |
| **T-01** | Resolved | Added muted-feed `[*] --> read` transition (`read_at = read_state_updated_at = fetched_at`) to `data-model.md` §3.1–§3.2 and documented `1d` default ("extract yes, enrich no" unless explicitly requested). | `1a` §2, §9.1; `data-model.md` §3.1–§3.2 |
| **T-02** | Resolved | Confirmed milestone-scoped schema + early `sync_state`/`sync_changes`, with singleton sentinel UUIDs fixed per `A-16`. | `1a` §2, §5.1; `data-model.md` §4.2 |
| **T-03–T-08** | Confirmed | Retained as designed; `T-05` cross-references `A-14` network-namespace residual risk and `1f` isolation requirement. | `1a` §2, §13 |

### Blockers (`A-01`–`A-06`)
| ID | Disposition | Resolution | Where Recorded |
| :--- | :--- | :--- | :--- |
| **A-01** | Fixed | Split hashes into `entry_hash` (SHA-256 of feed `title + raw_summary`, owned by `poll_feed` for update detection, never touched by extraction) and `content_hash` (SHA-256 of `title + (extracted_text or raw_summary)`, updated by `extract_content` on `ok` for `1e` dedup). Added integration test. | `1a` §5.1, §9.1, §9.2, §12.1; `data-model.md` §3.3, §4.5 |
| **A-02** | Fixed | Backfill path now always runs `(feed_id, guid)` / `(feed_id, canonical_url)` dedup lookup first, applying cap `N` only to genuinely new entries (preserving existing `read`/`expired` items). Reactivation (`POST /feeds`, `import-opml`) clears `etag`/`last_modified` and resets health/schedule. Added integration test. | `1a` §9.1, §10.1, §12.1; `data-model.md` §3.2 |
| **A-03** | Fixed | Added intra-batch dedup by `guid` (first occurrence wins), changed fallback `guid` to `"fallback:" + sha256(raw_url + "\n" + title.strip())`, and added `INSERT ... ON CONFLICT (feed_id, guid) DO NOTHING RETURNING id` backstop. Added integration test. | `1a` §9.1, §12.1 |
| **A-04** | Fixed | `worker run-once` drain loop counts only `running` or ready (`queued AND run_after <= now()`) jobs, sets default `--max-duration 1800` (30m), and reports backed-off jobs as `jobs_deferred` in summary/JSON and `pipeline_runs.notes`. Added integration test. | `1a` §8.3, §10.2, §12.1 |
| **A-05** | Fixed | Added `poll_feed` job-failure/dead-letter hook (including reaper dead-lettering) that performs a column-scoped `UPDATE feeds` incrementing `consecutive_errors`, degrading `health_state`, recording `last_error`, and advancing `next_fetch_at` with exponential backoff + jitter (emitting diff-gated `feed` `upsert`). Added integration test. | `1a` §8.1, §12.1 |
| **A-06** | Fixed | Switched `SafeHttpClient` to `Accept-Encoding: gzip, deflate` only (rejecting other/chained encodings with `UnsupportedEncodingError`), streaming raw wire bytes via `response.aiter_raw()` and manually decompressing incrementally with `zlib.decompressobj().decompress(chunk, max_length=remaining + 1)`. Added single-chunk gzip-bomb boundary test. | `1a` §7.2, §12.1 |

### Major Findings (`A-07`–`A-18`)
| ID | Disposition | Resolution | Where Recorded |
| :--- | :--- | :--- | :--- |
| **A-07** | Fixed | Mandated explicit column-scoped `UPDATE feeds` touching only poll-owned columns (never persisting pre-I/O ORM state) and re-reading `status`/config via `SELECT ... FOR UPDATE` inside the short persist transaction. Added concurrent-`PATCH` integration test. | `1a` §9.1, §12.1 |
| **A-08** | Fixed | Fenced heartbeat, complete, retry, dead-letter, and shutdown-release updates with `WHERE id = :id AND status = 'running' AND locked_by = :worker_id AND attempts = :claimed_attempt`; 0-row updates log `job_lease_lost` and abort without clobbering the new owner. Added integration test. | `1a` §8.1, §12.1 |
| **A-09** | Fixed | Switched per-tick `LOCK_SCHEDULER` and `LOCK_REAPER` to `pg_try_advisory_xact_lock`, and bound session-level `LOCK_RUN_ONCE` to a dedicated checked-out `AsyncConnection` for its entire lifetime. Added pool-safety integration test. | `1a` §8.2, §12.1 |
| **A-10** | Fixed | `run-once` now always runs `reap_stale_leases` first; persisted `app_settings.last_retention_cleanup_at` (internal operational column, not client-synced) so daily `retention_cleanup` runs in both daemon and `run-once --stage all`; added `--stage maintenance` and `WORKER_MODE=daemon\|cron` (explicitly unsupported to run both concurrently). | `1a` §4.3, §5.1, §8.3, §9.3, §10.2, §12.1 |
| **A-11** | Fixed | Made `feed` `upsert` emission diff-gated across all poll paths (no-op polls emit 0 `feed` upserts; any `304`/`429`/`410`/error health change emits `feed` `upsert`); stamped `field_updated_at` on all server LWW writes (including `DELETE /categories/{id}` clearing `category_id` and reactivation setting `status`); added a SQLAlchemy `before_commit` structural test guard (`MissingSyncEventError`). | `1a` §5.1, §9.1, §10.1, §12.1 |
| **A-12** | Fixed | On final `extract_content` retry exhaustion (`attempts >= max_attempts`), transition `items.extraction_status = 'failed'`, emit `item` `upsert` via `SyncLogWriter`, and dead-letter the job so items never remain stuck in `pending`. Added integration test. | `1a` §8.1, §9.2, §12.1 |
| **A-13** | Fixed | Spawn extractor with `start_new_session=True`, `stderr=DEVNULL` (suppressing noisy third-party stderr loggers in `runner.py`), bounded incremental `stdout` read (1 MB) inside `asyncio.timeout(20.0)`, and `os.killpg(proc.pid, SIGKILL)` on timeout/cap breach. Added 1 MB stderr and process-group kill boundary tests. | `1a` §9.2, §12.1 |
| **A-14** | **Accepted residual risk (owner decision)** | Documented shared `worker` network namespace in `1a` (`trafilatura`/`lxml` HTML only) as an accepted residual risk; required network isolation (`network_mode: none` extractor service or `unshare --net`/seccomp) in **`1f`** prior to adding `pymupdf`. | `1a` §2 (T-05), §13 |
| **A-15** | Fixed + **owner decision** + **1b/1c deferral** | Required explicit `--database-url` / `--dbname` on `smartrss db rotate-epoch`; added full Production Restore Runbook (stop → restore → rotate epoch → review/re-revoke devices → start); documented manual revocation checklist in §11.2 & §13 (owner decision vs. out-of-DB revocation file); explicitly deferred client-resync verification of the drill to **`1b`/`1c`**. | `1a` §10.2, §11.2, §11.3, §13 |
| **A-16** | Fixed | Defined fixed deterministic sentinel UUID constants for singleton synced entities: `APP_SETTINGS_ENTITY_ID = 00000000-0000-0000-0000-000000000001` and `INTEREST_PROFILE_ENTITY_ID = 00000000-0000-0000-0000-000000000002`. | `data-model.md` §4.2; `1a` §5.1 |
| **A-17** | Fixed | Explicitly specified initial insert timestamps: `read_state_updated_at = fetched_at`, `starred_updated_at = fetched_at`, `read_at = fetched_at` (muted), and `expired_at = fetched_at` (backfill beyond cap `N`). | `1a` §5.1, §9.1; `data-model.md` §3.1, §4.5 |
| **A-18** | Fixed | Added `feed_url` to `PATCH /api/v1/feeds/{id}` with SSRF validation (`422`), uniqueness check (`409`), clearing `etag`/`last_modified`, resetting health/errors/`next_fetch_at`/`initial_backfill_done=FALSE` (backfill cap applies to newly fetched entries while existing items dedup), and enqueueing immediate poll. Added integration test. | `1a` §10.1, §12.1 |

### Minor Findings (`A-19`–`A-33`)
| ID | Disposition | Resolution | Where Recorded |
| :--- | :--- | :--- | :--- |
| **A-19** | Fixed | Added `bump_existing=True` enqueue path (`ON CONFLICT ... DO UPDATE SET priority = LEAST(...), run_after = LEAST(...) WHERE jobs.status = 'queued'`) for manual `POST /feeds/{id}/poll` and subscribe/URL-edit triggers. | `1a` §8.1, §10.1, §12.1 |
| **A-20** | Fixed | `POST /api/v1/system/jobs/{id}/retry` catches `uq_jobs_active_dedupe` conflicts and returns `409 Conflict` with `active_job_id`. | `1a` §10.1, §12.1 |
| **A-21** | Fixed | Declared `jobs.run_id` and `stage_metrics.run_id` FKs `ON DELETE SET NULL` and ordered `retention_cleanup` to delete `stage_metrics` before `pipeline_runs`. Added integration test. | `1a` §5.1, §9.3, §12.1; `data-model.md` §4.9 |
| **A-22** | Fixed | Set `stop_grace_period: 40s` on `worker`; graceful shutdown waits up to 30s then cancels and releases unfinished jobs back to `queued` (`run_after = now()`, `attempts = GREATEST(attempts - 1, 0)`) fenced by `locked_by`. | `1a` §4.1, §8.3 |
| **A-23** | Fixed | Wrapped each full `SafeHttpClient` fetch (redirects + body streaming) in `asyncio.timeout(total)` (default `30s`) alongside per-op `httpx.Timeout`. Added boundary test. | `1a` §7.2, §12.1 |
| **A-24** | Fixed | Specified `defusedxml` for OPML parsing in `api` and recorded `feedparser` in-process parsing (capped at 5 MB, relying on Expat's entity-expansion protections) as an explicit design decision. | `1a` §3, §9.1, §10.1, §12.1 |
| **A-25** | Fixed | Switched `/api/v1/auth/pair` rate limiter to a single global sliding-window bucket (5 attempts / 5m), avoiding `X-Forwarded-For` proxy-trust complexity behind Nginx at 80-bit code entropy. | `1a` §6.1, §10.1 |
| **A-26** | Fixed | Fallback `guid` now hashes `raw_url + "\n" + title.strip()` (decoupled from `url_canon`); `canonical_url` is used strictly for fallback matching and `1e` dedup. | `1a` §9.1 |
| **A-27** | Fixed | Defined `feeds.title`/`site_url` ownership: `title` initializes from user/OPML input or feed URL; `PATCH` stamps `field_updated_at['title']`; `poll_feed` updates `title` only while `'title'` is absent from `field_updated_at` and updates `site_url` when present. | `1a` §9.1, §10.1, §12.1 |
| **A-28** | Fixed | Stated `feeds.config_version` scope (`S-04`) now: increments **only** on `curation_instructions` writes, independent of `field_updated_at` LWW fields. | `1a` §5.1, §10.1; `data-model.md` §4.4 |
| **A-29** | Fixed | Clarified env vars (`DEFAULT_POLL_INTERVAL_MINUTES`, `DEFAULT_BACKFILL_CAP`, `DEFAULT_EXPIRATION_DAYS`) seed `app_settings` only on initial migration `0001`; DB is authoritative thereafter and feed inserts read default poll interval from `app_settings`. | `1a` §4.3, §5.1, §10.1 |
| **A-30** | Fixed | Added `db/sync_log.py`, `extract/client.py`, and `jobs/locks.py` to the ≥90% critical-coverage list and added a real-Postgres concurrent-writers test proving `sync_changes.seq` is gap-free in commit order. | `1a` §12, §12.1 |
| **A-31** | Fixed | Replaced invalid `--remove-source-files=false` with `rsync -az --delete` (matching 14-day local retention on remote mirror), added `umask 077` for `0600` dump files, and added `deploy/systemd/smartrss-backup.{service,timer}` + cron example. | `1a` §3, §11.1 |
| **A-32** | Fixed | Exempted `GET /api/v1/health` from `HostAllowListMiddleware` for internal compose healthchecks; moved `http://localhost:5173` CORS origin to `.env.dev.example`; removed `'cli'` from `devices.platform` to match `ubuntu\|android\|web`. | `1a` §3, §4.3, §5.1, §6.1, §10.1, §12.1 |
| **A-33** | Fixed + **owner decision** | Added NAT64 (`64:ff9b::/96` unwrapped + `64:ff9b:1::/48` blocked) and 6to4 (`2002::/16` unwrapped via `ip.sixtofour`) to SSRF denylist + unit tests; documented hairpin NAT / host own global IP as accepted residual risk in §13 for owner decision. | `1a` §7.2, §12.1, §13 |

### Deferred & Residual-Risk Items — Owner Decisions (2026-11-24: all accepted as proposed)
| ID | Category | Proposed Disposition | Owner Decision |
| :--- | :--- | :--- | :--- |
| **A-14** | Accepted residual risk (`1a`) / Deferred (`1f`) | Accept shared `worker` network namespace for `trafilatura`/`lxml` HTML extraction in `1a`; require network isolation (`network_mode: none` container or `unshare --net`/seccomp) in **`1f`** before adding `pymupdf`. | **Accepted** — `1f` isolation is a hard design gate. |
| **A-15** *(revocations)* | Operational trade-off | Manual post-restore `smartrss devices list` + `revoke` checklist before starting `api` (§11.2 step 4). | **Accepted** — manual checklist; no out-of-DB revocation log. |
| **A-15** *(client resync)* | Deferred (`1b`/`1c`) | Defer the paired-client resync + pending-outbox preservation drill step to **`1b`/`1c`** when sync endpoints and client outbox exist. | **Accepted** — deferred to `1b`/`1c`. |
| **A-33** *(hairpin / own IP)* | Accepted residual risk | Accept hairpin NAT / host own global IPv6 as residual risk (no router port-forwarding in SmartRSS architecture; static `SSRF_BLOCKED_CIDRS` available). | **Accepted** — residual risk; README documents adding WAN/IPv6 prefixes to `SSRF_BLOCKED_CIDRS`. |

---

## 7. Verification Pass (2026-11-24)

The resolutions were checked against the revised design (commit `c1d01d2`). All `A-*` fixes were confirmed correct. The pass found two new issues introduced or exposed by the fixes, plus nine smaller gaps. All are fixed in the same commit that marks `1a` **`Approved`**.

| ID | Severity | Finding | Resolution | Where Recorded |
| :--- | :--- | :--- | :--- | :--- |
| **N-01** | Major | The `(feed_id, canonical_url)` fallback in the dedup lookup (exposed by `A-03`) matched any new entry sharing an existing item's link. In comic, podcast or "latest page" feeds, new entries were never inserted, and an unread item younger than 7 days had its `title`/`raw_summary` overwritten. The `A-03` test only covered a single fetch. | The fallback now applies only when it is unambiguous: one existing item and one batch entry share the URL, and the matched item's `guid` is absent from the batch. Added a two-poll regression test. | `1a` §9.1, §12.1; `data-model.md` §3.3 |
| **N-02** | Major | The `A-31` fix used `rsync --delete`, so an emptied or wrong local `BACKUP_DIR` would wipe the off-host copy. | No `--delete`. The copy is skipped when no valid local dump exists, and remote retention is pruned independently (`BACKUP_REMOTE_RETENTION_DAYS=30`). Added a manual exit check. | `1a` §4.3, §11.1, §12.2 |
| **N-03** | Minor | The `A-11` `before_commit` guard only sees ORM dirty state, but `poll_feed`, `expire_items` and the failure hooks write through Core SQL. | Core writes to synced tables go through the `sync_log.execute_synced()` helpers. A test-only `before_cursor_execute` hook flags any other writes. | `1a` §5.1, §12.1 |
| **N-04** | Minor | Diff-gating on the non-200 paths needs the prior column values, and PG16 has no `OLD` in `RETURNING`. | Added a short-transaction `SELECT … FOR UPDATE` re-read before the column-scoped update. | `1a` §9.1 |
| **N-05** | Minor | The `A-05` failure hook could run inside the stage's aborted transaction. | The hook runs in a fresh session/transaction after rollback. | `1a` §8.1 |
| **N-06** | Minor | The shutdown release was fenced on `locked_by` only, contradicting the `A-08` full fence. | The shutdown release now uses the full fence. | `1a` §8.3 |
| **N-07** | Minor | With `WORKER_MODE=cron`, the compose `worker` service would still start, refuse to run, and crash-loop. | `worker` sits behind the `daemon` compose profile (`COMPOSE_PROFILES`), with `init: true`. The refusal exits `78` as a safety net. | `1a` §4.1, §4.3, §8.3 |
| **N-08** | Minor | After `A-29`, nothing in `1a` could change `app_settings`, although `FR-FEED-05` requires a configurable cap. | Added `smartrss settings get\|set`, which emits an `app_settings` upsert. | `1a` §10.2, §12.1; `data-model.md` §4.9 |
| **N-09** | Minor | User-supplied subscribe titles and OPML titles were not stamped in `field_updated_at['title']`, so the poller would overwrite them. | Both are stamped. Titles derived from the URL stay unstamped. | `1a` §10.1, §12.1 |
| **N-10** | Minor | The restore runbook started the apps before migrating, and `pg_restore --clean` leaves behind objects created after the backup. | Restore now goes into a dropped and recreated DB, then rotates the epoch, re-revokes devices, and migrates before starting. | `1a` §11.2 |
| **N-11** | Minor / doc sync | `data-model.md` didn't document the `app_settings` columns or `last_retention_cleanup_at`, and it listed "scheduling mode" as a DB setting. Nits: the `deflate` variant and multi-member gzip were unspecified, and deterministic extractor kills were retried 5× (re-fetching a hostile doc each time). | Added an `app_settings` entry to data-model §4.9 and clarified that `WORKER_MODE` is env config. Specified the zlib/raw `deflate` fallback and multi-member gzip handling. Extractor timeouts and rlimit kills are now permanent (`fallback_rss`). | `data-model.md` §1, §4.9; `1a` §7.2, §9.2, §12.1 |

