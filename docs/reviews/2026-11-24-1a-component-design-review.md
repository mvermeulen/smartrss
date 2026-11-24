# Design Review — Milestone `1a` Component Design

- **Date**: 2026-11-24
- **Scope**: [`docs/design/1a-backend-ingestion-and-security.md`](../design/1a-backend-ingestion-and-security.md) (Draft, as of commit `aa3e6da`), checked against `requirements.md`, `data-model.md`, `architecture.md`, and ADRs `0003`, `0004`, `0006`
- **Reviewer**: Claude Code (at the project owner's request)
- **Status**: Open. 6 Blockers (`A-01`–`A-06`), 12 Major, 15 Minor. The overall structure is sound, but the Blockers are correctness bugs that would show up within the first day of running against real feeds. Each one is a small spec change.

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
