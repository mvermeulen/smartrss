# Review Checklist — Recurring Failure Classes

> **Status:** Living document. Used by the **reviewer** (Claude Code) during design and implementation reviews, and by the **implementer** (Antigravity) as a self-check before requesting review. See [development-process.md §7](development-process.md#7-review-gates).
>
> **Maintenance:** When a review finds a new *class* of bug (not just a new instance), add it here in the same commit as the review or fix, citing the finding ID.

Each item lists what to look for, how it went wrong before, and what "correct" looks like.

## 1. Security boundaries

| # | Check | Origin | Correct pattern |
| :--- | :--- | :--- | :--- |
| S1 | **Untrusted input into libraries.** Is fetched or user-supplied data passed to a library call that also accepts a path, URL, or file handle? | `I-01` (`feedparser.parse(bytes)` opened the body as a local file) | Wrap in `io.BytesIO`, or decode explicitly; add a static guard test that greps for the call. |
| S2 | **Every outbound fetch goes through `SafeHttpClient`.** Includes redirects, extraction, research, favicon/image fetches. | ADR 0006 | No direct `httpx`/`requests`/`urllib` use outside `security/safe_http.py`. |
| S3 | **Secrets in argv or logs.** Are DB URLs, tokens, or passwords placed on a command line (including `docker ... -e NAME=value`) or logged? | `I-18` | Pass by environment variable *name* only (`-e NAME`, `--database-url-env`); redact in logs. |
| S4 | **Stored URLs are scheme-validated.** Any URL synced to clients or rendered (`site_url`, links) is `http(s)` only. | `I-11` | Validate on every write path (API, OPML, poller), not just one. |
| S5 | **Parser resource limits.** Recursion depth, entity expansion, byte caps, and timeouts on XML/HTML/PDF/JSON parsing. | `I-12`, `A-` series | Iterative traversal or explicit depth cap; caps enforced before parsing. |
| S6 | **Size caps survive encoding.** Character caps vs byte caps vs serialized (JSON-escaped) size. | `I-15` | Cap the *final serialized* size against the consumer's limit. |

## 2. Concurrency & state

| # | Check | Origin | Correct pattern |
| :--- | :--- | :--- | :--- |
| C1 | **Stale reads across I/O.** Does code read a row, do network/LLM I/O, then write based on the old read? | `I-04` (PATCH `feed_url` during a poll was overwritten) | Re-read `FOR UPDATE` after I/O; compare identity fields (URL, version); if changed, write only bookkeeping and re-enqueue. Apply on **every** exit path, including error and unexpected-exception hooks. |
| C2 | **Ownership-scoped "in progress".** Do completion and wait checks count only work claimed by *this* run or worker? | `I-03` | Filter by `locked_by` prefix or `run_id`; report foreign work as deferred. |
| C3 | **Lease fencing.** Do heartbeat, complete, fail, and release all fence on `(id, status, locked_by, attempts)`? | ADR 0003 | Fenced `UPDATE … WHERE`; treat 0 rows as "lease lost". |
| C4 | **Enqueue while running.** Does a "trigger now" or dedupe enqueue silently no-op when the job is `running`, while the API reports success? | `I-14`, `I-04` | Return the true outcome; re-enqueue after the running job closes. |
| C5 | **One clock.** Are times compared or computed with both the DB `now()` and the Python clock? | `I-21` | Use `sa.func.now() + make_interval(...)` for DB-side scheduling. |
| C6 | **Request-scoped side effects.** Does a dependency or helper `commit()` a shared request session? | `I-24` | Use a separate short session for incidental writes. |
| C7 | **Monotonic LWW timestamps across online & offline writers.** Can an online edit write a timestamp earlier than a future-clamped offline write? | `P-02` | Online LWW writes use the DB clock and `GREATEST(db_now, stored_ts + 1µs)` via a shared helper so field timestamps never move backward. |
| C8 | **References to soft-deletable parents.** Does any write point a row at a soft-deletable parent (`feeds.category_id` → `categories`) without checking the parent is still active? The foreign key does not catch soft deletes. | `K-01`, `K-01-F1` (an offline move raced a category delete; OPML `default_category_id` was never validated) | Check against active parent rows locked `FOR SHARE` (`ORDER BY id ASC`) **before** locking or writing the child (§5.1 parent → child). Cover **every** entry path, including bulk import, defaults, and optional request fields. Return `422`, not a foreign-key `500`, for a missing parent. |
| C9 | **Predicate re-evaluation after a pre-lock pass.** After candidate rows are locked in `id ASC` order, does the later `UPDATE` re-run the predicate instead of targeting the locked IDs? | `K-04` | Add `id = ANY(CAST(:locked_ids AS uuid[]))` to the `UPDATE` (keep the re-check of state and LWW). Rows that start matching late are skipped, not locked out of order. |

## 3. Long-running processes

| # | Check | Origin | Correct pattern |
| :--- | :--- | :--- | :--- |
| L1 | **Loop survival.** Can one exception (DB blip, failed heartbeat) end a daemon task for good while the process stays "healthy"? | `I-02` | Catch and log `Exception` per iteration, then back off; re-raise only `CancelledError`; the supervisor exits non-zero if all workers die. |
| L2 | **Signal handling.** Do batch and cron commands handle `SIGTERM` by releasing leases and locks? | `I-03` | `loop.add_signal_handler` → cancel tasks → release in `except CancelledError`. |
| L3 | **Event-loop blocking.** Is CPU-heavy parsing or hashing run on the event loop? "Bounded" input is not the same as cheap: work out the worst case **per request** (rows per page × per-row cap), then measure it. Are DB round trips issued once per row? | `I-19`, `K-03` (100 × 64 KB HTML strips took 1.4 s on the loop, plus 100 serial `ts_headline` queries; the approved design called the bounded slices fine) | `await asyncio.to_thread(...)` or a subprocess; pre-slice to what the output needs; batch per-row SQL (`unnest(...) WITH ORDINALITY`). |
| L4 | **Unbounded in-memory growth.** Are there per-host or per-key dicts that never shrink? | `I-23` | Prune idle entries; cap size. |
| L5 | **Pooled connection hygiene.** Can a session lock or a failed cleanup leak back into the pool? | `I-16` | `invalidate()` the connection on cleanup failure or cancellation. |
| L6 | **Busy-wait polling.** Are there short sleep loops that hold locks for long durations? | `I-03` | Use an event, or a bounded wait with a deadline. |

## 4. Database & data

| # | Check | Origin | Correct pattern |
| :--- | :--- | :--- | :--- |
| D1 | **Bind-parameter limits.** Can multi-row `INSERT`s or `IN (...)` lists exceed PostgreSQL's 65,535 parameters? | `I-05` | Chunk (for example 500 rows) inside the same transaction. |
| D2 | **Every synced write emits a `sync_changes` event**, diff-gated, inside the same transaction. | ADR 0004 | `execute_synced` plus `SyncLogWriter`; the guards stay installed in tests. |
| D3 | **Defaults on reactivate/upsert.** Does re-creating or reactivating a row overwrite user-set fields with request defaults? | `I-13` | Apply only fields in `model_fields_set`. |
| D4 | **One field, one purpose.** Is a single column (a hash, a timestamp) used for two meanings? | `A-01` | Split into separate columns. |
| D5 | **SQLAlchemy `text()` bind-cast syntax.** Does raw SQL use `:param::type`, which SQLAlchemy leaves unbound as literal text? | `P-07` | Always use `CAST(:param AS type)` or `sa.bindparam(..., type_=...)` / `sa.cast(...)`. |
| D6 | **Canonical idempotency hashes.** Is a payload hash (idempotency key, dedupe hash) computed over a serialization that differs across equivalent encodings, such as time-zone offsets, UUID letter case, or number vs string? | `K-05` | Normalize before hashing: timestamps to UTC, UUIDs and typed values to canonical form. Unit-test equivalent encodings → identical hash. |

## 5. Input handling & encoding

| # | Check | Origin | Correct pattern |
| :--- | :--- | :--- | :--- |
| E1 | **HTTP context to parsers.** Do parsers get the base URL (`Content-Location`) and charset? | `I-06` | Pass response headers; fall back to in-document charset sniffing. |
| E2 | **Silent drops.** Are skipped or invalid records counted and surfaced (health, logs), or do they disappear? | `I-06` | Count and log them; mark the source unhealthy if *all* records are dropped. |
| E3 | **Network-dependent validation at bulk import.** Is serial DNS or HTTP done inside a request transaction? | `I-10` | Validate syntax at import; enforce at fetch time. |

## 6. Tests, gates & docs

| # | Check | Origin | Correct pattern |
| :--- | :--- | :--- | :--- |
| T1 | **The regression test exercises the fix, on every fixed path.** Does it fail with the fix reverted, and are the fixed lines covered? When a fix spans several paths, does each path have a test that fails when only that path is reverted? | `I-02` follow-up, `K-01-F1` | Revert-the-fix check, one path at a time, with the full suite, plus coverage of the fixed lines ([§2.3](development-process.md#23-regression-testing-policy-mandatory)). |
| T2 | **Promised tests exist.** Every case in the design's test-matrix section has a test. | `I-07` | Map each design test-matrix row to a test name in the review. |
| T3 | **Gates are enforced, not just documented.** Coverage thresholds and critical-module lists are checked by `make check`. | `I-08` | `scripts/check-critical-coverage.py`; `CRITICAL_MODULES` matches the critical-modules section of every approved component design (`1a` §12, `1b` §10.1). |
| T4 | **Docs in the same commit.** Status headers, index rows (no duplicates), env-var comments, and deviations from the design are updated together with the code. | `I-09` follow-up | Grep for stale status and duplicate rows before committing. |
| T5 | **Reproducible builds.** Do images install from the lockfile? | `I-17` | `uv export --frozen` / `uv sync --frozen`. |
| T6 | **Concurrency tests that can fail.** Does a race or lock-order test actually force the interleaving, and would it fail if the invariant broke? Internal retry loops (`40P01`/`40001`) turn a real deadlock into a passing end state. | `K-02` (the `T01b` lock-order test passed 5/5 with the sort reversed) | Force the overlap (barriers or hook points), disable retries (`max_attempts=1`), and assert ordering directly (capture statement order with `before_cursor_execute`) rather than only end state. Prove it with the invariant broken. |
