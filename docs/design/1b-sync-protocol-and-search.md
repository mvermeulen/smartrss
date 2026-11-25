# Component Design 1b: Server Sync Engine, Conflict Resolution & Full-Text Search

> **Status:** **Approved** by project owner 2026-11-25 *(after [`1b` design review](../reviews/2026-11-25-1b-design-review.md): findings `P-01`–`P-17` and follow-up `P-11-F1` verified; owner decisions `Q-1`–`Q-4` and residual risks `RR-1`/`RR-2` accepted — see §12)*
> **Milestone:** `1b` — Server Sync Engine, Conflict Resolution & Search ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-STATE-01`–`02` (sync/restore transitions), `FR-STATE-03` (bounded bulk read; cluster read in `1e`), `FR-STATE-04` (partial: full-text archive search over title/author/summary/body; enrichment TL;DR/takeaways/tags in `1d`), `FR-SYNC-01`–`04`, `NFR-SYNC-01`, `NFR-SYNC-02` (500-mutation `< 5s` flush), `NFR-SEC-01`, `NFR-SEC-03` (XSS-safe search snippets)
> **Related ADRs:** [ADR 0002](../decisions/0002-native-sync-api-and-detached-client-model.md), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)
> **Carry-over Design Items Resolved:** `S-04` (`feeds.config_version` independence from `field_updated_at`), `S-07` (bounded cursor-paginated `bootstrap`) from [Second-Pass Design Review](../reviews/2026-11-23-second-pass-design-review.md)

---

## 1. Scope & Boundaries

Milestone `1b` builds the server-side synchronization engine, per-field conflict resolution, idempotent mutation ingestion, online settings/feed LWW alignment, and PostgreSQL full-text archive search on top of Milestone `1a`'s ingestion, auth, and `sync_state`/`sync_changes` foundation.

### In Scope for `1b`
1. **Alembic Migration `0002_milestone_1b_sync_and_search`**:
   - New `sync_mutations` table (idempotency log of client mutations with `payload_sha256` conflict guard, pruned after 90 days).
   - `sync_state.pruned_through_seq BIGINT NOT NULL DEFAULT 0` for exact retention-boundary (`resync_required`) detection.
   - `items.ingest_sync_seq BIGINT NOT NULL DEFAULT 0` (indexed) for pruning-proof, commit-ordered `mark_bulk_read`.
   - `items.search_vector TSVECTOR` + GIN index, backfilled via the shared Python `HTMLParser` builder.
2. **Sync Engine (`/api/v1/sync/*`)**:
   - `GET /api/v1/sync/bootstrap`: Stateless keyset-paginated snapshot pinned to Page 1's `watermark` and `instance_epoch`, returning `server_now` and a forward-compatible aggregate wire envelope.
   - `GET /api/v1/sync/pull`: Commit-ordered delta stream from `sync_changes` with within-page coalescing, bounded-column bulk hydration, tombstone propagation, `server_now`, and exact `resync_required` detection (`epoch_mismatch`, `cursor_ahead`, `cursor_pruned`).
   - `POST /api/v1/sync/push`: Single-transaction batch mutation processor (up to 500 mutations / 5,000 explicit `item_ids` / 2 MiB body, `< 5s` target) with transient-error server retry (`40P01`/`40001`/`23505`), `client_mutation_id` + `payload_sha256` idempotency, server clock-skew clamping (`+5 min`) & dead-RTC rejection (`< server_now - 90d`), deterministic value-rank LWW tie-breaking, bounded `mark_bulk_read`, and per-mutation `{applied | superseded | rejected}` outcomes with slim entity snapshots.
3. **Online Settings & LWW Consistency**:
   - `GET /api/v1/settings` and `PATCH /api/v1/settings` (online-only `app_settings` management emitting `app_settings` `upsert` in `sync_changes`).
   - Align `PATCH /api/v1/feeds/{id}`, `POST /api/v1/feeds` reactivation, and `DELETE /api/v1/categories/{id}` to use PostgreSQL `now()` (`C5`), monotonic `GREATEST(db_now, stored_ts + 1µs)` LWW timestamps, granular `research_policy.enabled` / `research_policy.fetch_pdfs` keys, shared status-transition side effects (`unsubscribed → active` reset + poll enqueue; `paused → active|muted`), and `config_version` incrementing **only** on `curation_instructions` writes (`S-04`).
4. **PostgreSQL Full-Text Archive Search**:
   - `GET /api/v1/search` (authenticated): Full-archive search via `websearch_to_tsquery` (with `querytree = 'T'` negation-only guard), weighted `search_vector` (`A: title`, `B: author`, `C: HTML-stripped raw_summary`, `D: extracted_text[:65536]`), facet filters, `relevance` / `newest` sorting, slim `SearchItemDTO` results, and **structured XSS-safe snippet fragments** (`list[list[SnippetFragment]]`) computed on the returned page only.
   - Application-code `search_vector` maintenance hooks in `poll_feed` and `extract_content`, plus `smartrss search reindex` CLI command.
5. **1a Pipeline & Maintenance Updates**:
   - `SyncLogWriter.record()` uses `clock_timestamp()` (monotonic with `seq` under `sync_state FOR UPDATE`) and chunks large multi-row `INSERT`s to stay well below PostgreSQL's 65,535 bind-parameter cap (`D1`).
   - `FEED_SYNCED_COLUMNS` adds `poll_interval_minutes` so online poll-interval edits emit a `feed` `upsert`; `ITEM_SYNCED_COLUMNS` excludes internal-only `Item` columns (`search_vector`, `ingest_sync_seq`, `updated_at`).
   - `poll_feed` sorts `matched_existing` by `item.id ASC` before per-item updates (honoring §5.1 lock order) and stamps `items.ingest_sync_seq` from the allocated `sync_changes.seq` inside the same final write transaction.
   - `retention_cleanup` updates `app_settings` before `sync_state` (honoring §5.1 lock order), prunes `sync_changes` strictly by a contiguous `seq` prefix, updates `sync_state.pruned_through_seq` atomically, and prunes `sync_mutations` older than 90 days.
6. **Verification & Benchmarks**:
   - Real-Postgres concurrent-commit `sync_seq` gap-free & deadlock-free lock-order tests, unit + real-Postgres `hypothesis` permutation convergence tests (including tied/clamped timestamps and bulk mark-read), 500-mutation & 20,000-item bulk-read benchmarks (`< 5s`), and full restore-epoch end-to-end sync recovery drill (covering both pending outbox and 48h acknowledged replay journal).

### Explicitly Out of Scope for `1b`
- React/TypeScript/Dexie client UI, Service Worker, local outbox, and 48h acknowledged replay journal implementation (`1c`).
- Database tables for `item_enrichments`, `tags`, `item_tags`, `interest_profiles`, `prompt_configs` (`1d`).
- Database tables for `clusters`, `cluster_exclusions`, `item_embeddings` and `split_cluster` mutation execution (`1e`).
- Database table for `research_sources` (`1f`).
- Database table for `feedback_events` and `item_feedback` / `feed_instruction_*` mutation execution (`1g`).

---

## 2. Key `1b` Design Trade-offs & Decisions

All seven trade-offs below were reviewed and confirmed with the project owner on 2026-11-25 (with `1b-D04` refined per owner decision `Q-2` on `P-01`).

| # | Decision Area | Chosen Approach | Alternative(s) Considered | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **1b-D01** *(S-07)* | **Bootstrap pagination & snapshot consistency** *(Confirmed)* | **Stateless keyset pagination pinned to Page 1's `watermark`**: Page 1 captures `{watermark, instance_epoch, server_now}` first and returns metadata + first item page (`limit=500`, `ORDER BY id ASC`); Pages 2..N fetch remaining items by `cursor`; client then runs `sync/pull?since_seq=<page1_watermark>`. | (a) Single unpaginated response with hard cap; (b) server-side frozen snapshot session. | Zero server state, bounded HTTP/memory footprint even for 20k+ items, and mathematically gap-free because any write after Page 1's watermark has `seq > watermark` and is replayed idempotently by the trailing `pull`. |
| **1b-D02** | **Item sync payload scope (`extracted_text` & `raw_summary`)** *(Confirmed)* | **Metadata + capped `raw_summary` (32,768 chars + `summary_truncated`) + derived `lead_excerpt` (1,500 chars plain text); omit full `extracted_text`**. SQL hydration projects bounded `LEFT(...)` slices only. | (a) Truncated 8–16 KB `extracted_text` in sync; (b) full `extracted_text` in sync. | Keeps 2,000-item client cache at ~4–8 MB (ideal for Ubuntu Dexie and Phase 2 Android), avoids pulling 15 MB of TOASTed bodies per page into the API process, respects ADR 0002's metadata-only boundary, and powers detached search while full-archive body search runs via Postgres FTS. |
| **1b-D03** | **`sync/pull` coalescing & `resync_required` detection** *(Confirmed)* | **Within-page coalescing** (fetch `LIMIT + 1` raw rows, advance `next_seq` to max raw `seq`, collapse per `(entity_type, entity_id)` keeping highest `seq`/`op`) + **`sync_state.pruned_through_seq`** returning HTTP `200` `{resync_required: true, resync_reason}`. | (a) Uncoalesced raw log replay; (b) `MIN(sync_changes.seq)` check + HTTP `410`. | Avoids 3–4× redundant serialization per item; `pruned_through_seq` is 100% deterministic even if `sync_changes` becomes empty after 90 idle days; HTTP `200` distinguishes expected recovery from transport errors. |
| **1b-D04** | **`sync/push` atomicity & equal-timestamp LWW tie-breaking** *(Confirmed; refined per `Q-2`)* | **Single DB transaction per push batch** (max 500 mutations, transient-error retry up to 3 attempts, pre-validated so bad mutations record `rejected` without aborting the batch, single `SyncLogWriter.record()` at commit) + **deterministic value-rank tie-breaker** on `effective_ts == stored_ts`. | (a) One transaction per mutation; (b) `last_mutation_id` columns; (c) naive `>=` / `>`. | Single transaction clears the `< 5s` 500-mutation target (~50–150 ms) with minimal `sync_state` lock contention; value-rank tie-breaking guarantees strict arrival-order-independent convergence for single-field LWW mutations (`item_set_state`, `item_set_starred`, `feed_set_field`) and explicit `scope='items'` bulk mark-read over `unread`/`read` items. Scoped bulk mark-read (`all`/`feed`/`category`) intentionally filters `state = 'unread'` only as an owner-accepted non-commuting carve-out to avoid archive-wide write/sync amplification (see §6.5). |
| **1b-D05** | **Bounded `mark_bulk_read` (`up_to_sync_seq`)** *(Confirmed)* | **Add `items.ingest_sync_seq BIGINT NOT NULL DEFAULT 0`** (indexed; stamped by `poll_feed` from its allocated `sync_changes.seq`), supporting both epoch-pinned scoped `{scope, up_to_sync_seq, epoch, older_than_ts?}` and epoch-agnostic explicit `item_ids[]`. | (a) Explicit `item_ids[]` only; (b) `fetched_at <= client_ts`. | Survives 90-day `sync_changes` pruning permanently, avoids huge UUID arrays for "Mark all read", rejects stale pre-restore sequence bounds on `epoch_mismatch` (`P-11-F1`), and gives exact commit-order bounds immune to clock skew. |
| **1b-D06** | **Forward-compatible sync surface vs. stub tables** *(Confirmed)* | **Finalize the forward-compatible JSON wire envelope in `1b`** (`clusters: []`, `interest_profile: null`, item `enrichment: null` / `tags: []` / `research_sources: []` / `user_feedback: null`, feed `active_feed_instructions: []`); **add only `1b`-owned DB tables/columns** now and defer `1d`–`1g` tables to their milestones. | Create empty stub tables for `1d`–`1g` in `1b`. | Gives `1c` a stable TypeScript/Dexie contract without locking `1d`–`1g` schemas before their component design reviews. |
| **1b-D07** | **Full-text search snippets & query parsing** *(Confirmed)* | **`websearch_to_tsquery` + structured XSS-safe snippet fragments** (`list[list[{text, highlight}]]`) built via `ts_headline` with `STX`/`ETX`/`EOT` control-character sentinels on the returned page only; HTML-stripped `raw_summary` and 64 KB `extracted_text` slice in `search_vector`. | (a) Raw HTML `<mark>` snippets; (b) no snippets; (c) `plainto_tsquery`. | Natural query UX without syntax-error crashes, zero HTML emitted by the search API (zero XSS surface even for articles discussing `<script>` tags), unambiguous fragment boundaries, and fast page-scoped headline generation. |

---

## 3. Repository & Module Additions

Building directly on `1a`'s layout (`backend/src/smartrss/`):

```text
backend/
├── alembic/versions/
│   ├── 0001_milestone_1a_core.py            # Existing (unchanged)
│   └── 0002_milestone_1b_sync_and_search.py # New reversible migration (sync_mutations, pruned_through_seq, ingest_sync_seq, search_vector + GIN)
├── src/smartrss/
│   ├── config.py                            # Add FTS_LANGUAGE (allow-listed Postgres text search config, default "english")
│   ├── db/
│   │   ├── models.py                        # Add SyncMutation ORM model; add pruned_through_seq, ingest_sync_seq, search_vector columns
│   │   └── sync_log.py                      # clock_timestamp(), chunked inserts (D1), ITEM_SYNCED_COLUMNS, poll_interval_minutes in FEED_SYNCED_COLUMNS
│   ├── domain/
│   │   ├── sync_merge.py                    # Pure LWW clock-skew clamp & lower-bound check, value-rank tie-breakers, monotonic online LWW helper, shared feed status transition helper, payload hash, HTML stripper, lead_excerpt & summary truncation
│   │   └── fts_text.py                      # Pure FTS language allow-list, structured snippet parser (STX/ETX/EOT sentinels -> list[list[Fragment]])
│   ├── sync/
│   │   ├── __init__.py
│   │   ├── schemas.py                       # Sync wire DTOs (bootstrap, pull, push discriminated union, slim push snapshots, forward-compatible aggregates)
│   │   └── service.py                       # Bootstrap pagination, bounded-slice pull coalescing + hydration, retrying single-tx push engine & bulk mark-read
│   ├── search/
│   │   ├── __init__.py
│   │   └── fts.py                           # rebuild_item_search_vector(), bulk reindex helper, GET /api/v1/search SQL execution
│   ├── api/
│   │   ├── app.py                           # Mount sync, search, and settings routers
│   │   ├── schemas.py                       # Add AppSettingsResponse / AppSettingsUpdateRequest & SearchItemDTO / search response DTOs
│   │   └── routes/
│   │       ├── categories.py                # DELETE /categories/{id}: DB clock + monotonic field_updated_at['category_id']
│   │       ├── feeds.py                     # DB clock + monotonic granular field_updated_at (S-04) + shared status-transition helper
│   │       ├── settings.py                  # New: GET /api/v1/settings, PATCH /api/v1/settings
│   │       ├── sync.py                      # New: GET /bootstrap, GET /pull, POST /push (2 MiB body cap)
│   │       └── search.py                    # New: GET /api/v1/search (authenticated)
│   ├── worker/stages/
│   │   ├── poll_feed.py                     # Sort matched_existing by item.id ASC (§5.1) + stamp items.ingest_sync_seq + rebuild search_vector
│   │   ├── extract_content.py               # Rebuild search_vector when extracted_text is written
│   │   └── maintenance.py                   # Lock-ordered retention cleanup (app_settings before sync_state) + contiguous-prefix sync_changes prune + sync_mutations 90d prune
│   └── cli.py                               # Add `smartrss search reindex` CLI command
└── tests/
    ├── unit/
    │   ├── test_sync_merge_properties.py    # Hypothesis permutation convergence, clock-skew clamp/lower-bound, online monotonic LWW, tie-breakers, HTML strip / truncation
    │   └── test_fts_text.py                 # FTS language allow-list, HTML stripping, XSS-safe multi-fragment snippet parsing
    └── integration/
        ├── test_sync_watermark_concurrency.py # Real-Postgres interleaved commit-order gap-free pull test + poll_feed/push lock-order deadlock test
        ├── test_sync_bootstrap_and_pull.py  # Multi-page bootstrap + mid-bootstrap writes, coalescing & cross-entity ordering, tombstones, resync_required (all 3 reasons), push->pull two-device test
        ├── test_sync_push_and_conflicts.py  # Idempotency, payload_sha256 mismatch, concurrent duplicate push retry, LWW item/feed/online merges, expired->unread, bulk mark-read, real-PG hypothesis permutations, 500-mutation & 20k bulk-read benchmarks
        ├── test_sync_restore_epoch_drill.py # Simulated DB restore rewind + epoch rotation + pending outbox & 48h acknowledged replay journal recovery
        └── test_search_fts.py               # Weighted ranking, websearch phrases/negation/negation-only 422, stop-words, filters, XSS-safe multi-fragment snippets, poll/extract hooks, reindex CLI
```

### Configuration Additions (`config.py` & `.env.example`)
| Variable | Default | Validation / Notes |
| :--- | :--- | :--- |
| `FTS_LANGUAGE` | `english` | Must be in explicit allow-list `{"english", "simple", "danish", "dutch", "finnish", "french", "german", "hungarian", "italian", "norwegian", "portuguese", "romanian", "russian", "spanish", "swedish", "turkish"}`. Never interpolated raw into SQL; bound via `CAST(:fts_cfg AS regconfig)`. Changing it requires running `smartrss search reindex`. |

---

## 4. Database Schema & Migration (`0002_milestone_1b_sync_and_search.py`)

Migration `0002` is strictly reversible (`downgrade()` drops the table, indexes, and added columns cleanly).

### 4.1 New Table: `sync_mutations`
| Column | Type | Constraints / Notes |
| :--- | :--- | :--- |
| `client_mutation_id` | `UUID` | Primary key |
| `device_id` | `UUID` | `NOT NULL`, `FK → devices.id` (devices are soft-revoked via `revoked_at`, never hard-deleted) |
| `mutation_type` | `TEXT` | `NOT NULL` (`item_set_state`, `item_set_starred`, `item_mark_bulk_read`, `feed_set_field`, …) |
| `entity_type` | `TEXT` | `NOT NULL` (`item`, `feed`, `cluster`, `bulk`) |
| `entity_id` | `UUID` | `NULL` (populated for single-entity mutations and feed/category-scoped bulk mutations; `NULL` for `scope='all'` or `scope='items'`) |
| `payload_sha256` | `TEXT` | `NOT NULL` (SHA-256 hex digest of canonical JSON mutation payload; detects same-device reuse of a `client_mutation_id` with a different payload) |
| `client_timestamp` | `TIMESTAMPTZ` | `NOT NULL` |
| `effective_timestamp` | `TIMESTAMPTZ` | `NOT NULL` (clamped: `LEAST(client_timestamp, server_now + INTERVAL '5 minutes')`) |
| `server_applied_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` |
| `outcome` | `TEXT` | `NOT NULL CHECK (outcome IN ('applied', 'superseded', 'rejected'))` |
| `rejection_reason` | `TEXT` | `NULL` |
| `affected_count` | `INT` | `NULL` (records number of modified rows for `item_mark_bulk_read` so idempotent replay returns the exact count) |

- **Index**: `ix_sync_mutations_applied_at ON sync_mutations (server_applied_at)` for 90-day retention pruning.

### 4.2 Column & Index Additions on Existing Tables

1. **`sync_state`**:
   - Add `pruned_through_seq BIGINT NOT NULL DEFAULT 0` (`CHECK (pruned_through_seq >= 0)`).
2. **`items`**:
   - Add `ingest_sync_seq BIGINT NOT NULL DEFAULT 0` (`CHECK (ingest_sync_seq >= 0)`).
   - Index: `ix_items_unread_ingest_seq ON items (feed_id, ingest_sync_seq) WHERE state = 'unread'` — directly accelerates scoped `item_mark_bulk_read` for both `feed` and `all` scopes.
   - Add `search_vector TSVECTOR NULL`.
   - Index: `ix_items_search_vector ON items USING GIN (search_vector)`.
   - Index for bootstrap keyset pagination & working-set filtering:
     - `ix_items_bootstrap_unread_id ON items (id) WHERE state = 'unread'`
     - `ix_items_bootstrap_starred_id ON items (id) WHERE is_starred = TRUE`
     - `ix_items_bootstrap_read_at_id ON items (read_at DESC, id) WHERE state = 'read'`
     *(These small partial indexes make `GET /api/v1/sync/bootstrap` fast even when the permanent archive grows to hundreds of thousands of `expired` and old `read` items.)*

### 4.3 Migration `0002` Backfill Rules
- **`items.ingest_sync_seq`**: Backfill existing rows with the minimum `sync_changes.seq` for `(entity_type = 'item', entity_id = items.id)`, defaulting to `0` if no `sync_changes` row exists (so pre-migration items are always included in any `up_to_sync_seq >= 0` bulk mark-read). *(Note: if early insert events were already pruned before `0002`, `MIN(seq)` selects the earliest remaining update event; this approximation is harmless prior to deployment and never affects post-`0002` items stamped at insert time.)*
- **`items.search_vector`**: Migration `0002` resolves `fts_cfg = validate_fts_language(os.environ.get("FTS_LANGUAGE", "english"))` (using the allow-list in `domain/fts_text.py`) and backfills existing `items` rows in 500-row batches using the **same Python `HTMLParser` summary stripper and `extracted_text[:65536]` slicer** as `rebuild_item_search_vector()` (with `CAST(:fts_cfg AS regconfig)`). This guarantees backfilled lexemes are byte-for-byte identical to rows produced by `poll_feed`, `extract_content`, and `smartrss search reindex`, without diverging on `<script>`/`<style>` blocks or HTML entities.

---

## 5. Sync Log, Locking & Retention Invariants

### 5.1 Global Lock Ordering (Deadlock Prevention)
All database transactions across API and worker MUST acquire row locks in this strict hierarchy:
1. `categories` (by `id ASC`)
2. `feeds` (by `id ASC`)
3. `items` (by `id ASC`)
4. `jobs` (active dedupe target / `id ASC`, e.g. `enqueue_job` during `unsubscribed → active`)
5. `app_settings` (`id = 1`)
6. `sync_state` (`id = 1`, **always last**, immediately before `COMMIT`)

No slow I/O (HTTP fetch, subprocess extraction, or future LLM inference) may ever occur inside a transaction holding any of these locks (`ADR 0004`, `S-02`).

### 5.2 `SyncLogWriter` Hardening (`db/sync_log.py`)
- **Monotonic `changed_at`**: `SyncLogWriter.record()` explicitly writes `changed_at = sa.func.clock_timestamp()` (instead of transaction-start `now()`) while holding `sync_state FOR UPDATE`, ensuring `changed_at` is monotonically non-decreasing with `seq`.
- **Chunked Inserts (`D1`)**: When `len(events) > 1000` (e.g., a large `mark_bulk_read`), `SyncLogWriter.record()` inserts into `sync_changes` in chunks of `1000` rows (4,000 bind parameters per chunk, far below PostgreSQL's 65,535 limit) within the same transaction while holding the single `sync_state FOR UPDATE` lock.
- **`FEED_SYNCED_COLUMNS` & `ITEM_SYNCED_COLUMNS`**:
  - Add `poll_interval_minutes` to `FEED_SYNCED_COLUMNS` so `PATCH /api/v1/feeds/{id}` updating `poll_interval_minutes` emits a `feed` `upsert` in `sync_changes` (keeping every field in `SyncFeedDTO` backed by a sync event). `last_fetched_at` and `last_success_at` intentionally stay **excluded** from both `FEED_SYNCED_COLUMNS` and `SyncFeedDTO` so polling never floods `sync_changes` and clients never display arbitrarily stale poll timestamps.
  - Add explicit `ITEM_SYNCED_COLUMNS` to `_entity_requires_sync()` (excluding server-only `search_vector`, `ingest_sync_seq`, `updated_at`), matching `FEED_SYNCED_COLUMNS` and `APP_SETTINGS_SYNCED_COLUMNS`.

### 5.3 `poll_feed` Lock-Order & `ingest_sync_seq` Stamping
In `poll_feed.py`'s final short write transaction:
1. **Sorted item updates (§5.1)**: Sort `matched_existing` by `item.id ASC` before issuing per-item `UPDATE`s so `poll_feed` strictly honors the `items (id ASC)` lock order when running concurrently with `POST /api/v1/sync/push`.
2. **Stamping `ingest_sync_seq`**: Immediately after `seqs = await sync_log.record(session, all_events)` returns:
   - Map each newly inserted `item_id` (from `inserted_ids`) to its allocated `seq` by zipping `all_events` with `seqs` and filtering to `event.entity_type == "item" and event.entity_id in inserted_id_set` *(never indexing `seqs` by position in `inserted_ids`, because `all_events` prepends the feed event and interleaves updated-item events)*.
   - Execute a single `UNNEST`-based `UPDATE` via `execute_synced(..., lambda _res: [])` using standard `CAST(...)` bind syntax (`D5`, 2 bind parameters total regardless of batch size):
     ```sql
     UPDATE items
     SET ingest_sync_seq = v.seq
     FROM (
       SELECT unnest(CAST(:ids AS uuid[])) AS id,
              unnest(CAST(:seqs AS bigint[])) AS seq
     ) AS v
     WHERE items.id = v.id
     ```
   - Existing items updated via `entry_hash` change keep their original `ingest_sync_seq` (their initial ingest order never regresses).

### 5.4 Contiguous-Prefix Retention Cleanup (`worker/stages/maintenance.py`)
To eliminate any possibility of holes in `sync_changes`, keep `pruned_through_seq` exact, and honor the §5.1 lock hierarchy (`app_settings` before `sync_state`):
1. Update `app_settings.last_retention_cleanup_at = now()` (internal operational column, zero sync events — acquires `app_settings` lock before `sync_state`).
2. Delete `sync_mutations` older than 90 days:
   ```sql
   DELETE FROM sync_mutations WHERE server_applied_at < now() - INTERVAL '90 days'
   ```
3. Compute the contiguous prefix cutoff inside the same `retention_cleanup` transaction:
   ```sql
   SELECT MAX(seq) FROM sync_changes WHERE changed_at < now() - INTERVAL '90 days'
   ```
4. If `cutoff_seq IS NOT NULL`:
   - Delete strictly by prefix: `DELETE FROM sync_changes WHERE seq <= :cutoff_seq`.
   - Atomically advance `sync_state` **last**:
     ```sql
     UPDATE sync_state
     SET pruned_through_seq = GREATEST(pruned_through_seq, :cutoff_seq),
         updated_at = now()
     WHERE id = 1
     ```
     *(Acquiring `sync_state` strictly last respects §5.1 and prevents deadlocks with `PATCH /api/v1/settings` and races with concurrent `sync/pull` reads.)*

---

## 6. Sync Wire Protocol (`/api/v1/sync/*`)

All `/api/v1/sync/*` endpoints require a valid per-device bearer token (`Authorization: Bearer <token>`) and are protected by `1a`'s `Host` allow-list, CORS allow-list, and JSON-only mutation middleware (`ADR 0006`).

### 6.1 Canonical Synced Entity DTOs (Forward-Compatible Envelope)

All timestamps serialize as ISO-8601 UTC (`Z` / `+00:00`).

#### `SyncCategoryDTO`
```json
{
  "id": "uuid",
  "name": "string",
  "sort_order": 0,
  "updated_at": "2026-11-25T12:00:00Z"
}
```

#### `SyncFeedDTO`
```json
{
  "id": "uuid",
  "category_id": "uuid | null",
  "feed_url": "https://...",
  "site_url": "https://... | null",
  "title": "string",
  "status": "active | paused | muted | unsubscribed",
  "health_state": "healthy | degraded | broken | gone",
  "poll_interval_minutes": 60,
  "consecutive_errors": 0,
  "last_error": "string | null",
  "expiration_days_override": "int | null",
  "fetch_full_text": true,
  "research_policy": {
    "enabled": false,
    "fetch_pdfs": false,
    "guidance": "",
    "max_hops": 1
  },
  "curation_instructions": "",
  "active_feed_instructions": [],
  "config_version": 1,
  "field_updated_at": {
    "status": "2026-11-25T12:00:00Z"
  },
  "updated_at": "2026-11-25T12:00:00Z"
}
```
*(In `1b`, `active_feed_instructions` is always `[]`; populated by `1g`. Operational poll timestamps `last_fetched_at` and `last_success_at` are intentionally omitted from `SyncFeedDTO` and served online-only via `GET /api/v1/feeds` and System Health so every synced feed field is backed by `FEED_SYNCED_COLUMNS`.)*

#### `SyncItemDTO` *(Metadata-Only Aggregate — Decision `1b-D02` & `1b-D06`)*
```json
{
  "id": "uuid",
  "feed_id": "uuid",
  "cluster_id": null,
  "raw_url": "https://...",
  "canonical_url": "https://...",
  "title": "string",
  "author": "string | null",
  "published_at": "2026-11-25T11:30:00Z | null",
  "fetched_at": "2026-11-25T12:00:00Z",
  "raw_summary": "string | null",
  "summary_truncated": false,
  "lead_excerpt": "string | null",
  "extraction_status": "pending | ok | fallback_rss | failed",
  "state": "unread | read | expired",
  "is_starred": false,
  "read_at": "2026-11-25T12:05:00Z | null",
  "starred_at": "2026-11-25T12:05:00Z | null",
  "expired_at": "2026-11-25T12:05:00Z | null",
  "unexpired_at": "2026-11-25T12:05:00Z | null",
  "read_state_updated_at": "2026-11-25T12:00:00Z",
  "starred_updated_at": "2026-11-25T12:00:00Z",
  "ingest_sync_seq": 42,
  "enrichment": null,
  "tags": [],
  "research_sources": [],
  "user_feedback": null,
  "updated_at": "2026-11-25T12:00:00Z"
}
```
- **Bounded SQL column projection**: Both `bootstrap` and `pull` hydration project `LEFT(items.raw_summary, 32769)` (32,768 + 1 sentinel character to detect truncation) and `LEFT(items.extracted_text, 8192)` (ample headroom for whitespace normalization into 1,500 chars) instead of loading full TOASTed 20–30 KB `extracted_text` columns into the API process.
- **`raw_summary` capping**: Truncated at `32_768` characters if longer (`len > 32_768`), setting `summary_truncated = true`; otherwise passed verbatim with `summary_truncated = false`. Full `extracted_text` is **omitted** from sync payloads (stays server-only for Postgres FTS and `GET /api/v1/items/{id}`).
- **`lead_excerpt` derivation**: Derived in pure Python (`domain/sync_merge.py`):
  - Source text: `LEFT(extracted_text, 8192)` if non-empty, else HTML-stripped `raw_summary`.
  - Normalize consecutive whitespace to single spaces, strip leading/trailing whitespace, slice to `1_500` characters (or `null` if empty).
  - Conveniently reuses the exact same 1,500-character excerpt window specified for `1e` embeddings (`data-model.md` §4.7).
- **Forward-compatible fields in `1b`**: `cluster_id = null`, `enrichment = null`, `tags = []`, `research_sources = []`, `user_feedback = null`.

#### `SyncAppSettingsDTO`
```json
{
  "default_expiration_days": 30,
  "backfill_cap": 10,
  "default_poll_interval_minutes": 60,
  "updated_at": "2026-11-25T12:00:00Z"
}
```
*(Excludes internal operational column `last_retention_cleanup_at`.)*

---

### 6.2 `GET /api/v1/sync/bootstrap`

**Query Parameters**
| Param | Type | Default | Constraints |
| :--- | :--- | :--- | :--- |
| `cursor` | `UUID \| null` | `null` | Keyset cursor (`items.id` of last item from previous page). Omit on Page 1. |
| `limit` | `int` | `500` | `1 <= limit <= 1000` |

**Response (`200 OK`)**
```json
{
  "instance_epoch": "uuid",
  "server_now": "2026-11-25T12:00:00Z",
  "watermark": 1234,
  "categories": [ ... ],
  "feeds": [ ... ],
  "clusters": [],
  "interest_profile": null,
  "app_settings": { ... },
  "items": [ ... ],
  "next_cursor": "uuid | null",
  "has_more": true
}
```
- **Page 1 (`cursor` omitted)**:
  - Executes inside a single `REPEATABLE READ` read-only transaction so `sync_state` (`watermark`, `instance_epoch`), `server_now = SELECT now()`, metadata collections, and the first page of items come from a consistent DB snapshot.
  - Returns full `categories` (`WHERE deleted_at IS NULL ORDER BY sort_order ASC, id ASC`), `feeds` (`ORDER BY id ASC`, including `unsubscribed` so clients know feed metadata for retained starred items and re-subscribe state), `clusters: []`, `interest_profile: null`, `app_settings`, and the first `limit` working-set `items`.
- **Pages 2..N (`cursor` provided)**:
  - Returns `instance_epoch`, `server_now`, `watermark: null` *(omitted deliberately so clients can never accidentally overwrite Page 1's pinned `watermark` with a later sequence and introduce a sync gap)*, `categories: null`, `feeds: null`, `clusters: null`, `interest_profile: null`, `app_settings: null`, plus the next `limit` working-set `items` where `items.id > :cursor`.
- **Working-Set Item Filter** (uses PostgreSQL `now()`, `C5`, and bounded `LEFT(...)` column projection):
  ```sql
  SELECT id, feed_id, raw_url, canonical_url, title, author, published_at, fetched_at,
         LEFT(raw_summary, 32769) AS raw_summary_slice,
         LEFT(extracted_text, 8192) AS extracted_text_slice,
         extraction_status, state, is_starred, read_at, starred_at, expired_at, unexpired_at,
         read_state_updated_at, starred_updated_at, ingest_sync_seq, updated_at
  FROM items
  WHERE (
    state = 'unread'
    OR is_starred = TRUE
    OR (state = 'read' AND read_at >= now() - INTERVAL '14 days')
  )
    AND (:cursor IS NULL OR id > :cursor)
  ORDER BY id ASC
  LIMIT :limit + 1
  ```
  - Fetching `limit + 1` rows determines `has_more = len(rows) > limit` accurately without an extra `COUNT(*)` query. If `has_more` is true, slice to `rows[:limit]` and set `next_cursor = rows[limit - 1].id`; else `next_cursor = None`.
- **Client Bootstrap & Restore-Replay Protocol (Normative for `1c` and Phase 2)**:
  1. Call Page 1 (`cursor` omitted). Save `page1_epoch = instance_epoch` and `page1_watermark = watermark`. Check `|client_now - server_now| > 5 minutes` to surface a non-blocking clock-drift warning banner if the device clock is misconfigured.
  2. While `has_more == true`, call `GET /api/v1/sync/bootstrap?cursor=<next_cursor>&limit=500`. If `response.instance_epoch != page1_epoch` on any page, abort and restart from Step 1.
  3. Commit the complete local working set + `cursor = page1_watermark` + `epoch = page1_epoch` inside a **single local Dexie transaction**, tolerating temporarily dangling `feed_id`/`category_id` references (e.g., feeds created after Page 1 whose items appeared on Pages 2..N) until Step 4 completes.
  4. Immediately run `GET /api/v1/sync/pull?since_seq=<page1_watermark>&epoch=<page1_epoch>` to converge any changes (including newly created feeds/categories) committed during bootstrap pagination.
  5. **48-hour Acknowledged Replay Journal & Restore Recovery (Owner decision `Q-1` / `P-11`, `P-11-F1`)**: Clients MUST retain recently acknowledged mutations in a local replay journal for a rolling **48-hour window** (covering the 24h nightly backup interval + margin). When bootstrap is triggered by `resync_reason == "epoch_mismatch"` (DB backup restore):
     - Single-field mutations (`item_set_state`, `item_set_starred`, `feed_set_field`) and explicit `scope='items'` bulk mark-read mutations are **epoch-agnostic**: the client retains them from both its pending unsynced outbox and its 48-hour acknowledged replay journal, re-bootstraps cleanly, and flushes both via `POST /api/v1/sync/push` so zero post-backup read/star/feed-toggle state is lost (mutations already in the restored DB hit `sync_mutations` as idempotent replays; post-backup mutations re-apply cleanly via LWW).
     - Scoped `item_mark_bulk_read` mutations (`all`/`feed`/`category`) carry the old epoch's `up_to_sync_seq` sequence space, which cannot safely bound post-restore items. Before overwriting local state during re-bootstrap, the client MUST either **drop** old-epoch scoped bulk-read entries from its outbox/replay journal or **rewrite** them into chunked `scope='items'` mutations over the explicit `item_ids` known to the client pre-restore. As server-side defense-in-depth, any scoped `item_mark_bulk_read` whose `epoch != current instance_epoch` is rejected with `reason = "epoch_mismatch"` and zero side effects (see §6.5 item 4).

---

### 6.3 `GET /api/v1/sync/pull`

**Query Parameters**
| Param | Type | Default | Constraints |
| :--- | :--- | :--- | :--- |
| `since_seq` | `int` | *(required)* | `>= 0` |
| `epoch` | `UUID` | *(required)* | Client's stored `instance_epoch` |
| `limit` | `int` | `200` | `1 <= limit <= 500` (applies to raw `sync_changes` rows scanned per page) |

**Response (`200 OK`) — Normal Delta**
```json
{
  "resync_required": false,
  "resync_reason": null,
  "instance_epoch": "uuid",
  "server_now": "2026-11-25T12:02:00Z",
  "watermark": 1250,
  "next_seq": 1250,
  "has_more": false,
  "changes": [
    {
      "seq": 1245,
      "entity_type": "feed",
      "entity_id": "uuid",
      "op": "upsert",
      "changed_at": "2026-11-25T12:01:00Z",
      "data": { ... }
    },
    {
      "seq": 1248,
      "entity_type": "category",
      "entity_id": "uuid",
      "op": "delete",
      "changed_at": "2026-11-25T12:02:00Z",
      "data": null
    }
  ]
}
```

**Response (`200 OK`) — Resync Required**
```json
{
  "resync_required": true,
  "resync_reason": "epoch_mismatch | cursor_ahead | cursor_pruned",
  "instance_epoch": "uuid",
  "server_now": "2026-11-25T12:02:00Z",
  "watermark": 1250,
  "next_seq": 1250,
  "has_more": false,
  "changes": []
}
```

**Exact Server Execution Algorithm**
1. Execute inside a single `REPEATABLE READ` read-only transaction (prevents reading a `sync_state` row from snapshot T1 and `sync_changes` rows from snapshot T2!).
2. Read `server_now = SELECT now()` and `sync_state WHERE id = 1`: `(current_seq, instance_epoch, pruned_through_seq)`.
3. Check resync conditions in priority order:
   - `epoch != instance_epoch` → return `resync_required=True, resync_reason="epoch_mismatch"`.
   - `since_seq > current_seq` → return `resync_required=True, resync_reason="cursor_ahead"`.
   - `since_seq < pruned_through_seq` → return `resync_required=True, resync_reason="cursor_pruned"`.
4. Query raw `sync_changes` bounded to `current_seq`:
   ```sql
   SELECT seq, entity_type, entity_id, op, changed_at
   FROM sync_changes
   WHERE seq > :since_seq AND seq <= :current_seq
   ORDER BY seq ASC
   LIMIT :limit + 1
   ```
5. Compute `has_more = len(raw_rows) > limit`, slice `page_rows = raw_rows[:limit]`.
   - If `page_rows` is empty: `next_seq = since_seq`, `changes = []`, return.
   - Otherwise: `next_seq = page_rows[-1].seq`.
6. **Within-page coalescing (Decision `1b-D03`)**:
   - Iterate `page_rows` in ascending `seq` order and keep the **latest** row per `(entity_type, entity_id)`.
   - Sort the surviving deduplicated entries by `seq ASC` so `changes` stays strictly ordered by `seq`.
7. **Bulk hydration** (grouped by `entity_type`, chunked at 500 IDs for `D1` safety):
   - For `op == "delete"`: emit `data = None`.
   - For `op == "upsert"`:
     - `category`: load from `categories WHERE id = ANY(:ids)`. If missing or `deleted_at IS NOT NULL` (deleted by a later transaction beyond this page), emit `op = "delete", data = None` so clients never see a soft-deleted row as active (the later `delete` tombstone on a subsequent page is an idempotent no-op).
     - `feed`: load from `feeds WHERE id = ANY(:ids)` → `SyncFeedDTO`. (If missing, emit `op = "delete", data = None`.)
     - `item`: load using bounded `LEFT(raw_summary, 32769)` and `LEFT(extracted_text, 8192)` slices from `items WHERE id = ANY(:ids)` → `SyncItemDTO`. (If missing, emit `op = "delete", data = None`.)
     - `app_settings`: load `app_settings WHERE id = 1` → `SyncAppSettingsDTO`.
     - Future types (`cluster`, `interest_profile`): handled cleanly when `1d`/`1e` introduce their tables.
- **Normative Client Pull Application Rule (`1c` and Phase 2)**:
  - Apply each `sync/pull` page (all `changes` + advancing `cursor = next_seq`) inside a **single local Dexie transaction**.
  - Because within-page coalescing retains the latest `seq` per entity, a parent entity's upsert can appear *after* a child item referencing it (e.g. `[feed(1), item(2), feed(3)]` coalesces to `[item(2), feed(3)]`). Clients MUST tolerate temporarily dangling `feed_id`/`category_id` references within a page or across multi-page pulls until `has_more == false`.
  - Compare `|client_now - server_now| > 5 minutes` to surface a non-blocking clock-drift warning banner if the device clock is skewed.

---

### 6.4 `POST /api/v1/sync/push`

**Request Schema**
```json
{
  "mutations": [
    {
      "client_mutation_id": "uuid",
      "client_timestamp": "2026-11-25T12:00:00.123Z",
      "type": "item_set_state",
      "item_id": "uuid",
      "state": "read"
    }
  ]
}
```
- **Request-body size cap (`S6`)**: Maximum HTTP body size for `POST /api/v1/sync/push` is **`2 MiB`** (`2_097_152` bytes); larger payloads are rejected with `413 Payload Too Large` before JSON parsing.
- **Batch bounds**: `0 <= len(mutations) <= 500`, and total explicit `item_ids` across all `scope='items'` mutations in a single push request MUST be `<= 5_000` (each individual `scope='items'` mutation remains capped at `<= 1_000`). Requests exceeding either bound return `422 Unprocessable Entity` (client chunks larger outboxes into smaller batches).
- **Duplicate `client_mutation_id` inside the same request array**: The first occurrence processes normally; any duplicate within the same request array with the same `payload_sha256` receives the identical replay outcome without double-applying, while a duplicate with a **different** `payload_sha256` receives `outcome = "rejected", reason = "mutation_id_conflict"`.
- **Timezone requirement**: `client_timestamp` MUST be timezone-aware (reject naive datetimes with `422`).

#### Supported Mutation Types in `1b` (Discriminated Union on `type`)

| `type` | Payload Fields | Validation & Conflict Policy |
| :--- | :--- | :--- |
| **`item_set_state`** | `item_id: UUID`, `state: Literal["read", "unread"]` | Per-field LWW comparing `(effective_ts, state_rank(new))` vs `(item.read_state_updated_at, state_rank(item.state))`. Rejects if `item_id` not found (`entity_not_found`) or `client_timestamp < server_now - 90d` (`clock_skew`). |
| **`item_set_starred`** | `item_id: UUID`, `is_starred: bool` | Per-field LWW comparing `(effective_ts, star_rank(new))` vs `(item.starred_updated_at, star_rank(item.is_starred))`. Orthogonal to `state`; starring an `expired` item succeeds and keeps `state='expired'`. Rejects if `item_id` not found (`entity_not_found`) or `clock_skew`. |
| **`item_mark_bulk_read`** | `scope: Literal["all", "feed", "category", "items"]`, `up_to_sync_seq: int \| null`, `epoch: UUID \| null`, `feed_id: UUID \| null`, `category_id: UUID \| null`, `older_than_ts: datetime \| null`, `item_ids: list[UUID] \| null` | See §6.5 below. (`epoch` is required for scoped modes `all`/`feed`/`category` and checked against `sync_state.instance_epoch`; optional/ignored for `scope='items'`.) |
| **`feed_set_field`** | `feed_id: UUID`, `field: Literal["status", "category_id", "expiration_days_override", "research_policy.enabled", "research_policy.fetch_pdfs"]`, `value: Any` (typed per field) | Per-field LWW comparing `(effective_ts, field_rank(new))` vs `(stored_ts, field_rank(current))`. See §6.6 below. |

*Reserved for future milestones (`1e`: `split_cluster`; `1g`: `item_feedback`, `feed_instruction_add`, `feed_instruction_archive`). Any unknown `type` fails Pydantic validation (`422`), or if explicitly listed as a reserved future type returns `outcome = "rejected", reason = "unsupported_mutation_type"`.*

**Response Schema (`200 OK`)**
```json
{
  "instance_epoch": "uuid",
  "server_now": "2026-11-25T12:00:01Z",
  "results": [
    {
      "client_mutation_id": "uuid",
      "outcome": "applied | superseded | rejected",
      "reason": "string | null",
      "affected_count": "int | null",
      "current_entity_type": "item | feed | null",
      "current_entity": { "...SyncPushItemSnapshot or SyncPushFeedSnapshot..." }
    }
  ]
}
```
- **No `watermark` in push response (Normative for `1c` and Phase 2 — `P-03`)**: `POST /api/v1/sync/push` deliberately **omits** any sequence/watermark field so a client can never mistakenly advance its pull cursor past concurrent changes from other devices or pollers (`NFR-SYNC-01`). **Normative rule:** Clients MUST advance their pull cursor **only** from `GET /api/v1/sync/pull.next_seq` or `GET /api/v1/sync/bootstrap` Page 1 `watermark`. Clients SHOULD run `GET /api/v1/sync/pull` immediately after `POST /api/v1/sync/push`; receiving the push's own changes back in that pull is expected and idempotent.
- **Slim `current_entity` policy (`P-08`)**:
  - **`applied`**: `current_entity = None`, `current_entity_type = None` (keeps a 500-mutation flush response lean; `affected_count` is populated for `item_mark_bulk_read`).
  - **`superseded`** or **`rejected`**: Populate `current_entity_type` + a slim snapshot whenever the target entity exists (so the client immediately reconciles or rolls back its Dexie record without waiting for `pull`, while keeping even a 500-mutation all-superseded response `< 512 KB` instead of ~16 MB); `None` if `reason == "entity_not_found"`:
    - `SyncPushItemSnapshot`: `{id, state, is_starred, read_at, starred_at, expired_at, unexpired_at, read_state_updated_at, starred_updated_at, updated_at}` *(omits `raw_summary`, `lead_excerpt`, and non-mutable fields)*.
    - `SyncPushFeedSnapshot`: `{id, status, category_id, expiration_days_override, research_policy, config_version, field_updated_at, updated_at}`.

---

### 6.5 LWW Rules, Clock-Skew Clamping & Tie-Breaking (`domain/sync_merge.py`)

Pure deterministic functions with zero DB dependencies (tested exhaustively with `hypothesis`):

1. **Clock-Skew Clamping & Dead-RTC Lower Bound (`C5`, Owner decision `Q-3`)**:
   - At the start of `POST /api/v1/sync/push`, query `server_now = (await session.execute(sa.select(sa.func.now()))).scalar_one()`.
   - **Dead-RTC lower bound**: If `client_timestamp.astimezone(UTC) < server_now - timedelta(days=90)` (beyond the 90-day `sync_changes` retention window, catching a dead RTC / 1970 clock without ever rejecting multi-day offline travel), reject the mutation with `outcome = "rejected", reason = "clock_skew"` (and populate slim `current_entity` if the target exists).
   - **Future clamp**: Otherwise compute:
     ```python
     max_allowed = server_now + timedelta(minutes=5)
     effective_ts = min(client_timestamp.astimezone(UTC), max_allowed)
     ```
   - In addition, `server_now` is returned on every `bootstrap`, `pull`, and `push` response so `1c` can compare `|client_now - server_now| > 5 minutes` and warn the user of device clock drift directly.
2. **Deterministic Value-Rank LWW Comparator (Decision `1b-D04`)**:
   - Let `incoming_key = (effective_ts, rank(new_value))` and `stored_key = (stored_ts, rank(current_value))`.
   - **Tie-break ranks**:
     - `items.state`: `{"expired": 1, "unread": 2, "read": 3}` (`read > unread > expired`).
     - `items.is_starred`: `{False: 1, True: 2}` (`True > False`).
     - `feeds.status`: `{"active": 1, "paused": 2, "muted": 3, "unsubscribed": 4}`.
     - `feeds.category_id`: canonical string `""` for `None`, else `str(uuid)` (lexicographic).
     - `feeds.expiration_days_override`: `(-1,)` for `None`, else `(val,)` (integer order).
     - `feeds.research_policy.enabled` / `feeds.research_policy.fetch_pdfs`: `{False: 1, True: 2}`.
   - **Decision rule**:
     - If `new_value == current_value` and `effective_ts <= stored_ts`: **`superseded`** (no-op; no `sync_changes` emitted).
     - If `new_value == current_value` and `effective_ts > stored_ts`: advance `stored_ts = effective_ts`, emit `sync_changes` `upsert` (so other devices converge on the latest LWW timestamp), and return **`applied`**.
     - If `incoming_key > stored_key`: apply `new_value`, set `stored_ts = effective_ts`, emit `sync_changes` `upsert`, return **`applied`**.
     - Else (`incoming_key <= stored_key`): leave DB unchanged, return **`superseded`** with slim `current_entity`.
   - **Scope of Commutativity**: Because `(effective_ts, rank(value))` defines a strict total order on `(timestamp, value)` pairs for every field, taking the maximum over any finite set of single-field LWW mutations (`item_set_state`, `item_set_starred`, `feed_set_field`) — plus explicit `scope='items'` bulk mark-read on `unread`/`read` items — yields the exact same `(timestamp, value)` regardless of arrival order or duplicates. Scoped bulk mark-read (`all`/`feed`/`category`) and `expired` exclusion are deliberate non-commuting carve-outs documented in item 4 below.
3. **Side-Effect Timestamps on `item_set_state` & `item_set_starred`**:
   - When `item_set_state` wins and sets `state = "read"`:
     - `item.state = "read"`, `item.read_at = effective_ts`, `item.read_state_updated_at = effective_ts`, `item.unexpired_at = None`, `item.updated_at = server_now`.
   - When `item_set_state` wins and sets `state = "unread"`:
     - If `item.state == "expired"` or `item.expired_at is not None`: set `item.unexpired_at = effective_ts` (resets expiration grace clock per `data-model.md` §3.1 & §6.1 so `expire_items` does not immediately re-expire a restored item, and keeps every column—including `unexpired_at`—order-independent across concurrent `read`/`unread` permutations).
     - `item.state = "unread"`, `item.read_at = None`, `item.read_state_updated_at = effective_ts`, `item.updated_at = server_now`.
   - When `item_set_starred` wins:
     - `item.is_starred = new_starred`, `item.starred_at = effective_ts if new_starred else None`, `item.starred_updated_at = effective_ts`, `item.updated_at = server_now`. (`item.state` is untouched.)
4. **Bounded `item_mark_bulk_read` Rules (Decision `1b-D05`, Owner decision `Q-2` / `P-01`, `P-11-F1`)**:
   - **Validation**:
     - `scope == "all"`: requires `up_to_sync_seq is not None` (`>= 0`) and `epoch is not None`; `feed_id`, `category_id`, `item_ids` must be `None`.
     - `scope == "feed"`: requires `feed_id`, `up_to_sync_seq >= 0`, and `epoch is not None`; rejects with `entity_not_found` if feed doesn't exist.
     - `scope == "category"`: requires `category_id`, `up_to_sync_seq >= 0`, and `epoch is not None`; rejects with `category_not_found` if category doesn't exist or `deleted_at IS NOT NULL`.
     - `scope == "items"`: requires non-empty `item_ids` (`1 <= len(item_ids) <= 1000`, with total explicit `item_ids` across the entire push request `<= 5000`); `up_to_sync_seq` and `epoch` are optional/ignored (epoch-agnostic, like single-field mutations).
   - **Epoch guard for scoped modes (`P-11-F1`)**:
     - For `scope IN ('all', 'feed', 'category')`, compare `mutation.epoch` against `sync_state.instance_epoch`. If `mutation.epoch != instance_epoch`, immediately reject the mutation with `outcome = "rejected", reason = "epoch_mismatch", affected_count = None` and perform **zero** item updates (preventing a pre-restore `up_to_sync_seq` from matching newly ingested post-restore items whose sequence numbers fell below the old bound).
   - **Two-tier matching semantics (`Q-2`)**:
     - **Explicit `scope == "items"` (true per-item LWW over `unread` & `read`)**: For each existing item in `item_ids` with `state IN ('unread', 'read')` (still excluding `expired` as a documented carve-out; starred items stay `is_starred = True`), apply the exact same LWW `item_set_state(state='read', effective_ts)` rule as single-item writes:
       - `unread` rows with `effective_ts >= read_state_updated_at` flip to `read` (`read_at = effective_ts, read_state_updated_at = effective_ts, unexpired_at = None, updated_at = server_now`) and emit `item` `upsert`.
       - Already-`read` rows with `effective_ts > read_state_updated_at` advance `read_state_updated_at = effective_ts, read_at = effective_ts, updated_at = server_now` and emit `item` `upsert`.
       - This makes `scope='items'` (including `1e` `mark_cluster_read`) strictly commutative with `item_set_state` across `unread`/`read` items!
     - **Scoped modes (`scope IN ('all', 'feed', 'category')` — owner-accepted non-commuting carve-out)**:
       - Filter strictly to `items.state = 'unread'` (never touches already-`read` or `expired` archive rows, avoiding locking and re-emitting tens of thousands of historical archived `read` items on every "Mark all read" click; starred unread items DO transition to `read` while staying `is_starred = True`).
       - Filter `items.ingest_sync_seq <= :up_to_sync_seq`, plus optional `older_than_ts` (`COALESCE(items.published_at, items.fetched_at) < :older_than_ts`), plus per-item LWW guard `effective_ts >= items.read_state_updated_at` (since `rank('read') = 3 > rank('unread') = 2`).
       - **Documented non-commuting carve-out**: Because scoped bulk read skips rows that are currently `read` or `expired`, an offline `read → unread` (or `expired → unread`) mutation at `t2 < t3` can produce `unread` if the bulk read arrives before it, vs. `read` if the bulk read arrives after it. Accepted by the project owner (`Q-2`) to preserve O(unread) latency and zero archive sync amplification.
   - **Execution & Outcome**:
     - Locks matching `items` rows in `id ASC` order (respecting §5.1 lock ordering), updates matching rows, records `SyncEvent("item", id, "upsert")` for every modified item.
     - Records `outcome = "applied"`, `affected_count = <modified_rows>` (even if `0` items matched — e.g., already read by another device — the mutation is valid and `applied` with `affected_count = 0`, never `rejected`).

### 6.6 `feed_set_field`, Online Monotonic LWW & Shared Status Helper (`S-04`, `P-02`, `Q-4`)

- **Shared Feed Status Transition Helper (`domain/sync_merge.py` — Owner decision `Q-4`)**:
  - Introduce `apply_feed_status_transition(feed, new_status, db_now) -> bool` (returns `should_enqueue_poll`) used identically by **both** online `PATCH /api/v1/feeds/{id}` (`routes/feeds.py`) and offline `feed_set_field` (`sync/service.py`):
    - If `old_status == "unsubscribed"` and `new_status == "active"`: reset `etag = None`, `last_modified = None`, `health_state = "healthy"`, `consecutive_errors = 0`, `last_error = None`, `initial_backfill_done = False`, `next_fetch_at = db_now`, set `feed.status = "active"`, and return `True` (caller enqueues `poll_feed` with `priority=10, bump_existing=True` in §5.1 lock order after `items` and before `sync_state`).
    - If `old_status == "paused"` and `new_status in ("active", "muted")`: set `next_fetch_at = min(feed.next_fetch_at, db_now)` if `feed.next_fetch_at` else `db_now`, set `feed.status = new_status`, return `False`.
    - Otherwise: set `feed.status = new_status`, return `False`.
- **Allowed `feed_set_field` fields & validation**:
  | `field` | Expected `value` | Rejection Cases | Side Effects on Apply |
  | :--- | :--- | :--- | :--- |
  | `status` | `"active" \| "paused" \| "muted" \| "unsubscribed"` | Invalid enum → `invalid_field_value` | Calls shared `apply_feed_status_transition(feed, value, server_now)` and enqueues `poll_feed` if returned `True`. |
  | `category_id` | `UUID \| null` | Non-null UUID missing or `deleted_at IS NOT NULL` → `category_not_found` | Sets `feed.category_id = value`. |
  | `expiration_days_override` | `int (0..3650) \| null` | Out of range / non-int → `invalid_field_value` | Sets `feed.expiration_days_override = value`. |
  | `research_policy.enabled` | `bool` | Non-bool → `invalid_field_value` | Updates `feed.research_policy["enabled"] = value` (preserving sibling keys). |
  | `research_policy.fetch_pdfs` | `bool` | Non-bool → `invalid_field_value` | Updates `feed.research_policy["fetch_pdfs"] = value` (preserving sibling keys). *(In `1g`, atomically logs `config_fetch_pdfs_audit` in `feedback_events`.)* |
- **Stored LWW timestamp lookup**:
  - Read `field_updated_at.get(field)`; for `research_policy.<subkey>`, fall back to `field_updated_at.get("research_policy")` if the granular key is absent (backward compatibility with `1a` rows). If neither exists, treat stored timestamp as `feed.created_at`.
  - On win, write `field_updated_at[field] = effective_ts.isoformat()`.
- **Online Monotonic LWW Rule (`P-02`, `C5`, `C7`) & `config_version` independence (`S-04`)**:
  - `feed_set_field` NEVER reads or increments `feeds.config_version`.
  - Add pure helper `compute_online_lww_timestamp(db_now: datetime, stored_ts: datetime | None) -> datetime` in `domain/sync_merge.py`:
    ```python
    if stored_ts is not None and stored_ts >= db_now:
        return stored_ts + timedelta(microseconds=1)
    return db_now
    ```
  - **Online `PATCH /api/v1/feeds/{id}` and `POST /api/v1/feeds` reactivation (`routes/feeds.py`)**:
    - Read `db_now = (await session.execute(sa.select(sa.func.now()))).scalar_one()` (never Python `datetime.now(UTC)`).
    - `config_version` increments **only** when `curation_instructions` actually changes (requires matching `expected_config_version`, returning `409 Conflict` on mismatch).
    - For every LWW field modified online (`status`, `category_id`, `expiration_days_override`, `research_policy.enabled`, `research_policy.fetch_pdfs`, `title`, `feed_url`, `poll_interval_minutes`, `fetch_full_text`), always apply the online value and stamp `field_updated_at[f] = compute_online_lww_timestamp(db_now, stored_ts_f).isoformat()` (for `research_policy`, stamp granular `research_policy.enabled` and `research_policy.fetch_pdfs` when changed, plus `research_policy` when `guidance`/`max_hops` change). Because the timestamp is strictly monotonic and always `>= db_now` and `> stored_ts_f`, a future-clamped offline write can never cause an online edit's timestamp to move backwards or lose to an older offline toggle.
    - Status edits in `PATCH /api/v1/feeds/{id}` call the shared `apply_feed_status_transition(feed, body.status, db_now)` helper (`Q-4`).
  - **Online `DELETE /api/v1/categories/{id}` (`routes/categories.py`)**:
    - Read `db_now = SELECT now()`, set `cat.deleted_at = db_now`, `cat.updated_at = db_now`, and update affected child feeds (`ORDER BY id ASC` / bulk `UPDATE`) setting `category_id = NULL`, `updated_at = db_now`, and `field_updated_at['category_id']` monotonically via `GREATEST(now(), COALESCE(CAST(field_updated_at->>'category_id' AS timestamptz), CAST('-infinity' AS timestamptz)) + INTERVAL '1 microsecond')` formatted as ISO-8601 UTC.

### 6.7 Single-Transaction Push Execution Flow & Transient Retry Policy (`sync/service.py`, `P-05`)

- **Transient-Error Retry Policy (`P-05`)**:
  - Wrap the push transaction below in a bounded retry loop of **up to 3 attempts** with jittered backoff (`20–100 ms`), issuing `await session.rollback()` between attempts, retrying **only** on PostgreSQL SQLSTATE:
    - `40P01` (`deadlock_detected`),
    - `40001` (`serialization_failure`),
    - `23505` (`unique_violation`) on `sync_mutations_pkey` (handles the realistic case where a client times out and retries a push while the first request is still committing — on attempt 2, step 2 finds the committed `sync_mutations` rows and returns the identical cached replay with HTTP `200`).
  - If all 3 attempts fail with a retryable SQLSTATE, return HTTP `503 Service Unavailable` with `Retry-After: 1` (which `1c`'s outbox treats as retryable without dropping mutations).

```text
1. Begin transaction. Read server_now = SELECT now() and current_epoch = sync_state.instance_epoch (plain SELECT, no FOR UPDATE).
2. Compute canonical payload_sha256 for every mutation in the batch.
   Bulk-load existing sync_mutations WHERE client_mutation_id = ANY(batch_ids) (chunked at 500).
   - Maintain a `seen_in_batch: dict[UUID, tuple[str, SyncPushResult]]` map (payload_sha256, result)
     so duplicate client_mutation_ids within the same request array reuse the first occurrence's
     outcome if payload_sha256 matches, or record outcome="rejected", reason="mutation_id_conflict"
     if payload_sha256 differs.
   - For any mutation_id already persisted in sync_mutations:
     - Same device_id AND matching payload_sha256 → prepare cached replay outcome
       (+ fresh slim current_entity if superseded/rejected).
     - Different device_id OR mismatched payload_sha256 → prepare outcome="rejected",
       reason="mutation_id_conflict".
3. Collect all non-replayed target IDs up front to preserve strict §5.1 lock ordering:
   - Active categories referenced by feed_set_field / epoch-matching item_mark_bulk_read (read check / id ASC).
   - Feeds referenced by feed_set_field / epoch-matching item_mark_bulk_read → lock ORDER BY id ASC FOR UPDATE.
   - Items union: (explicit item_ids from item_set_state / item_set_starred / scope='items')
     UNION (candidate unread item_ids matching any epoch-matching scoped item_mark_bulk_read in this batch)
     → lock in strict ascending UUID order (ORDER BY id ASC FOR UPDATE, chunked at 500 if large).
4. Process non-replayed mutations sequentially in request array order:
   - Check dead-RTC lower bound (client_timestamp < server_now - 90 days → rejected: "clock_skew").
   - For scoped item_mark_bulk_read (all/feed/category), check mutation.epoch == current_epoch
     (mismatch → rejected: "epoch_mismatch", no item updates).
   - Compute effective_ts = min(client_timestamp, server_now + 5 minutes).
   - Validate entity existence & field rules → record "rejected" without raising DB errors.
   - Apply pure LWW merge in memory/SQL against the already-locked rows → "applied" or "superseded".
   - Stage any poll_feed job enqueues (for unsubscribed → active feed transitions) so jobs locks
     are acquired after items and before sync_state (§5.1).
   - Stage SyncMutation row (one per unique client_mutation_id, with payload_sha256) +
     collect SyncEvent(s) for modified entities.
5. Execute staged enqueue_job() calls (acquires jobs locks per §5.1 step 4).
6. Bulk-insert staged SyncMutation rows (chunked at 500 for D1 safety).
7. Coalesce staged SyncEvents by (entity_type, entity_id) preserving order, and call
   await sync_log.record(session, coalesced_events) ONCE (acquires sync_state FOR UPDATE last).
8. Read instance_epoch from sync_state, commit transaction, and return SyncPushResponse
   (instance_epoch, server_now, results — no watermark).
```

---

## 7. Online Settings API (`routes/settings.py`)

Exposes `app_settings` over REST (previously CLI-only in `1a`) so clients can inspect and modify global settings online:

- **`GET /api/v1/settings`** (authenticated): Returns `SyncAppSettingsDTO` (`default_expiration_days`, `backfill_cap`, `default_poll_interval_minutes`, `updated_at`).
- **`PATCH /api/v1/settings`** (authenticated, JSON-only):
  - Accepts optional fields: `default_expiration_days (0..3650)`, `backfill_cap (0..500)`, `default_poll_interval_minutes (5..10080)`.
  - Passing an explicit `null` for any non-nullable field returns `422 Unprocessable Entity`.
  - Uses `model_fields_set` (`D3`), acquires `AppSettings WHERE id = 1 FOR UPDATE` (before `sync_state` per §5.1), updates only changed fields using DB `now()`, emits `SyncEvent("app_settings", APP_SETTINGS_ENTITY_ID, "upsert")` via `sync_log.record()` **only if at least one synced value actually changed** (`D2`), and commits.

---

## 8. PostgreSQL Full-Text Search (`search/fts.py` & `routes/search.py`)

### 8.1 `search_vector` Maintenance
- **Field Weights & Input Preparation**:
  | Weight | Source Field(s) in `1b` | Future Additions (`1d`/`1f`) | Preparation |
  | :--- | :--- | :--- | :--- |
  | **`A`** | `items.title` | `+ tldr`, `+ tags[]` (`1d`) | Plain text |
  | **`B`** | `items.author` | — | Plain text (`COALESCE(author, '')`) |
  | **`C`** | `items.raw_summary` | `+ takeaways[]` (`1d`) | Strip HTML via stdlib `HTMLParser` (skipping `<script>`/`<style>` content, capped to first 256 KB raw input before parsing, collapsing whitespace, `S1`/`S5`) |
  | **`D`** | `items.extracted_text` | `+ research_context` (`1f`) | Slice to first `65_536` characters (`extracted_text[:65536]`) |
- **SQL Expression** (`rebuild_item_search_vector(session, item_id, language=...)` and batch variant for `poll_feed`, using `CAST(...)` bind syntax per `D5`):
  ```sql
  UPDATE items
  SET search_vector =
      setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:title, '')), 'A') ||
      setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:author, '')), 'B') ||
      setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:summary_plain, '')), 'C') ||
      setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:body_slice, '')), 'D')
  WHERE id = :item_id
  ```
- **Maintenance Hooks**:
  - `poll_feed.py`: Compute `search_vector` when inserting new items (chunked batch `UPDATE` or SQL expression in `INSERT`) and when updating an existing item on `entry_hash` change.
  - `extract_content.py`: Rebuild `search_vector` inside `_set_extraction_status()` when `extraction_status == "ok"` and `extracted_text` is persisted.
  - **Sync guard**: Rebuilding `search_vector` alone (e.g., `smartrss search reindex`) uses `execute_synced(..., lambda _res: [])` — zero `sync_changes` emitted.
- **CLI Command (`smartrss search reindex`)**:
  - Rebuilds `items.search_vector` in batches of `500` items (committed per batch so it never holds long locks or hits memory limits), using the configured `FTS_LANGUAGE`. Useful if `FTS_LANGUAGE` changes or after manual DB maintenance.

### 8.2 `GET /api/v1/search`

**Authentication & Security**: `GET /api/v1/search` requires a valid per-device bearer token (`Authorization: Bearer <token>`) and is protected by `1a`'s `Host` allow-list and CORS allow-list middleware (`ADR 0006`).

**Query Parameters**
| Param | Type | Default | Constraints / Behavior |
| :--- | :--- | :--- | :--- |
| `q` | `str \| null` | `null` | `max_length=500`. Parsed via `websearch_to_tsquery(CAST(:fts_cfg AS regconfig), :q)`. If provided and blank/whitespace-only, treated as `None`. If non-blank and `numnode(tsq) = 0` (only stop-words `"the and"` or punctuation `"!!!"`), returns `results=[], total=0` immediately. If `querytree(tsq) = 'T'` (negation-only or non-GIN-indexable query such as `"-foo"`, `"-foo -bar"`, `-"hello world"`, or `"foo OR -bar"` — verified on PG16), returns `422 Unprocessable Entity` (`"Search query must include at least one positive required search term"`). |
| `feed_id` | `UUID \| null` | `null` | Exact match on `items.feed_id`. |
| `category_id` | `UUID \| null` | `null` | Matches items whose feed has `feeds.category_id == :category_id`. |
| `state` | `Literal["unread", "read", "expired"] \| null` | `null` | Exact match on `items.state`. |
| `is_starred` | `bool \| null` | `null` | Exact match on `items.is_starred`. |
| `date_from` | `datetime \| null` | `null` | Timezone-aware lower bound on `COALESCE(items.published_at, items.fetched_at) >= :date_from`. |
| `date_to` | `datetime \| null` | `null` | Timezone-aware upper bound on `COALESCE(items.published_at, items.fetched_at) <= :date_to`. |
| `sort` | `Literal["relevance", "newest"]` | `"relevance"` | When `q` is omitted, `"relevance"` automatically falls back to `"newest"`. `"relevance"` orders by `ts_rank_cd(search_vector, tsq) DESC, COALESCE(published_at, fetched_at) DESC, id DESC`. `"newest"` orders by `COALESCE(published_at, fetched_at) DESC, id DESC`. |
| `limit` | `int` | `20` | `1 <= limit <= 100`. |
| `offset` | `int` | `0` | `0 <= offset <= 10000`. |

**Structured XSS-Safe Snippets (Decision `1b-D07`, `P-15`)**
- Computed **only** for the `limit` rows returned on the current page (never over the full match set), and only when `q` produced a non-empty, GIN-indexable `tsquery`.
- Source text per matched row: plain text built from `LEFT(extracted_text, 65536)` if non-empty, else HTML-stripped `LEFT(raw_summary, 65536)` if non-empty, else `title`, with any literal `\x02` (`STX`), `\x03` (`ETX`), and `\x04` (`EOT`) control characters stripped first.
- PostgreSQL `ts_headline` runs with `CAST(...)` and a double-quoted `chr(4)` fragment delimiter (verified on PG16 to preserve unambiguous fragment boundaries without dropping spaces or colliding with article `…`):
  ```sql
  ts_headline(
    CAST(:fts_cfg AS regconfig),
    :plain_text,
    :tsq,
    'StartSel=' || chr(2) || ', StopSel=' || chr(3) || ', MaxWords=35, MinWords=15, MaxFragments=2, FragmentDelimiter="' || chr(4) || '"'
  )
  ```
- Python (`domain/fts_text.py`) splits the returned string on `\x04` into fragments and parses each fragment on `\x02` / `\x03` boundaries into `list[list[SnippetFragment]]`:
  ```json
  "snippet": [
    [
      { "text": "PostgreSQL ", "highlight": false },
      { "text": "pgvector", "highlight": true },
      { "text": " HNSW indexes accelerate nearest-neighbor search", "highlight": false }
    ],
    [
      { "text": "Tune ", "highlight": false },
      { "text": "pgvector", "highlight": true },
      { "text": " ef_search for recall", "highlight": false }
    ]
  ]
  ```
- **Security property**: Zero HTML tags are ever injected or interpreted; even if an article's body literally contains `<script>alert(1)</script>`, it is returned as inert plain text inside `{"text": "<script>alert(1)</script>", "highlight": false}` and rendered safely as React text nodes in `1c`.

**Response (`200 OK`)**
```json
{
  "query": "pgvector \"sync watermark\"",
  "sort": "relevance",
  "total": 42,
  "limit": 20,
  "offset": 0,
  "results": [
    {
      "item": { "...SearchItemDTO (SyncItemDTO minus raw_summary/summary_truncated)..." },
      "rank": 0.4125,
      "snippet": [
        [
          { "text": "...", "highlight": false },
          { "text": "pgvector", "highlight": true }
        ]
      ]
    }
  ]
}
```
- **Slim `SearchItemDTO` (`P-08`)**: Search results omit `raw_summary` and `summary_truncated` (retaining `lead_excerpt` plus structured `snippet` fragments) so up to 100 search results stay compact.
- **Normative Client Cache Rule (`1c` and Phase 2 — `P-17`)**: Search results can return `expired` or `>14d read` archive items outside the client's working set. Clients MUST treat search results as **display-only** and MUST NOT blindly merge them into Dexie's active working set; if the user stars or restores a search result to `unread`, the resulting mutation/pull brings the item into the working set normally.

---

## 9. Security, Edge Cases & Checklist Verification

Cross-checked against [docs/review-checklist.md](../review-checklist.md):

| Checklist ID | Concern | Design Mitigation in `1b` |
| :--- | :--- | :--- |
| **S1 / S5** | Untrusted HTML/text in libraries & parser limits | HTML stripping uses stdlib `HTMLParser` (linear-time, non-recursive, never touches filesystem/URLs) on a pre-sliced input cap (`<= 256 KB`). `FTS_LANGUAGE` is validated against a strict allow-list and bound via `CAST(:fts_cfg AS regconfig)`. |
| **S6** | Size caps & JSON payload bounds | `sync/push` capped at `2 MiB` body (`413`), `500` mutations, and `5,000` total explicit `item_ids` (`1,000` per mutation); `raw_summary` capped at `32,768` chars; `lead_excerpt` at `1,500` chars; push `current_entity` uses slim snapshots and search omits `raw_summary`; `extracted_text` omitted from sync and projected via `LEFT(..., 8192)` on hydration / `LEFT(..., 65536)` on FTS/headline; `q` capped at `500` chars. |
| **C1 / Deadlocks** | Concurrent writes & stale reads | Strict global lock order (`categories` → `feeds` (`id ASC`) → `items` (`id ASC`, including `poll_feed` sorting `matched_existing`) → `jobs` → `app_settings` → `sync_state`). `bootstrap` Page 1 and `pull` run in single `REPEATABLE READ` read transactions. `sync/push` retries `40P01`/`40001`/`23505` up to 3× with jitter before returning `503`. |
| **C5 / C7** | Single clock & monotonic LWW | `sync/push`, online `PATCH /feeds`, `POST /feeds` reactivation, `DELETE /categories`, and `PATCH /settings` all use PostgreSQL `SELECT now()`. Online LWW edits use `GREATEST(db_now, stored_ts + 1µs)` via shared `compute_online_lww_timestamp()`. `SyncLogWriter` uses `clock_timestamp()` under `sync_state FOR UPDATE`. |
| **D1** | PostgreSQL 65,535 bind-param limit | `SyncLogWriter.record()` chunks `sync_changes` inserts at `1,000` rows; `pull` hydration & `push` pre-loads chunk at `500` IDs; `poll_feed` `ingest_sync_seq` update uses `UNNEST` (2 params total). |
| **D2** | Sync emission guard | `poll_interval_minutes` added to `FEED_SYNCED_COLUMNS` (`last_fetched_at`/`last_success_at` excluded from `SyncFeedDTO`); `ITEM_SYNCED_COLUMNS` added to `sync_log.py`; all synced mutations go through `SyncLogWriter.record()`; `search_vector` & `ingest_sync_seq` updates explicitly emit zero extra events. |
| **D3** | Defaults on partial updates | `PATCH /api/v1/settings` (rejecting explicit `null` with `422`) and `feed_set_field` apply only explicitly provided fields (`model_fields_set`), never clobbering sibling keys in `research_policy` or `app_settings`. |
| **D5** | SQLAlchemy `text()` bind casts | All parameterized casts in SQLAlchemy `text()` use `CAST(:param AS type)` (verified on SQLAlchemy 2.1.9) instead of `:param::type`. |
| **L3** | Event-loop responsiveness | HTML stripping and snippet parsing run quickly on bounded slices (`<= 64–256 KB`); bulk `smartrss search reindex` and `0002` backfill commit in 500-row batches. |

---

## 10. Verification & Test Plan (Critical Modules & Invariant Matrix)

### 10.1 Critical Core Modules (`>= 90%` Branch+Line Coverage Gate)
Add the following `1b` modules to `CRITICAL_MODULES` in [`scripts/check-critical-coverage.py`](../../scripts/check-critical-coverage.py) in the same commit as implementation (preserving all 12 `1a` modules):
- `src/smartrss/domain/sync_merge.py`
- `src/smartrss/domain/fts_text.py`
- `src/smartrss/sync/service.py`
- `src/smartrss/search/fts.py`

### 10.2 Mandatory Test Matrix (Mapped to Requirements & `development-process.md` §2.4)

| # | Test Area | Layer | Required Assertions |
| :--- | :--- | :--- | :--- |
| **1b-T01** | **Gap-free concurrent-commit `sync_seq` & lock ordering** | Integration (Real PG) | (a) Two concurrent DB connections: Tx A begins first, mutates an item, pauses; Tx B begins second, mutates another item, commits; client pulls `since_seq=0` (sees Tx B); Tx A commits (receives higher `seq`); client pulls `since_seq=B.seq` → receives Tx A with zero skipped or duplicated rows. (b) Two-connection test where `poll_feed` updates two existing items while `sync/push` locks the same two items, asserting sorted `item.id ASC` update order and zero deadlocks (`P-04`). |
| **1b-T02** | **LWW commutativity, clock-skew & mixed-batch convergence** | Unit + Integration (`hypothesis`) | (a) Pure `hypothesis` tests over `item_set_state`, `item_set_starred`, `feed_set_field` including identical timestamps, out-of-order arrivals, duplicates, far-future timestamps (`+1 year`, clamped to `server_now + 5 min`), and dead-RTC timestamps (`< server_now - 90d` → `rejected: clock_skew`). (b) Real-Postgres `hypothesis` test (small `max_examples`) permuting mixed batches through `sync/service.py` including `item_mark_bulk_read`: asserts identical final rows across permutations for `scope='items'` on `unread`/`read` items, and asserts the documented non-commuting carve-out for `scope='feed'` (`P-01`). |
| **1b-T03** | **Bootstrap pagination + mid-bootstrap writes** | Integration (Real PG) | Seed > 2 pages of mixed `unread`, `starred`, recent `read` (<= 14d), old `read` (> 14d), and `expired` items. Verify working-set filter, bounded SQL slice projection, keyset pagination without duplicates/gaps, `server_now` presence, mid-bootstrap inserts/mutations caught by trailing `pull(page1_watermark)`, and `instance_epoch` mismatch detection mid-pagination. |
| **1b-T04** | **`sync/pull` coalescing, cross-entity ordering, push→pull & `resync_required`** | Integration (Real PG) | (a) Multiple `sync_changes` for the same item coalesce to 1 hydrated DTO while advancing `next_seq` to max raw `seq`. (b) Coalescing `[feed(1), item(2), feed(3)]` emits `[item(2), feed(3)]` (`P-12`). (c) Two-device push→pull test (`P-03`): Device B pushes, Device A pushes (no `watermark` in response), Device A pulls from its old cursor and receives B's change. (d) `category` soft-delete emits `op="delete"` even if pulled before its delete `seq`. (e) All 3 `resync_required` reasons (`epoch_mismatch`, `cursor_ahead`, `cursor_pruned` including empty-table-after-prune). |
| **1b-T05** | **`sync/push` idempotency, payload hash, concurrent retry & `expired` transitions** | Integration (Real PG) | (a) Duplicate `client_mutation_id` (same device + same `payload_sha256` → cached replay, no duplicate side effects; same device + different `payload_sha256` → `rejected: mutation_id_conflict`; different device → `rejected: mutation_id_conflict`). (b) Two concurrent pushes of the same 50-mutation batch from the same device both return `200` with identical outcomes and single application (`P-05`). (c) Dead-RTC timestamp `< server_now - 90d` → `rejected: clock_skew`. (d) Request > 5,000 total explicit `item_ids` → `422`; body > 2 MiB → `413`. (e) Invalid `item_id` / deleted `category_id` returns `rejected` + slim `current_entity` without rolling back sibling mutations; `expired → unread` sets `unexpired_at` and survives `expire_items`; starring an `expired` item leaves `state='expired'`. |
| **1b-T06** | **Bounded `mark_bulk_read` (`up_to_sync_seq`, `scope='items'` & two-order regression)** | Integration (Real PG) | Poll batch 1 (`seq <= W`), poll batch 2 (`seq > W`), push `item_mark_bulk_read(scope='feed', up_to_sync_seq=W, epoch=E)` → batch 1 marked `read`, batch 2 stays `unread`, `expired` untouched, starred unread marked `read` while staying `is_starred=True`; verify `older_than_ts`, `category` scope, `items` scope, and persistence across `sync_changes` pruning. Explicit two-order regression from `P-01`: `scope='items'` converges identically in both arrival orders; `scope='feed'` documents the accepted `unread`-only carve-out. |
| **1b-T07** | **Online LWW monotonicity, `config_version`, `poll_interval_minutes` & status side effects (`S-04`, `P-02`, `P-06`, `Q-4`)** | Integration (Real PG) | (a) Offline `feed_set_field` never bumps `config_version`; concurrent online `PATCH curation_instructions` with original `expected_config_version` succeeds. (b) Fast-laptop `P-02` regression: laptop pushes `muted@+4m`, online `PATCH active` at `t+1m`, tablet pushes `paused@+2m` in both arrival orders → both converge to `active` with non-decreasing `field_updated_at['status']`; same monotonic check for `DELETE /categories/{id}`. (c) Online `PATCH status: unsubscribed → active` runs the exact same reset + `poll_feed` enqueue as offline `feed_set_field` (`Q-4`). (d) `PATCH poll_interval_minutes` emits a `feed` `upsert` visible via `sync/pull`, and `last_fetched_at`/`last_success_at` are absent from `SyncFeedDTO` (`P-06`). |
| **1b-T08** | **500-mutation flush, response-size & 20k bulk-read benchmarks (`NFR-SYNC-02`, `P-08`, `P-09`)** | Integration (Real PG) | (a) Push 500 mixed mutations in one `POST /api/v1/sync/push` call → wall-clock `< 5.0s` (expected `< 0.5s`) and all 500 `sync_mutations` + coalesced `sync_changes` recorded cleanly. (b) Push 500 all-superseded mutations against items with 32 KB `raw_summary` → response payload `< 512 KB` (`P-08`). (c) `item_mark_bulk_read(scope='all')` over 20,000 unread items → completes `< 5.0s` with chunked `SyncLogWriter` inserts (`P-09`). |
| **1b-T09** | **DB restore-epoch end-to-end sync recovery drill (`Q-1`, `P-11`, `P-11-F1`)** | Integration (Real PG) | (a) Simulate a paired client at `seq = N, epoch = E1` with both recently-acknowledged mutations in its 48h replay journal (pushed after backup time) and pending offline outbox mutations (single-field and `scope='items'`); simulate backup restore (rewind `current_seq < N`, rotate `instance_epoch = E2`); verify `sync/pull` returns `resync_required (epoch_mismatch)`, client re-bootstraps, replays both the 48h journal and pending outbox idempotently, and converges with zero lost read/star/feed state. (b) Explicit `P-11-F1` regression: client offline at `W = 1000, epoch = E1`; server restores to `current_seq = 900, epoch = E2`; post-restore poll ingests new items at `seq = 901..950` (`fetched_at` 10:05); client had a scoped `item_mark_bulk_read(scope='all', up_to_sync_seq=1000, epoch=E1)` at 11:00 → push rejects it with `reason='epoch_mismatch'`, leaving items `901..950` `unread`, while epoch-agnostic `scope='items'` and single-field mutations in the same batch apply cleanly. |
| **1b-T10** | **PostgreSQL FTS ranking, filters, negation guard, hooks & XSS-safe snippets (`P-13`, `P-15`)** | Unit + Integration (Real PG) | Verify bearer auth required (`401` without token); `A > B > C > D` weighting (`title` beats `extracted_text`); `websearch_to_tsquery` phrases & `foo -bar`; stop-word/punctuation-only queries return empty cleanly; negation-only / non-indexable queries (`-foo`, `-foo -bar`, `-"hello world"`, `foo OR -bar`) return `422` (`P-13`); HTML tags in `raw_summary` are stripped; `poll_feed` & `extract_content` populate `search_vector`; `smartrss search reindex` works; `SearchItemDTO` omits `raw_summary`; `<script>` / `\x02` / `\x03` / `\x04` payloads and multi-fragment matches produce safe `list[list[SnippetFragment]]` preserving fragment boundaries (`P-15`). |
| **1b-T11** | **Online settings API & lock-ordered retention cleanup** | Integration (Real PG) | `GET`/`PATCH /api/v1/settings` rejects explicit `null` with `422` (`P-17`), emits diff-gated `app_settings` `upsert`; `retention_cleanup` updates `app_settings` before `sync_state` (`P-04`), prunes contiguous `sync_changes` prefix, advances `sync_state.pruned_through_seq`, and prunes `sync_mutations` older than 90 days. |
| **1b-T12** | **Alembic `0002` upgrade + downgrade round-trip** | Integration (Real PG) | Migrate `base → 0001 → seed rows (including HTML with <script>/entities) → 0002` (verifying `ingest_sync_seq` and Python-builder `search_vector` backfill match `smartrss search reindex`, `P-14`) → `downgrade 0001` → `upgrade head` cleanly. |

---

## 11. Planned High-Level Doc Alignments (Upon `1b` Approval / Implementation)

In accordance with [development-process.md §4.2](../development-process.md#42-docs-in-the-same-change-rule), the following minor refinements will be reflected in [docs/data-model.md](../data-model.md) in the same commit as `1b` approval/implementation:
- `sync_state` (§4.2): add `pruned_through_seq BIGINT NOT NULL DEFAULT 0`.
- `items` (§4.5): add `ingest_sync_seq BIGINT NOT NULL DEFAULT 0`.
- `sync_mutations` (§4.9): mark `entity_id` nullable (for `scope='all'` / `scope='items'` bulk mark-read), add `payload_sha256 TEXT NOT NULL`, and add `affected_count INT NULL`.
- `feeds` (§4.4 & §5.1): document granular `research_policy.enabled` / `research_policy.fetch_pdfs` keys in `field_updated_at`, include `poll_interval_minutes` in synced feed fields, and note `last_fetched_at`/`last_success_at` as online-only.
- `ADR 0004` / `data-model.md` §5.2: document the client 48-hour acknowledged replay journal on `epoch_mismatch`, epoch-pinning of scoped `item_mark_bulk_read` (`all`/`feed`/`category` require `epoch` and reject on `epoch_mismatch` so pre-restore bounds never mark unseen post-restore items read, while `scope='items'` and single-field mutations stay epoch-agnostic; `P-11-F1`), the `server_now` clock-drift signal + 90-day dead-RTC lower bound, and the two-tier `mark_bulk_read` semantics (`scope='items'` LWW vs. scoped `unread`-only carve-out).

---

## 12. Accepted Residual Risks (Owner Sign-Off 2026-11-25)

| ID | Residual risk | Impact | Why accepted |
| :--- | :--- | :--- | :--- |
| **RR-1** | **Scoped bulk mark-read is arrival-order-dependent** (`P-01` / `Q-2`, §6.5 item 4). `scope IN ('all','feed','category')` touches only rows that are `unread` when the mutation is applied, so a concurrent offline `read → unread` (or `expired → unread`) on the same item can end `read` or `unread` depending on which device syncs first. | The server still holds one result that every device converges to. The only effect is an occasional single item with the "other" plausible read/unread label, recoverable from History or search. | A fully commutative version would have to lock and re-emit every already-`read` archive row in scope on each "Mark all read", causing large write and sync amplification. |
| **RR-2** | **`expired` items are excluded from all bulk mark-read modes**, including `scope='items'` / `mark_cluster_read` (§6.5 item 4). They stay `expired` instead of becoming `read`, with the same order-dependence as RR-1 for a concurrent `expired → unread` restore. | Archive labeling only: expired items are not in the inbox and do not count as unread. Opening an expired item or marking that single item read still works. | Keeps the "never actually read" signal (useful for `1g` evaluation) and matches `data-model.md` §5.2 ("never marks expired"). |
