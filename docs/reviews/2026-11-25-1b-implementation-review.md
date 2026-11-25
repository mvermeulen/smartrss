# Code Review — Milestone `1b` Implementation

- **Date**: 2026-11-25
- **Scope**: Committed branch `milestone/1b`, `main` (`c8aa6f7`) → `070b82c` (`feat(1b): implement sync engine, LWW conflict resolution, and PostgreSQL FTS search`). Covers migration `0002`, `sync/` (`schemas.py`, `service.py`), `domain/sync_merge.py`, `domain/fts_text.py`, `search/fts.py`, the new routes (`sync`, `search`, `settings`), changes to `feeds.py`, `categories.py`, `sync_log.py`, `poll_feed.py`, `extract_content.py`, `maintenance.py`, `cli.py` and `config.py`, all new tests, `scripts/check-critical-coverage.py`, and the doc updates. Checked against [`docs/design/1b-sync-protocol-and-search.md`](../design/1b-sync-protocol-and-search.md) (Approved), `data-model.md`, ADR `0004`, and [`review-checklist.md`](../review-checklist.md).
- **Reviewer**: Claude Code (at the project owner's request)
- **Finding prefix**: `K-`
- **Status**: `Addressed (2026-11-25) — awaiting verification`

## Review outcome

The implementation is close to the approved design, and the hard invariants are handled correctly:

- **Gap-free sync.** `bootstrap` and `pull` really do run in a `REPEATABLE READ READ ONLY` snapshot (checked with `SHOW transaction_isolation` after `_begin_repeatable_read_ro`). `pull` bounds its query to `current_seq`, coalesces within each page, and turns upserts of missing or soft-deleted rows into `delete`. Every `resync_required` reason is checked in the specified priority order against `pruned_through_seq`.
- **Push engine.** The push runs in one transaction. It loads `sync_mutations` up front for idempotency, guards `payload_sha256`, and handles duplicate IDs within a batch. It locks rows in §5.1 order: categories → feeds (`id ASC FOR UPDATE`) → items (`id ASC`, chunked) → jobs → `sync_state`. It flushes before Core bulk `UPDATE`s and refreshes the ORM cache afterwards, and calls `sync_log.record()` once with coalesced events. With psycopg the retry detection reads a real `sqlstate`, and the concurrent duplicate-push test exercises the `23505` replay path for real.
- **LWW logic.** `sync_merge.py` is pure, has 100% coverage, and uses the specified rank tables. Its `unexpired_at` handling is order-independent. The epoch guard (`P-11-F1`), dead-RTC bound and future clamp are applied in the specified order.
- **Online writes.** Online writes use the DB clock. `compute_online_lww_timestamp()` is shared by `PATCH /feeds`, feed reactivation, OPML reactivation and `DELETE /categories`. `config_version` changes only when `curation_instructions` changes.
- **Pipeline changes.** `poll_feed` stamps `ingest_sync_seq` by zipping events with seqs in a single 2-parameter `UNNEST` update. Retention prunes a contiguous prefix and advances `pruned_through_seq`, touching `app_settings` before `sync_state`. `SyncLogWriter` uses `clock_timestamp()` under the lock and chunks inserts at 1,000 rows.
- **Search.** It uses `websearch_to_tsquery` with the `numnode`/`querytree` guards. Snippets are built from `STX`/`ETX`/`EOT` sentinels, with sentinels removed from the input first. `FTS_LANGUAGE` is allow-listed and bound with `CAST(... AS regconfig)`.
- **Tests and docs.** Every row of the design's §10.2 test matrix (`1b-T01`–`T12`) has a named test. The four new critical modules are in `CRITICAL_MODULES` and pass the gate. `data-model.md`, ADR `0004`, `.env.example` and the status rows are updated in the same commit.

`make check` is green: **158 passed**, **90.84 %** total coverage, and all 16 critical modules ≥ 90 % (details in §5).

I found **no Blockers or Majors**. The findings are:

- one real but narrow **race**, confirmed by reproduction: an offline category move can attach a feed to a category deleted concurrently (`K-01`);
- a **lock-order regression test that does not test lock order** (`K-02`, confirmed by reverting the fix);
- a **long event-loop stall on search** (`K-03`, measured);
- a scoped bulk-read `UPDATE` that can lock rows outside the pre-locked set (`K-04`);
- two nits;
- one pre-existing `1a` clock issue that now matters for `1b`'s LWW comparisons.

**Recommendation:** fix `K-01`–`K-04` with regression tests before `Implementation Approved`; none of them blocks further `1c` design work. `K-05`/`K-06` can go in the same pass. `K-07` is pre-existing; the owner can schedule it now or accept it.

| Severity | Findings |
| :--- | :--- |
| Blocker | — |
| Major | — |
| Minor | `K-01`, `K-02`, `K-03`, `K-04` |
| Nit | `K-05`, `K-06` |
| Pre-existing (`1a`) | `K-07` |

---

## 1. Minor

### K-01 — `feed_set_field(category_id)` can attach a feed to a category being soft-deleted at the same time (race; checklist `C1`)
`backend/src/smartrss/sync/service.py:606-623` (step 3a) checks for active categories with a plain `SELECT … WHERE deleted_at IS NULL`, which takes no row lock. The feed row is locked and its `category_id` is written later, at the step 4 flush. If an online `DELETE /api/v1/categories/{id}` commits between those two points, nothing stops it:

- it locks the category `FOR UPDATE`, which does not conflict, because push holds no lock on it yet;
- it locks child feeds `WHERE category_id = :id`, which does not match this feed, because the feed's committed `category_id` is still the old value.

The push then commits `feed.category_id = <deleted category>`. The foreign key does not catch this because categories are soft-deleted. The online `PATCH /api/v1/feeds/{id}` category check (`backend/src/smartrss/api/routes/feeds.py:537-547`) has the same unlocked read, but that code predates `1b`.

- **Verified by reproduction.** A temporary integration test (deleted afterwards) hooked `_validate_feed_field_value`, which runs after step 3a and before the flush. From that hook it soft-deleted the category on a second connection. Result: push outcome `applied`, the feed's `category_id` points to the category, and `categories.deleted_at` is set.
- **Impact.** `bootstrap` omits deleted categories, and the `pull` delete tombstone has already gone out. Clients end up with a feed whose `category_id` matches no category, which in practice means a feed missing from the category tree. Nothing repairs this automatically; the user has to move the feed again. The window is small for a single user, but the symptom is lasting and confusing, and the design's rejection rule for this case (`category_not_found`) is effectively bypassed.
- **Fix.** In step 3a, read the categories with `FOR SHARE`, `ORDER BY id ASC`. This fits §5.1, where categories come first, and it blocks a concurrent soft-delete's `FOR UPDATE` until push commits. The delete then runs afterwards and clears the feed through its child-feed path. Apply the same lock in the `PATCH /feeds/{id}` and `POST /feeds` category checks.
- **Regression test.** Use a two-connection test with the same hook point. Assert that either the push gets `rejected: category_not_found`, or the delete waits and then clears the feed's `category_id` with a monotonic `field_updated_at['category_id']`. Either way, the end state must never be "feed → deleted category".

### K-02 — `1b-T01b` lock-order test passes even when the `poll_feed` sort is reversed (checklist `T1`/`T2`)
`backend/tests/integration/test_sync_watermark_concurrency.py:119-249` runs `run_poll_feed` and `sync_push` together with `asyncio.gather`, with no barrier, and only checks the final states. Two things make the assertion trivially true:

- nothing forces the two transactions to overlap;
- `sync_push` retries `40P01` itself, so even a real deadlock ends with both writes applied.

The comment says it verifies that sync changes are emitted in `id ASC` order, but no assertion checks order.

- **Verified by reproduction.** With `matched_existing.sort(..., reverse=True)` patched into `poll_feed.py`, the test **passed 5/5 runs** (fix reverted locally, then restored).
- **Impact.** The `P-04` lock-order invariant has no regression protection. A refactor that drops the sort would pass CI.
- **Fix.** Make the test deterministic. Either capture the `UPDATE items … WHERE id = …` statement order with a `before_cursor_execute` listener during `run_poll_feed` and assert ascending IDs, or use a two-connection test with barriers and `sync_push(..., max_attempts=1)` that deadlocks if the order is wrong. Then show that it fails with the sort reverted.

### K-03 — Search builds snippets and excerpts on the event loop: ≈ 1.4 s stall per 100-row page, plus 100 serial `ts_headline` round trips (checklist `L3`)
`backend/src/smartrss/search/fts.py:348-391` runs a per-row loop. For each row it calls `prepare_headline_input()`, which runs `HTMLParser` on up to 64 KB of `raw_summary`, then `derive_lead_excerpt()`, which runs `HTMLParser` again on the same 64 KB slice. It then sends one `ts_headline` query per row. All of this runs on the API event loop.

- **Measured.** One `limit=100` page of rows with 64 KB HTML summaries and no `extracted_text` (common for feeds with full content in `<content:encoded>`) took **1.41 s of CPU** on the event loop for the Python stripping alone. During that time every other request is stalled, including `sync/push` and `pull` from other devices.
- **Secondary.** `derive_lead_excerpt` strips the whole 64 KB slice only to keep 1,500 characters. In sync hydration, at most 32 KB per item is stripped.
- **Fix.**
  - Run the per-page text preparation in `await asyncio.to_thread(...)`.
  - Build `lead_excerpt` from a smaller pre-slice, for example the first 8 KB of HTML, matching `EXTRACTED_EXCERPT_SQL_SLICE`.
  - Send one batched `ts_headline` query for the page, for example `SELECT ord, ts_headline(...) FROM unnest(CAST(:texts AS text[])) WITH ORDINALITY`.
  - Apply the same `to_thread` treatment to `rebuild_items_search_vectors_batch` in `poll_feed`. It strips up to 256 KB per new item on the worker loop, but heartbeats and the extractor share that loop.
- **Regression test.** A unit test asserting that the page builder issues one headline query, plus a timing-free check that preparation is dispatched to a thread, for example by patching `asyncio.to_thread`.

### K-04 — Scoped `item_mark_bulk_read` `UPDATE` re-evaluates its predicate instead of targeting the pre-locked candidate set (checklist `C1`, §5.1)
Step 3c (`service.py:647-672`) selects scoped candidate IDs and locks them in `id ASC` order. The actual update (`_apply_bulk_mark_read`, `service.py:1068-1087`) then runs `UPDATE items WHERE <scoped conditions>` again, without restricting it to those IDs. Under READ COMMITTED, a row that starts matching after step 3c is updated too, and locked at that moment in heap order rather than `id ASC` order. One example is an item flipped `read → unread` by a concurrent push or online write that commits in between.

- **Found by code reading.** I did not force the interleaving.
- **Impact.** Rows locked outside §5.1 order, so a possible deadlock with another writer. Push's `40P01` retry usually absorbs it, but `poll_feed` and the online routes do not retry. The returned `affected_count` and emitted events can also include rows nobody pre-validated.
- **Fix.** Keep each mutation's own candidate set from step 3c, filtered for epoch, existence and skew. Add `Item.id = ANY(CAST(:ids AS uuid[]))` to the `UPDATE` along with the existing predicate (re-checking `state = 'unread'` and the LWW guard is still right). Rows that start matching late are skipped, which is consistent with the design's "unread when applied" semantics.
- **Regression test.** A hook between step 3c and step 4 that flips a non-candidate in-scope item to `unread` and commits on another connection. Assert that the bulk mutation does not touch it and that `affected_count` equals the candidate count.

## 2. Nit

### K-05 — `payload_sha256` is not canonical across equivalent encodings
`_mutation_payload_sha256` (`service.py:406-408`) hashes `model_dump(mode="json")`. Two encodings of the same mutation therefore hash differently. **Verified:** `"2026-11-25T12:00:00Z"` and `"2026-11-25T13:00:00+01:00"` give different hashes, and so do the same UUID in upper and lower case inside `feed_set_field.value`. A client that re-serializes an outbox entry, for example after a time-zone change or with a different date library, would get `mutation_id_conflict` on a legitimate retry. **Fix:** normalize `client_timestamp` and `older_than_ts` to UTC, and coerce `feed_set_field.value` to its typed form (UUID / int / bool) before hashing. Add a unit test for both cases.

### K-06 — `1b-T12` does not check that the migration backfill matches `smartrss search reindex`
The design (§10.2 `1b-T12`, `P-14`) requires checking that the Python-builder `search_vector` backfill in `0002` matches `smartrss search reindex`. `test_1b_t12_alembic_0002_backfill_and_downgrade_round_trip` (`tests/integration/test_sync_bootstrap_and_pull.py:529`) only probes three `@@` matches. **Fix:** after `upgrade head`, save `search_vector::text`, run `reindex_all_items` against the scratch DB, and assert that the values are equal.

## 3. Pre-existing (`1a`) issue surfaced by `1b`

### K-07 — `expire_items` stamps `read_state_updated_at`/`expired_at` with the Python clock (checklist `C5`)
`backend/src/smartrss/worker/stages/expire_items.py:56` uses `datetime.now(UTC)` for `expired_at` and `read_state_updated_at`. Since `1b`, that column is the LWW comparator for `item_set_state`, which uses the DB clock (`server_now`, clamped `effective_ts`). Bulk read's guard `read_state_updated_at <= effective_ts` compares against it too. If the worker host's clock drifts ahead of PostgreSQL's, a "restore to unread" or "read" made just after expiration is wrongly `superseded`. In the Docker Compose deployment, worker and DB share the host clock, so the practical risk is low. **Fix:** use `sa.func.now()` in the `UPDATE`. **Regression test:** a static or behavioural check that the expire `UPDATE` uses the DB clock.

---

## 4. Checklist walk (diff only)

| # | Result |
| :--- | :--- |
| S1 | OK — no new library call takes fetched bytes as a path or URL. HTML stripping uses stdlib `HTMLParser.feed(str)`. |
| S2 | OK — no new outbound HTTP. |
| S3 | OK — `search reindex` reads `DATABASE_URL` from settings; nothing goes on argv. |
| S4 | N/A — no new stored URLs. |
| S5 | OK — `HTMLParser` input capped at 256 KB; `extracted_text` capped at 64 KB in SQL. |
| S6 | OK — 2 MiB push body cap enforced on `Content-Length` *and* streamed bytes; 500 mutations / 5,000 IDs; slim snapshots; `SearchItemDTO` omits `raw_summary`. |
| C1 | `K-01`, `K-04`. |
| C2 / C3 / C4 | OK — `unsubscribed → active` enqueues with `bump_existing=True` (same as `1a`). |
| C5 | OK in `1b` code (push, online feeds, categories, settings, retention use `now()`); `K-07` is pre-existing. |
| C6 | OK — auth `touch` uses a separate session factory, so the read-transaction reset in `_begin_repeatable_read_ro` cannot drop it. |
| C7 | OK — `compute_online_lww_timestamp()` on every online LWW write. |
| L1–L6 | `K-03` (L3). No new daemon loops. |
| D1 | OK — chunked at 500 or 1,000 everywhere; `UNNEST` for `ingest_sync_seq` and the item lock. |
| D2 | OK — `ITEM_SYNCED_COLUMNS` diff-gating; `search_vector`/`ingest_sync_seq` writes use `execute_synced(..., lambda _: [])`; settings PATCH is diff-gated. |
| D3 | OK — settings PATCH uses `model_fields_set` and ignores explicit `null` after a schema-level `422`. |
| D5 | OK — every raw-SQL cast uses `CAST(:p AS type)`. |
| E1–E3 | N/A. |
| T1/T2 | `K-02`, `K-06`; all other matrix rows map to tests (§6). |
| T3 | OK — four modules added to `CRITICAL_MODULES`, gate enforced. |
| T4 | OK — design status header, `docs/README.md`, `design/README.md`, `data-model.md`, ADR 0004, `.env.example`. |
| T5 | N/A — no dependency changes. |

## 5. Verification performed

**`make check`** (lint, typecheck, full suite with coverage, critical-module gate) on `070b82c`: green.

```text
============================= 158 passed in 18.87s =============================
Required test coverage of 80% reached. Total coverage: 90.84%
  [OK  ] src/smartrss/domain/sync_merge.py              100.00%
  [OK  ] src/smartrss/domain/fts_text.py                 97.64%
  [OK  ] src/smartrss/sync/service.py                    94.35%
  [OK  ] src/smartrss/search/fts.py                      98.93%
  (all 12 `1a` critical modules also >= 90%)
All critical core modules meet the >= 90% coverage gate.
```

**Reproductions.** All temporary; the tree was restored, and `git status` was clean afterwards.

| Finding | Method | Result |
| :--- | :--- | :--- |
| `K-01` | Temporary integration test: monkeypatched `_validate_feed_field_value` soft-deletes the target category on a second connection after step 3a | Push `applied`; `feeds.category_id` = deleted category (`deleted_at` set) — **reproduced** |
| `K-02` | `matched_existing.sort(..., reverse=True)` in `poll_feed.py`, then ran `test_1b_t01b_…` 5× | 5/5 **passed** with the fix reverted — test does not detect the regression |
| `K-03` | `prepare_headline_input` + `derive_lead_excerpt` over 100 × 64 KB HTML summaries | **1.41 s** CPU, synchronous |
| `K-05` | Hash of the same mutation with `Z` vs `+01:00`, and with an upper- vs lower-case UUID value | Hashes differ in both cases |
| Isolation (no finding) | `_begin_repeatable_read_ro` then `SHOW transaction_isolation`, `SHOW transaction_read_only` | `repeatable read`, `on` — as designed |

`K-04` and `K-07` were found by code reading.

## 6. Test-matrix mapping (§10.2)

| Row | Test | Notes |
| :--- | :--- | :--- |
| `1b-T01` | `test_1b_t01a_gap_free_concurrent_commit_sync_seq_pull`, `test_1b_t01b_poll_feed_sorted_item_updates_and_concurrent_push_no_deadlock` | `T01a` is barrier-driven and sound; `T01b` → `K-02` |
| `1b-T02` | `test_1b_t02a_*` (3 hypothesis tests), `test_clock_skew_clamp_dead_rtc_and_naive_rejection`, `test_1b_t02b_real_postgres_hypothesis_mixed_batch_permutations` | OK |
| `1b-T03` | `test_1b_t03_bootstrap_pagination_working_set_and_mid_bootstrap_writes` | OK |
| `1b-T04` | `test_1b_t04_pull_coalescing_cross_entity_push_pull_and_resync_reasons` | OK |
| `1b-T05` | `test_1b_t05_push_idempotency_payload_hash_concurrent_retry_and_expired_transitions`, `test_1b_push_edge_cases_retries_replay_snapshots_and_schema_validation` | OK |
| `1b-T06` | `test_1b_t06_bounded_mark_bulk_read_and_two_order_regression` | OK |
| `1b-T07` | `test_1b_t07_online_lww_monotonicity_config_version_poll_interval_and_status` | OK |
| `1b-T08` | `test_1b_t08_benchmarks_500_push_response_size_and_20k_bulk_read` | OK |
| `1b-T09` | `test_1b_t09a_…`, `test_1b_t09b_p11_f1_…` | OK |
| `1b-T10` | `test_1b_t10_fts_auth_weights_queries_filters_and_xss_safe_snippets`, `test_1b_t10_poll_extract_hooks_and_search_reindex_cli`, `tests/unit/test_fts_text.py` | OK |
| `1b-T11` | `test_1b_t11_online_settings_api_and_retention_cleanup` | OK |
| `1b-T12` | `test_1b_t12_alembic_0002_backfill_and_downgrade_round_trip` | Missing backfill = reindex check → `K-06` |

## 7. Resolution

All 7 findings (`K-01`–`K-07`, including both nits and the pre-existing `1a` clock issue `K-07`) have been fixed with proven regression tests and synchronized into [`docs/design/1b-sync-protocol-and-search.md`](../design/1b-sync-protocol-and-search.md) §11 and [`docs/README.md`](../README.md).

- **K-01** — Fixed. Added `.with_for_update(read=True)` (`FOR SHARE`, `ORDER BY id ASC`) to active-category lookups in `sync/push` step 3a (`backend/src/smartrss/sync/service.py:622-638`), `POST /api/v1/feeds` (`backend/src/smartrss/api/routes/feeds.py:349-361`), `PATCH /api/v1/feeds/{id}` moved **before** locking `Feed FOR UPDATE` to strictly preserve §5.1 `categories → feeds` lock ordering (`feeds.py:480-496`), and `POST /api/v1/feeds/import-opml` (`feeds.py:160-171`).
  Tests: `test_k01_category_delete_race_never_leaves_feed_on_deleted_category` and `test_k01_online_patch_and_post_feed_lock_category_for_share_before_feed` (`backend/tests/integration/test_sync_1b_review_regressions.py`).
  Evidence: reverted `.with_for_update(read=True)` in `sync/service.py` → test failed (`AssertionError: assert UUID(...) is None`, feed pointed to deleted category); reverted in `feeds.py` → online lock-order test failed (`assert ['feeds:FOR UPDATE'] == ['categories:FOR SHARE', 'feeds:FOR UPDATE']`); restored both, passed. Fixed lines covered.
- **K-02** — Fixed. Added deterministic SQL-capture regression test `test_k02_poll_feed_updates_matched_existing_items_in_strict_id_asc_order` (`test_sync_1b_review_regressions.py`) and upgraded `test_1b_t01b_poll_feed_sorted_item_updates_and_concurrent_push_no_deadlock` (`test_sync_watermark_concurrency.py`) to capture `UPDATE items SET title` IDs via `before_cursor_execute` and call `sync_push(..., max_attempts=1)`.
  Evidence: patched `matched_existing.sort(..., reverse=True)` in `poll_feed.py:760` → test failed (`assert [high_id, low_id] == [low_id, high_id]`); restored, passed. Fixed lines covered.
- **K-03** — Fixed. Offloaded per-page HTML stripping, `lead_excerpt` derivation (pre-sliced to `8,192` chars in `domain/sync_merge.py:127-140`), and headline fragment parsing to `asyncio.to_thread()` (`backend/src/smartrss/search/fts.py:114-141, 385-413`); replaced the per-row `ts_headline` loop with a single batched `SELECT u.ord, ts_headline(...) FROM unnest(CAST(:texts AS text[])) WITH ORDINALITY` query (`fts.py:372-411`); and offloaded HTML stripping in `rebuild_item_search_vector` and `rebuild_items_search_vectors_batch` to `asyncio.to_thread()` (`fts.py:157-184`).
  Test: `test_k03_search_batches_headlines_and_offloads_text_prep_to_thread` (`test_sync_1b_review_regressions.py`).
  Evidence: bypassed `asyncio.to_thread` in `fts.py` → test failed (`search_thread_calls < 2`); restored, passed (`headline_queries == 1` for 5 matched rows, `to_thread` exercised on both search and vector rebuilds). Fixed lines covered (`sync_merge.py` 100.00%, `fts.py` 99.03%).
- **K-04** — Fixed. Retained each scoped bulk mark-read mutation's own candidate IDs from step 3c (`scoped_candidates_by_idx`, `sync/service.py:665-681`), combined with `explicit_item_ids` (all pre-locked in `id ASC` order), and bounded `_apply_bulk_mark_read`'s `UPDATE` with `items.id = ANY(CAST(:prelocked_ids AS uuid[]))` (short-circuiting with `affected_count = 0` if empty; `sync/service.py:894-905, 1097-1106`).
  Test: `test_k04_scoped_bulk_read_update_restricted_to_prelocked_candidates` (`test_sync_1b_review_regressions.py`).
  Evidence: removed the `ANY(CAST(:prelocked_ids AS uuid[]))` filter → test failed (`AssertionError: assert 2 == 1`, late-arriving item B was wrongly marked read); restored, passed. Fixed lines covered (100% branch+line on the added guard).
- **K-05** — Fixed. Canonicalized `client_timestamp` and `older_than_ts` to UTC ISO-8601 and coerced `feed_set_field(category_id)` UUID strings to lowercase before hashing in `_mutation_payload_sha256()` (`sync/service.py:407-426`).
  Test: `test_k05_payload_sha256_canonical_across_tz_and_uuid_case` (`backend/tests/unit/test_sync_merge_properties.py`).
  Evidence: reverted UTC normalization → test failed (`AssertionError: assert '04902d...' == '989451...'`); restored, passed. Fixed lines covered.
- **K-06** — Fixed. Extended `test_1b_t12_alembic_0002_backfill_and_downgrade_round_trip` (`backend/tests/integration/test_sync_bootstrap_and_pull.py:583-628`) to capture `search_vector::text` right after `upgrade head`, run `reindex_all_items` against the scratch DB, and assert exact `backfilled_sv_text == reindexed_sv_text`.
  Evidence: substituted mismatched summary text in `0002` backfill → test failed (`AssertionError` on `tsvector` equality); restored, passed.
- **K-07** — Fixed. Replaced Python `datetime.now(UTC)` with PostgreSQL `sa.func.now()` for `expired_at`, `read_state_updated_at`, and `updated_at` in `run_expire_items()` (`backend/src/smartrss/worker/stages/expire_items.py:66-70`).
  Test: `test_k07_expire_items_uses_db_clock_not_python_clock` (`test_sync_1b_review_regressions.py`).
  Evidence: reverted to `datetime.now(UTC)` → test failed (`AssertionError: assert 'expired_at=now()' in 'update items set ... expired_at=%(expired_at)s...'`); restored, passed. Fixed lines covered (`expire_items.py` 0 missed statements, 96.67%).

### `make check` Summary
- `ruff check`, `ruff format --check`, `mypy src` (`strict = true`): all clean (53 source files, 83 files formatted).
- `pytest`: **165 passed**, total branch+line coverage **91.12%** (gate ≥ 80%).
- Critical-module coverage gate: **16/16 modules ≥ 90%** (`sync_merge.py` 100.00%, `search/fts.py` 99.03%, `fts_text.py` 97.64%, `expire_items.py` 96.67%, `sync_log.py` 95.93%, `sync/service.py` 94.53%, `poll_feed.py` 91.57%; lowest overall `security/middleware.py` 90.80%).
