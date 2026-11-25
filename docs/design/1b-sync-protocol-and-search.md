# Component Design 1b: Server Sync Engine, Conflict Resolution & Full-Text Search

> **Status:** In Design Review *(2026-11-25 — owner design trade-offs confirmed; handed off for Claude Code design review)*
> **Milestone:** `1b` — Server Sync Engine, Conflict Resolution & Search ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-STATE-01`–`02` (sync/restore transitions), `FR-SEARCH-01`, `FR-SYNC-01`–`04`, `NFR-SYNC-01`, `NFR-SYNC-02` (500-mutation `< 5s` flush), `NFR-SEC-01`, `NFR-SEC-03` (XSS-safe search snippets)
> **Related ADRs:** [ADR 0002](../decisions/0002-native-sync-api-and-detached-client-model.md), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)
> **Carry-over Design Items Resolved:** `S-04` (`feeds.config_version` independence from `field_updated_at`), `S-07` (bounded cursor-paginated `bootstrap`) from [Second-Pass Design Review](../reviews/2026-11-23-second-pass-design-review.md)

---

## 1. Scope & Boundaries

Milestone `1b` builds the server-side synchronization engine, per-field conflict resolution, idempotent mutation ingestion, online settings/feed LWW alignment, and PostgreSQL full-text archive search on top of Milestone `1a`'s ingestion, auth, and `sync_state`/`sync_changes` foundation.

### In Scope for `1b`
1. **Alembic Migration `0002_milestone_1b_sync_and_search`**:
   - New `sync_mutations` table (idempotency log of client mutations, pruned after 90 days).
   - `sync_state.pruned_through_seq BIGINT NOT NULL DEFAULT 0` for exact retention-boundary (`resync_required`) detection.
   - `items.ingest_sync_seq BIGINT NOT NULL DEFAULT 0` (indexed) for pruning-proof, commit-ordered `mark_bulk_read`.
   - `items.search_vector TSVECTOR` + GIN index, with backfill for existing rows.
2. **Sync Engine (`/api/v1/sync/*`)**:
   - `GET /api/v1/sync/bootstrap`: Stateless keyset-paginated snapshot pinned to Page 1's `watermark` and `instance_epoch`, returning a forward-compatible aggregate wire envelope.
   - `GET /api/v1/sync/pull`: Commit-ordered delta stream from `sync_changes` with within-page coalescing, bulk hydration, tombstone propagation, and exact `resync_required` detection (`epoch_mismatch`, `cursor_ahead`, `cursor_pruned`).
   - `POST /api/v1/sync/push`: Single-transaction batch mutation processor (up to 500 mutations/request, `< 5s` target) with `client_mutation_id` idempotency, server clock-skew clamping (`+5 min`), deterministic value-rank LWW tie-breaking, bounded `mark_bulk_read`, and per-mutation `{applied | superseded | rejected}` outcomes.
3. **Online Settings & LWW Consistency**:
   - `GET /api/v1/settings` and `PATCH /api/v1/settings` (online-only `app_settings` management emitting `app_settings` `upsert` in `sync_changes`).
   - Align `PATCH /api/v1/feeds/{id}` and `POST /api/v1/feeds` reactivation so `field_updated_at` uses granular `research_policy.enabled` / `research_policy.fetch_pdfs` keys alongside existing LWW keys, while `config_version` increments **only** on `curation_instructions` writes (`S-04`).
4. **PostgreSQL Full-Text Archive Search**:
   - `GET /api/v1/search`: Full-archive search via `websearch_to_tsquery`, weighted `search_vector` (`A: title`, `B: author`, `C: HTML-stripped raw_summary`, `D: extracted_text[:65536]`), facet filters, `relevance` / `newest` sorting, and **structured XSS-safe snippet fragments** computed on the returned page only.
   - Application-code `search_vector` maintenance hooks in `poll_feed` and `extract_content`, plus `smartrss search reindex` CLI command.
5. **1a Pipeline & Maintenance Updates**:
   - `SyncLogWriter.record()` uses `clock_timestamp()` (monotonic with `seq` under `sync_state FOR UPDATE`) and chunks large multi-row `INSERT`s to stay well below PostgreSQL's 65,535 bind-parameter cap (`D1`).
   - `poll_feed` stamps `items.ingest_sync_seq` from the allocated `sync_changes.seq` inside the same final write transaction.
   - `retention_cleanup` prunes `sync_changes` strictly by a contiguous `seq` prefix, updates `sync_state.pruned_through_seq` atomically, and prunes `sync_mutations` older than 90 days.
   - Extend `sync_log.py` ORM sync guard with `ITEM_SYNCED_COLUMNS` so internal-only `Item` column updates (`search_vector`, `ingest_sync_seq`) do not spuriously require a `sync_changes` event.
6. **Verification & Benchmarks**:
   - Real-Postgres concurrent-commit `sync_seq` gap-free test, `hypothesis` permutation convergence tests (including tied/clamped timestamps), 500-mutation flush benchmark (`< 5s`), and full restore-epoch end-to-end sync recovery drill.

### Explicitly Out of Scope for `1b`
- React/TypeScript/Dexie client UI, Service Worker, and local outbox (`1c`).
- Database tables for `item_enrichments`, `tags`, `item_tags`, `interest_profiles`, `prompt_configs` (`1d`).
- Database tables for `clusters`, `cluster_exclusions`, `item_embeddings` and `split_cluster` mutation execution (`1e`).
- Database table for `research_sources` (`1f`).
- Database table for `feedback_events` and `item_feedback` / `feed_instruction_*` mutation execution (`1g`).

---

## 2. Key `1b` Design Trade-offs & Decisions

All seven trade-offs below were reviewed and confirmed with the project owner on 2026-11-25.

| # | Decision Area | Chosen Approach | Alternative(s) Considered | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **B-01** *(S-07)* | **Bootstrap pagination & snapshot consistency** *(Confirmed)* | **Stateless keyset pagination pinned to Page 1's `watermark`**: Page 1 captures `{watermark, instance_epoch}` first and returns metadata + first item page (`limit=500`, `ORDER BY id ASC`); Pages 2..N fetch remaining items by `cursor`; client then runs `sync/pull?since_seq=<page1_watermark>`. | (a) Single unpaginated response with hard cap; (b) server-side frozen snapshot session. | Zero server state, bounded HTTP/memory footprint even for 20k+ items, and mathematically gap-free because any write after Page 1's watermark has `seq > watermark` and is replayed idempotently by the trailing `pull`. |
| **B-02** | **Item sync payload scope (`extracted_text` & `raw_summary`)** *(Confirmed)* | **Metadata + capped `raw_summary` (32,768 chars + `summary_truncated`) + derived `lead_excerpt` (1,500 chars plain text); omit full `extracted_text`**. | (a) Truncated 8–16 KB `extracted_text` in sync; (b) full `extracted_text` in sync. | Keeps 2,000-item client cache at ~4–8 MB (ideal for Ubuntu Dexie and Phase 2 Android), respects ADR 0002's metadata-only boundary, and still powers strong detached search while full-archive body search runs via Postgres FTS. |
| **B-03** | **`sync/pull` coalescing & `resync_required` detection** *(Confirmed)* | **Within-page coalescing** (fetch `LIMIT + 1` raw rows, advance `next_seq` to max raw `seq`, collapse per `(entity_type, entity_id)` keeping highest `seq`/`op`) + **`sync_state.pruned_through_seq`** returning HTTP `200` `{resync_required: true, resync_reason}`. | (a) Uncoalesced raw log replay; (b) `MIN(sync_changes.seq)` check + HTTP `410`. | Avoids 3–4× redundant serialization per item; `pruned_through_seq` is 100% deterministic even if `sync_changes` becomes empty after 90 idle days; HTTP `200` distinguishes expected recovery from transport errors. |
| **B-04** | **`sync/push` atomicity & equal-timestamp LWW tie-breaking** *(Confirmed)* | **Single DB transaction per push batch** (max 500 mutations, pre-validated so bad mutations record `rejected` without aborting the batch, single `SyncLogWriter.record()` at commit) + **deterministic value-rank tie-breaker** on `effective_ts == stored_ts`. | (a) One transaction per mutation; (b) `last_mutation_id` columns; (c) naive `>=` / `>`. | Single transaction clears the `< 5s` 500-mutation target (~50–150 ms) with minimal `sync_state` lock contention; value-rank tie-breaking needs zero extra columns and guarantees strict arrival-order-independent convergence (`hypothesis`). |
| **B-05** | **Bounded `mark_bulk_read` (`up_to_sync_seq`)** *(Confirmed)* | **Add `items.ingest_sync_seq BIGINT NOT NULL DEFAULT 0`** (indexed; stamped by `poll_feed` from its allocated `sync_changes.seq`), supporting both scoped `{scope, up_to_sync_seq, older_than_ts?}` and explicit `item_ids[]`. | (a) Explicit `item_ids[]` only; (b) `fetched_at <= client_ts`. | Survives 90-day `sync_changes` pruning permanently, avoids huge UUID arrays for "Mark all read", and gives exact commit-order bounds immune to clock skew. |
| **B-06** | **Forward-compatible sync surface vs. stub tables** *(Confirmed)* | **Finalize the forward-compatible JSON wire envelope in `1b`** (`clusters: []`, `interest_profile: null`, item `enrichment: null` / `tags: []` / `research_sources: []` / `user_feedback: null`, feed `active_feed_instructions: []`); **add only `1b`-owned DB tables/columns** now and defer `1d`–`1g` tables to their milestones. | Create empty stub tables for `1d`–`1g` in `1b`. | Gives `1c` a stable TypeScript/Dexie contract without locking `1d`–`1g` schemas before their component design reviews. |
| **B-07** | **Full-text search snippets & query parsing** *(Confirmed)* | **`websearch_to_tsquery` + structured XSS-safe snippet fragments** (`list[{text, highlight}]`) built via `ts_headline` with control-character sentinels on the returned page only; HTML-stripped `raw_summary` and 64 KB `extracted_text` slice in `search_vector`. | (a) Raw HTML `<mark>` snippets; (b) no snippets; (c) `plainto_tsquery`. | Natural query UX without syntax-error crashes, zero HTML emitted by the search API (zero XSS surface even for articles discussing `<script>` tags), and fast page-scoped headline generation. |

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
│   │   └── sync_log.py                      # clock_timestamp(), chunked inserts (D1), ITEM_SYNCED_COLUMNS
│   ├── domain/
│   │   ├── sync_merge.py                    # Pure LWW clock-skew clamp, value-rank tie-breakers, HTML stripper, lead_excerpt & summary truncation
│   │   └── fts_text.py                      # Pure FTS language allow-list, structured snippet parser (control-char sentinels -> fragments)
│   ├── sync/
│   │   ├── __init__.py
│   │   ├── schemas.py                       # Sync wire DTOs (bootstrap, pull, push discriminated union, forward-compatible aggregates)
│   │   └── service.py                       # Bootstrap pagination, pull coalescing + hydration, single-tx push engine & bulk mark-read
│   ├── search/
│   │   ├── __init__.py
│   │   └── fts.py                           # rebuild_item_search_vector(), bulk reindex helper, GET /api/v1/search SQL execution
│   ├── api/
│   │   ├── app.py                           # Mount sync, search, and settings routers
│   │   ├── schemas.py                       # Add AppSettingsResponse / AppSettingsUpdateRequest & search response DTOs
│   │   └── routes/
│   │       ├── feeds.py                     # Normalize granular research_policy.* LWW timestamps in field_updated_at (S-04)
│   │       ├── settings.py                  # New: GET /api/v1/settings, PATCH /api/v1/settings
│   │       ├── sync.py                      # New: GET /bootstrap, GET /pull, POST /push
│   │       └── search.py                    # New: GET /api/v1/search
│   ├── worker/stages/
│   │   ├── poll_feed.py                     # Stamp items.ingest_sync_seq + rebuild search_vector on insert/update
│   │   ├── extract_content.py               # Rebuild search_vector when extracted_text is written
│   │   └── maintenance.py                   # Contiguous-prefix sync_changes prune + sync_state.pruned_through_seq + sync_mutations 90d prune
│   └── cli.py                               # Add `smartrss search reindex` CLI command
└── tests/
    ├── unit/
    │   ├── test_sync_merge_properties.py    # Hypothesis permutation convergence, clock-skew clamping, tie-breakers, HTML strip / truncation
    │   └── test_fts_text.py                 # FTS language allow-list, HTML stripping, XSS-safe snippet fragment parsing
    └── integration/
        ├── test_sync_watermark_concurrency.py # Real-Postgres interleaved commit-order gap-free pull test
        ├── test_sync_bootstrap_and_pull.py  # Multi-page bootstrap + mid-bootstrap writes, coalescing, tombstones, resync_required (all 3 reasons)
        ├── test_sync_push_and_conflicts.py  # Idempotency, cross-device conflict, LWW item/feed merges, expired->unread, bulk mark-read, 500-mutation <5s benchmark
        ├── test_sync_restore_epoch_drill.py # Simulated DB restore rewind + epoch rotation + client outbox survival & idempotent flush
        └── test_search_fts.py               # Weighted ranking, websearch phrases/negation, stop-words, filters, XSS-safe snippets, poll/extract hooks, reindex CLI
```

### Configuration Additions (`config.py` & `.env.example`)
| Variable | Default | Validation / Notes |
| :--- | :--- | :--- |
| `FTS_LANGUAGE` | `english` | Must be in explicit allow-list `{"english", "simple", "danish", "dutch", "finnish", "french", "german", "hungarian", "italian", "norwegian", "portuguese", "romanian", "russian", "spanish", "swedish", "turkish"}`. Never interpolated raw into SQL; passed as a validated Postgres `regconfig` literal/parameter. Changing it requires running `smartrss search reindex`. |

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
- **`items.ingest_sync_seq`**: Backfill existing rows with the minimum `sync_changes.seq` for `(entity_type = 'item', entity_id = items.id)`, defaulting to `0` if no `sync_changes` row exists (so pre-migration items are always included in any `up_to_sync_seq >= 0` bulk mark-read).
- **`items.search_vector`**: Backfill existing rows in `0002` using SQL `setweight(to_tsvector(:cfg, ...))` with `LEFT(COALESCE(extracted_text, ''), 65536)` and basic HTML-tag stripping (`regexp_replace(COALESCE(raw_summary, ''), '<[^>]+>', ' ', 'g')`), matching the Python builder's weights. New/updated items are maintained by the Python `rebuild_item_search_vector()` helper.

---

## 5. Sync Log, Locking & Retention Invariants

### 5.1 Global Lock Ordering (Deadlock Prevention)
All database transactions across API and worker MUST acquire row locks in this strict hierarchy:
1. `categories` (by `id ASC`)
2. `feeds` (by `id ASC`)
3. `items` (by `id ASC`)
4. `sync_state` (`id = 1`, **always last**, immediately before `COMMIT`)

No slow I/O (HTTP fetch, subprocess extraction, or future LLM inference) may ever occur inside a transaction holding any of these locks (`ADR 0004`, `S-02`).

### 5.2 `SyncLogWriter` Hardening (`db/sync_log.py`)
- **Monotonic `changed_at`**: `SyncLogWriter.record()` explicitly writes `changed_at = sa.func.clock_timestamp()` (instead of transaction-start `now()`) while holding `sync_state FOR UPDATE`, ensuring `changed_at` is monotonically non-decreasing with `seq`.
- **Chunked Inserts (`D1`)**: When `len(events) > 1000` (e.g., a large `mark_bulk_read`), `SyncLogWriter.record()` inserts into `sync_changes` in chunks of `1000` rows (4,000 bind parameters per chunk, far below PostgreSQL's 65,535 limit) within the same transaction while holding the single `sync_state FOR UPDATE` lock.
- **`ITEM_SYNCED_COLUMNS`**: Add explicit `ITEM_SYNCED_COLUMNS` to `_entity_requires_sync()` (excluding server-only `search_vector`, `ingest_sync_seq`, `updated_at`), matching `FEED_SYNCED_COLUMNS` and `APP_SETTINGS_SYNCED_COLUMNS`.

### 5.3 `poll_feed` `ingest_sync_seq` Stamping
In `poll_feed.py`, immediately after `seqs = await sync_log.record(session, all_events)` returns inside the final short write transaction:
- Map each newly inserted `item_id` (from `inserted_ids`) to its allocated `seq` from `seqs`.
- Execute a single `UNNEST`-based `UPDATE` via `execute_synced(..., lambda _res: [])` (2 bind parameters total regardless of batch size):
  ```sql
  UPDATE items
  SET ingest_sync_seq = v.seq
  FROM (SELECT unnest(:ids::uuid[]) AS id, unnest(:seqs::bigint[]) AS seq) AS v
  WHERE items.id = v.id
  ```
- Existing items updated via `entry_hash` change keep their original `ingest_sync_seq` (their initial ingest order never regresses).

### 5.4 Contiguous-Prefix Retention Cleanup (`worker/stages/maintenance.py`)
To eliminate any possibility of holes in `sync_changes` and keep `pruned_through_seq` exact:
1. Compute the contiguous prefix cutoff inside the `retention_cleanup` transaction:
   ```sql
   SELECT MAX(seq) FROM sync_changes WHERE changed_at < now() - INTERVAL '90 days'
   ```
2. If `cutoff_seq IS NOT NULL`:
   - Delete strictly by prefix: `DELETE FROM sync_changes WHERE seq <= :cutoff_seq`.
   - Atomically advance `sync_state`:
     ```sql
     UPDATE sync_state
     SET pruned_through_seq = GREATEST(pruned_through_seq, :cutoff_seq),
         updated_at = now()
     WHERE id = 1
     ```
     *(Acquiring the `sync_state` row lock at the end of `retention_cleanup` respects the global lock order and prevents races with concurrent `sync/pull` reads.)*
3. Delete `sync_mutations` older than 90 days:
   ```sql
   DELETE FROM sync_mutations WHERE server_applied_at < now() - INTERVAL '90 days'
   ```
4. Update `app_settings.last_retention_cleanup_at = now()` (internal operational column, zero sync events).

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
  "last_fetched_at": "2026-11-25T12:00:00Z | null",
  "last_success_at": "2026-11-25T12:00:00Z | null",
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
*(In `1b`, `active_feed_instructions` is always `[]`; populated by `1g`.)*

#### `SyncItemDTO` *(Metadata-Only Aggregate — Decision B-02 & B-06)*
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
- **`raw_summary` capping**: Truncated at `32_768` characters if longer, setting `summary_truncated = true`; otherwise passed verbatim with `summary_truncated = false`. Full `extracted_text` is **omitted** from sync payloads (stays server-only for Postgres FTS and `GET /api/v1/items/{id}`).
- **`lead_excerpt` derivation**: Derived in pure Python (`domain/sync_merge.py`):
  - Source text: `extracted_text` if non-empty, else HTML-stripped `raw_summary`.
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
  - Executes inside a single `REPEATABLE READ` read-only transaction so `sync_state` (`watermark`, `instance_epoch`), metadata collections, and the first page of items come from a consistent DB snapshot.
  - Returns full `categories` (`WHERE deleted_at IS NULL ORDER BY sort_order ASC, id ASC`), `feeds` (`ORDER BY id ASC`, including `unsubscribed` so clients know feed metadata for retained starred items and re-subscribe state), `clusters: []`, `interest_profile: null`, `app_settings`, and the first `limit` working-set `items`.
- **Pages 2..N (`cursor` provided)**:
  - Returns `instance_epoch`, `watermark: null` *(omitted deliberately so clients can never accidentally overwrite Page 1's pinned `watermark` with a later sequence and introduce a sync gap)*, `categories: null`, `feeds: null`, `clusters: null`, `interest_profile: null`, `app_settings: null`, plus the next `limit` working-set `items` where `items.id > :cursor`.
- **Working-Set Item Filter** (uses PostgreSQL `now()`, `C5`):
  ```sql
  SELECT *
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
- **Client Bootstrap Protocol (Normative for `1c` and Phase 2)**:
  1. Call Page 1 (`cursor` omitted). Save `page1_epoch = instance_epoch` and `page1_watermark = watermark`.
  2. While `has_more == true`, call `GET /api/v1/sync/bootstrap?cursor=<next_cursor>&limit=500`. If `response.instance_epoch != page1_epoch` on any page, abort and restart from Step 1.
  3. Commit local working set + `cursor = page1_watermark` + `epoch = page1_epoch`.
  4. Immediately run `GET /api/v1/sync/pull?since_seq=<page1_watermark>&epoch=<page1_epoch>` to converge any changes committed during bootstrap pagination.
  5. If the client retained an unsynced local outbox (e.g., after `resync_required` restore recovery), flush it via `POST /api/v1/sync/push` after bootstrap completes.

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
  "watermark": 1250,
  "next_seq": 1250,
  "has_more": false,
  "changes": []
}
```

**Exact Server Execution Algorithm**
1. Execute inside a single `REPEATABLE READ` read-only transaction (prevents reading a `sync_state` row from snapshot T1 and `sync_changes` rows from snapshot T2!).
2. Read `sync_state WHERE id = 1`: `(current_seq, instance_epoch, pruned_through_seq)`.
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
6. **Within-page coalescing (Decision B-03)**:
   - Iterate `page_rows` in ascending `seq` order and keep the **latest** row per `(entity_type, entity_id)`.
   - Sort the surviving deduplicated entries by `seq ASC` so `changes` stays strictly ordered by `seq`.
7. **Bulk hydration** (grouped by `entity_type`, chunked at 500 IDs for `D1` safety):
   - For `op == "delete"`: emit `data = None`.
   - For `op == "upsert"`:
     - `category`: load from `categories WHERE id = ANY(:ids)`. If missing or `deleted_at IS NOT NULL` (deleted by a later transaction beyond this page), emit `op = "delete", data = None` so clients never see a soft-deleted row as active (the later `delete` tombstone on a subsequent page is an idempotent no-op).
     - `feed`: load from `feeds WHERE id = ANY(:ids)` → `SyncFeedDTO`. (If missing, emit `op = "delete", data = None`.)
     - `item`: load from `items WHERE id = ANY(:ids)` → `SyncItemDTO`. (If missing, emit `op = "delete", data = None`.)
     - `app_settings`: load `app_settings WHERE id = 1` → `SyncAppSettingsDTO`.
     - Future types (`cluster`, `interest_profile`): handled cleanly when `1d`/`1e` introduce their tables.

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
- **Batch bounds**: `0 <= len(mutations) <= 500`. Requests exceeding 500 mutations return `422 Unprocessable Entity` (client chunks larger outboxes into batches of ≤ 500).
- **Duplicate `client_mutation_id` inside the same request array**: The first occurrence processes normally; any duplicate within the same request array receives the identical replay outcome without double-applying.
- **Timezone requirement**: `client_timestamp` MUST be timezone-aware (reject naive datetimes with `422`).

#### Supported Mutation Types in `1b` (Discriminated Union on `type`)

| `type` | Payload Fields | Validation & Conflict Policy |
| :--- | :--- | :--- |
| **`item_set_state`** | `item_id: UUID`, `state: Literal["read", "unread"]` | Per-field LWW comparing `(effective_ts, state_rank(new))` vs `(item.read_state_updated_at, state_rank(item.state))`. Rejects if `item_id` not found (`entity_not_found`). |
| **`item_set_starred`** | `item_id: UUID`, `is_starred: bool` | Per-field LWW comparing `(effective_ts, star_rank(new))` vs `(item.starred_updated_at, star_rank(item.is_starred))`. Orthogonal to `state`; starring an `expired` item succeeds and keeps `state='expired'`. Rejects if `item_id` not found (`entity_not_found`). |
| **`item_mark_bulk_read`** | `scope: Literal["all", "feed", "category", "items"]`, `up_to_sync_seq: int \| null`, `feed_id: UUID \| null`, `category_id: UUID \| null`, `older_than_ts: datetime \| null`, `item_ids: list[UUID] \| null` | See §6.5 below. |
| **`feed_set_field`** | `feed_id: UUID`, `field: Literal["status", "category_id", "expiration_days_override", "research_policy.enabled", "research_policy.fetch_pdfs"]`, `value: Any` (typed per field) | Per-field LWW comparing `(effective_ts, field_rank(new))` vs `(stored_ts, field_rank(current))`. See §6.6 below. |

*Reserved for future milestones (`1e`: `split_cluster`; `1g`: `item_feedback`, `feed_instruction_add`, `feed_instruction_archive`). Any unknown `type` fails Pydantic validation (`422`), or if explicitly listed as a reserved future type returns `outcome = "rejected", reason = "unsupported_mutation_type"`.*

**Response Schema (`200 OK`)**
```json
{
  "instance_epoch": "uuid",
  "watermark": 1260,
  "results": [
    {
      "client_mutation_id": "uuid",
      "outcome": "applied | superseded | rejected",
      "reason": "string | null",
      "affected_count": "int | null",
      "current_entity_type": "item | feed | null",
      "current_entity": { "...SyncItemDTO or SyncFeedDTO..." }
    }
  ]
}
```
- **`current_entity` population policy**:
  - **`applied`**: `current_entity = None`, `current_entity_type = None` (keeps a 500-mutation flush response lean; `affected_count` is populated for `item_mark_bulk_read`).
  - **`superseded`** or **`rejected`**: Populate `current_entity_type` + `current_entity` whenever the target entity exists (so the client immediately reconciles or rolls back its Dexie record without waiting for `pull`); `None` if `reason == "entity_not_found"`.

---

### 6.5 LWW Rules, Clock-Skew Clamping & Tie-Breaking (`domain/sync_merge.py`)

Pure deterministic functions with zero DB dependencies (tested exhaustively with `hypothesis`):

1. **Clock-Skew Clamping (`C5`)**:
   - At the start of `POST /api/v1/sync/push`, query `server_now = (await session.execute(sa.select(sa.func.now()))).scalar_one()`.
   - For every mutation:
     ```python
     max_allowed = server_now + timedelta(minutes=5)
     effective_ts = min(client_timestamp.astimezone(UTC), max_allowed)
     ```
2. **Deterministic Value-Rank LWW Comparator (Decision B-04)**:
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
     - Else (`incoming_key <= stored_key`): leave DB unchanged, return **`superseded`** with `current_entity`.
   - **Proof of Commutativity**: Because `(effective_ts, rank(value))` defines a strict total order on `(timestamp, value)` pairs for every field, taking the maximum over any finite set of mutations yields the exact same `(timestamp, value)` regardless of arrival order or duplicates.
3. **Side-Effect Timestamps on `item_set_state` & `item_set_starred`**:
   - When `item_set_state` wins and sets `state = "read"`:
     - `item.state = "read"`, `item.read_at = effective_ts`, `item.read_state_updated_at = effective_ts`, `item.unexpired_at = None`, `item.updated_at = server_now`.
   - When `item_set_state` wins and sets `state = "unread"`:
     - If `item.state == "expired"` or `item.expired_at is not None`: set `item.unexpired_at = effective_ts` (resets expiration grace clock per `data-model.md` §3.1 & §6.1 so `expire_items` does not immediately re-expire a restored item, and keeps every column—including `unexpired_at`—order-independent across concurrent `read`/`unread` permutations).
     - `item.state = "unread"`, `item.read_at = None`, `item.read_state_updated_at = effective_ts`, `item.updated_at = server_now`.
   - When `item_set_starred` wins:
     - `item.is_starred = new_starred`, `item.starred_at = effective_ts if new_starred else None`, `item.starred_updated_at = effective_ts`, `item.updated_at = server_now`. (`item.state` is untouched.)
4. **Bounded `item_mark_bulk_read` Rules (Decision B-05)**:
   - **Validation**:
     - `scope == "all"`: requires `up_to_sync_seq is not None` (`>= 0`); `feed_id`, `category_id`, `item_ids` must be `None`.
     - `scope == "feed"`: requires `feed_id` and `up_to_sync_seq >= 0`; rejects with `entity_not_found` if feed doesn't exist.
     - `scope == "category"`: requires `category_id` and `up_to_sync_seq >= 0`; rejects with `category_not_found` if category doesn't exist or `deleted_at IS NOT NULL`.
     - `scope == "items"`: requires non-empty `item_ids` (`1 <= len(item_ids) <= 1000`); `up_to_sync_seq` optional/ignored.
   - **Matching filter**:
     - Always `items.state = 'unread'` (never touches `read` or `expired`; starred unread items DO transition to `read` while staying `is_starred = True`).
     - Scoped modes (`all`/`feed`/`category`): `items.ingest_sync_seq <= :up_to_sync_seq`.
     - Optional `older_than_ts` (timezone-aware): `COALESCE(items.published_at, items.fetched_at) < :older_than_ts`.
     - Per-item LWW guard: `(effective_ts > items.read_state_updated_at) OR (effective_ts = items.read_state_updated_at)` (since `rank('read') = 3 > rank('unread') = 2`, on `state = 'unread'` an equal timestamp always wins!).
   - **Execution & Outcome**:
     - Locks matching `items` rows in `id ASC` order (respecting §5.1 lock ordering), updates `state = 'read', read_at = effective_ts, read_state_updated_at = effective_ts, updated_at = server_now`, records `SyncEvent("item", id, "upsert")` for every modified item.
     - Records `outcome = "applied"`, `affected_count = <modified_rows>` (even if `0` items matched — e.g., already read by another device — the mutation is valid and `applied` with `affected_count = 0`, never `rejected`).

### 6.6 `feed_set_field` & Online `PATCH /api/v1/feeds/{id}` Alignment (`S-04`)

- **Allowed `feed_set_field` fields & validation**:
  | `field` | Expected `value` | Rejection Cases | Side Effects on Apply |
  | :--- | :--- | :--- | :--- |
  | `status` | `"active" \| "paused" \| "muted" \| "unsubscribed"` | Invalid enum → `invalid_field_value` | If `unsubscribed → active`: reset `etag=None`, `last_modified=None`, `health_state='healthy'`, `consecutive_errors=0`, `last_error=None`, `initial_backfill_done=False`, `next_fetch_at=server_now`, and enqueue `poll_feed` (`priority=10`, `bump_existing=True`). If `paused → active\|muted`: set `next_fetch_at = LEAST(COALESCE(next_fetch_at, server_now), server_now)`. |
  | `category_id` | `UUID \| null` | Non-null UUID missing or `deleted_at IS NOT NULL` → `category_not_found` | Sets `feed.category_id = value`. |
  | `expiration_days_override` | `int (0..3650) \| null` | Out of range / non-int → `invalid_field_value` | Sets `feed.expiration_days_override = value`. |
  | `research_policy.enabled` | `bool` | Non-bool → `invalid_field_value` | Updates `feed.research_policy["enabled"] = value` (preserving sibling keys). |
  | `research_policy.fetch_pdfs` | `bool` | Non-bool → `invalid_field_value` | Updates `feed.research_policy["fetch_pdfs"] = value` (preserving sibling keys). *(In `1g`, atomically logs `config_fetch_pdfs_audit` in `feedback_events`.)* |
- **Stored LWW timestamp lookup**:
  - Read `field_updated_at.get(field)`; for `research_policy.<subkey>`, fall back to `field_updated_at.get("research_policy")` if the granular key is absent (backward compatibility with `1a` rows). If neither exists, treat stored timestamp as `feed.created_at`.
  - On win, write `field_updated_at[field] = effective_ts.isoformat()`.
- **`config_version` independence (`S-04`)**:
  - `feed_set_field` NEVER reads or increments `feeds.config_version`.
  - In online `PATCH /api/v1/feeds/{id}` (`routes/feeds.py`):
    - `config_version` increments **only** when `curation_instructions` actually changes (requires matching `expected_config_version`, returning `409 Conflict` on mismatch).
    - When `research_policy` is updated via `PATCH /api/v1/feeds/{id}` or `POST /api/v1/feeds` reactivation, stamp granular `field_updated_at["research_policy.enabled"]` and `field_updated_at["research_policy.fetch_pdfs"]` for each subfield that changed (plus `field_updated_at["research_policy"]` for non-LWW subkeys `guidance`/`max_hops`), ensuring online edits properly beat older offline toggles.

### 6.7 Single-Transaction Push Execution Flow (`sync/service.py`)

```text
1. Begin transaction. Read server_now = SELECT now().
2. Bulk-load existing sync_mutations WHERE client_mutation_id = ANY(batch_ids) (chunked at 500).
   - Maintain a `seen_in_batch: dict[UUID, SyncPushResult]` map so duplicate
     client_mutation_ids within the same request array reuse the first occurrence's
     outcome without inserting a duplicate PK into sync_mutations.
   - For any mutation_id already persisted in sync_mutations:
     - Same device_id → prepare cached replay outcome (+ fresh current_entity if superseded/rejected).
     - Different device_id → prepare outcome="rejected", reason="mutation_id_conflict".
3. Collect all non-replayed target IDs up front to preserve strict §5.1 lock ordering:
   - Active categories referenced by feed_set_field / item_mark_bulk_read (read check).
   - Feeds referenced by feed_set_field / item_mark_bulk_read → lock ORDER BY id ASC FOR UPDATE.
   - Items union: (explicit item_ids from item_set_state / item_set_starred / scope='items')
     UNION (candidate unread item_ids matching any scoped item_mark_bulk_read in this batch)
     → lock in strict ascending UUID order (ORDER BY id ASC FOR UPDATE, chunked at 500 if large).
4. Process non-replayed mutations sequentially in request array order:
   - Compute effective_ts = min(client_timestamp, server_now + 5 minutes).
   - Validate entity existence & field rules → record "rejected" without raising DB errors.
   - Apply pure LWW merge in memory/SQL against the already-locked rows → "applied" or "superseded".
   - Stage SyncMutation row (one per unique client_mutation_id) + collect SyncEvent(s) for modified entities.
5. Bulk-insert staged SyncMutation rows (chunked at 500 for D1 safety).
6. Coalesce staged SyncEvents by (entity_type, entity_id) preserving order, and call
   await sync_log.record(session, coalesced_events) ONCE (acquires sync_state FOR UPDATE last).
7. Read final watermark = current_seq (from sync_state) and instance_epoch.
8. Commit transaction and return SyncPushResponse.
```

---

## 7. Online Settings API (`routes/settings.py`)

Exposes `app_settings` over REST (previously CLI-only in `1a`) so clients can inspect and modify global settings online:

- **`GET /api/v1/settings`** (authenticated): Returns `SyncAppSettingsDTO` (`default_expiration_days`, `backfill_cap`, `default_poll_interval_minutes`, `updated_at`).
- **`PATCH /api/v1/settings`** (authenticated, JSON-only):
  - Accepts optional fields: `default_expiration_days (0..3650)`, `backfill_cap (0..500)`, `default_poll_interval_minutes (5..10080)`.
  - Uses `model_fields_set` (`D3`), acquires `AppSettings WHERE id = 1 FOR UPDATE`, updates only changed fields, emits `SyncEvent("app_settings", APP_SETTINGS_ENTITY_ID, "upsert")` via `sync_log.record()` **only if at least one synced value actually changed** (`D2`), and commits.

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
- **SQL Expression** (`rebuild_item_search_vector(session, item_id, language=...)` and batch variant for `poll_feed`):
  ```sql
  UPDATE items
  SET search_vector =
      setweight(to_tsvector(:fts_cfg::regconfig, COALESCE(:title, '')), 'A') ||
      setweight(to_tsvector(:fts_cfg::regconfig, COALESCE(:author, '')), 'B') ||
      setweight(to_tsvector(:fts_cfg::regconfig, COALESCE(:summary_plain, '')), 'C') ||
      setweight(to_tsvector(:fts_cfg::regconfig, COALESCE(:body_slice, '')), 'D')
  WHERE id = :item_id
  ```
- **Maintenance Hooks**:
  - `poll_feed.py`: Compute `search_vector` when inserting new items (chunked batch `UPDATE` or SQL expression in `INSERT`) and when updating an existing item on `entry_hash` change.
  - `extract_content.py`: Rebuild `search_vector` inside `_set_extraction_status()` when `extraction_status == "ok"` and `extracted_text` is persisted.
  - **Sync guard**: Rebuilding `search_vector` alone (e.g., `smartrss search reindex`) uses `execute_synced(..., lambda _res: [])` — zero `sync_changes` emitted.
- **CLI Command (`smartrss search reindex`)**:
  - Rebuilds `items.search_vector` in batches of `500` items (committed per batch so it never holds long locks or hits memory limits), using the configured `FTS_LANGUAGE`. Useful if `FTS_LANGUAGE` changes or after manual DB maintenance.

### 8.2 `GET /api/v1/search`

**Query Parameters**
| Param | Type | Default | Constraints / Behavior |
| :--- | :--- | :--- | :--- |
| `q` | `str \| null` | `null` | `max_length=500`. Parsed via `websearch_to_tsquery(:fts_cfg::regconfig, :q)`. If provided and blank/whitespace-only, treated as `None`; if non-blank but reduces to an empty `tsquery` (e.g. only stop-words `"the and"` or punctuation `"!!!"`), returns `results=[], total=0` immediately. |
| `feed_id` | `UUID \| null` | `null` | Exact match on `items.feed_id`. |
| `category_id` | `UUID \| null` | `null` | Matches items whose feed has `feeds.category_id == :category_id`. |
| `state` | `Literal["unread", "read", "expired"] \| null` | `null` | Exact match on `items.state`. |
| `is_starred` | `bool \| null` | `null` | Exact match on `items.is_starred`. |
| `date_from` | `datetime \| null` | `null` | Timezone-aware lower bound on `COALESCE(items.published_at, items.fetched_at) >= :date_from`. |
| `date_to` | `datetime \| null` | `null` | Timezone-aware upper bound on `COALESCE(items.published_at, items.fetched_at) <= :date_to`. |
| `sort` | `Literal["relevance", "newest"]` | `"relevance"` | When `q` is omitted, `"relevance"` automatically falls back to `"newest"`. `"relevance"` orders by `ts_rank_cd(search_vector, tsq) DESC, COALESCE(published_at, fetched_at) DESC, id DESC`. `"newest"` orders by `COALESCE(published_at, fetched_at) DESC, id DESC`. |
| `limit` | `int` | `20` | `1 <= limit <= 100`. |
| `offset` | `int` | `0` | `0 <= offset <= 10000`. |

**Structured XSS-Safe Snippets (Decision B-07)**
- Computed **only** for the `limit` rows returned on the current page (never over the full match set), and only when `q` produced a non-empty `tsquery`.
- Source text per matched row: plain text built from `extracted_text[:65536]` if non-empty, else HTML-stripped `raw_summary[:65536]` if non-empty, else `title`, with any literal `\x02` (`STX`) and `\x03` (`ETX`) control characters stripped first.
- PostgreSQL `ts_headline` runs with:
  ```sql
  ts_headline(
    :fts_cfg::regconfig,
    :plain_text,
    :tsq,
    'StartSel=' || chr(2) || ', StopSel=' || chr(3) || ', MaxWords=35, MinWords=15, MaxFragments=2, FragmentDelimiter= … '
  )
  ```
- Python (`domain/fts_text.py`) parses the returned string on `\x02` / `\x03` boundaries into a list of typed fragments:
  ```json
  "snippet": [
    { "text": "PostgreSQL ", "highlight": false },
    { "text": "pgvector", "highlight": true },
    { "text": " HNSW indexes accelerate nearest-neighbor search", "highlight": false }
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
      "item": { "...SyncItemDTO..." },
      "rank": 0.4125,
      "snippet": [
        { "text": "...", "highlight": false },
        { "text": "pgvector", "highlight": true }
      ]
    }
  ]
}
```

---

## 9. Security, Edge Cases & Checklist Verification

Cross-checked against [docs/review-checklist.md](../review-checklist.md):

| Checklist ID | Concern | Design Mitigation in `1b` |
| :--- | :--- | :--- |
| **S1 / S5** | Untrusted HTML/text in libraries & parser limits | HTML stripping uses stdlib `HTMLParser` (linear-time, non-recursive, never touches filesystem/URLs) on a pre-sliced input cap (`<= 256 KB`). `FTS_LANGUAGE` is validated against a strict allow-list. |
| **S6** | Size caps & JSON payload bounds | `sync/push` capped at `500` mutations; `item_ids` in `item_mark_bulk_read` capped at `1,000`; `raw_summary` capped at `32,768` chars; `lead_excerpt` at `1,500` chars; `extracted_text` omitted from sync and sliced to `65,536` chars for FTS/headline; `q` capped at `500` chars. |
| **C1 / Deadlocks** | Concurrent writes & stale reads | Strict global lock order (`categories` → `feeds` (`id ASC`) → `items` (`id ASC`) → `sync_state`). `bootstrap` Page 1 and `pull` run in single `REPEATABLE READ` read transactions so `sync_state` and rows share one snapshot. |
| **C5** | Single clock | `sync/push` reads `server_now = SELECT now()` from PostgreSQL once per batch; `bootstrap` uses DB `now() - INTERVAL '14 days'`; `SyncLogWriter` uses `clock_timestamp()` under `sync_state FOR UPDATE`. |
| **D1** | PostgreSQL 65,535 bind-param limit | `SyncLogWriter.record()` chunks `sync_changes` inserts at `1,000` rows; `pull` hydration & `push` pre-loads chunk at `500` IDs; `poll_feed` `ingest_sync_seq` update uses `UNNEST` (2 params total). |
| **D2** | Sync emission guard | `ITEM_SYNCED_COLUMNS` added to `sync_log.py`; all synced mutations (`sync/push`, `PATCH /api/v1/settings`, `PATCH /api/v1/feeds`) go through `SyncLogWriter.record()`; `search_vector` & `ingest_sync_seq` updates explicitly emit zero extra events. |
| **D3** | Defaults on partial updates | `PATCH /api/v1/settings` and `feed_set_field` apply only explicitly provided fields (`model_fields_set`), never clobbering sibling keys in `research_policy` or `app_settings`. |
| **L3** | Event-loop responsiveness | HTML stripping and snippet parsing run quickly on bounded slices (`<= 64–256 KB`); bulk `smartrss search reindex` commits in 500-row batches. |

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
| **M-01** | **Gap-free concurrent-commit `sync_seq`** | Integration (Real PG) | Two concurrent DB connections: Tx A begins first, mutates an item, pauses; Tx B begins second, mutates another item, commits; client pulls `since_seq=0` (sees Tx B); Tx A commits (receives higher `seq`); client pulls `since_seq=B.seq` → receives Tx A with zero skipped or duplicated rows. |
| **M-02** | **LWW commutativity & clock-skew convergence** | Unit (`hypothesis`) | Generate random sets of `item_set_state`, `item_set_starred`, and `feed_set_field` mutations including **identical timestamps**, out-of-order arrivals, duplicates, and far-future timestamps (`+1 year`, clamped to `server_now + 5 min`). Assert every permutation converges to the exact same final state and timestamps. |
| **M-03** | **Bootstrap pagination + mid-bootstrap writes** | Integration (Real PG) | Seed > 2 pages of mixed `unread`, `starred`, recent `read` (<= 14d), old `read` (> 14d), and `expired` items. Verify working-set filter, keyset pagination without duplicates/gaps, mid-bootstrap inserts/mutations caught by trailing `pull(page1_watermark)`, and `instance_epoch` mismatch detection mid-pagination. |
| **M-04** | **`sync/pull` coalescing, tombstones & `resync_required`** | Integration (Real PG) | Verify multiple `sync_changes` for the same item coalesce to 1 hydrated DTO while advancing `next_seq` to max raw `seq`; verify `category` soft-delete emits `op="delete"` even if pulled before its delete `seq`; verify all 3 `resync_required` reasons (`epoch_mismatch`, `cursor_ahead`, `cursor_pruned` including empty-table-after-prune). |
| **M-05** | **`sync/push` idempotency, rejections & `expired` transitions** | Integration (Real PG) | Duplicate `client_mutation_id` (same device → cached replay, no duplicate side effects; different device → `rejected: mutation_id_conflict`); invalid `item_id` / deleted `category_id` returns `rejected` + `current_entity` without rolling back valid sibling mutations in the same batch; `expired → unread` sets `unexpired_at` and survives `expire_items`; starring an `expired` item leaves `state='expired'`. |
| **M-06** | **Bounded `mark_bulk_read` (`up_to_sync_seq` & `item_ids`)** | Integration (Real PG) | Poll batch 1 (`seq <= W`), poll batch 2 (`seq > W`), push `item_mark_bulk_read(scope='feed', up_to_sync_seq=W)` → batch 1 marked `read`, batch 2 stays `unread`, `expired` untouched, starred unread marked `read` while staying `is_starred=True`; verify `older_than_ts`, `category` scope, `items` scope, and persistence across `sync_changes` pruning. |
| **M-07** | **`feeds.config_version` vs `field_updated_at` (`S-04`)** | Integration (Real PG) | Offline `feed_set_field` (`status`, `category_id`, `research_policy.fetch_pdfs`) never bumps `config_version`; concurrent online `PATCH curation_instructions` with original `expected_config_version` still succeeds; online `PATCH research_policy.fetch_pdfs` stamps granular `field_updated_at` and beats older offline toggle. |
| **M-08** | **500-mutation flush performance benchmark (`NFR-SYNC-02`)** | Integration (Real PG) | Push 500 mixed mutations in a single `POST /api/v1/sync/push` call against real PostgreSQL; assert wall-clock time `< 5.0s` (expected `< 0.5s`) and all 500 `sync_mutations` + coalesced `sync_changes` recorded cleanly. |
| **M-09** | **DB restore-epoch end-to-end sync recovery drill** | Integration (Real PG) | Simulate a paired client at `seq = N, epoch = E1` with pending offline outbox mutations; simulate backup restore (rewind `current_seq < N`, rotate `instance_epoch = E2`); verify `sync/pull` returns `resync_required (epoch_mismatch)`, client re-bootstraps, flushes outbox idempotently, and converges with zero lost mutations. |
| **M-10** | **PostgreSQL FTS ranking, filters, hooks & XSS-safe snippets** | Unit + Integration (Real PG) | Verify `A > B > C > D` weighting (`title` beats `extracted_text`), `websearch_to_tsquery` phrases & `-exclude`, stop-word-only/punctuation queries return empty cleanly, HTML tags in `raw_summary` are stripped (not matched), `poll_feed` & `extract_content` populate `search_vector`, `smartrss search reindex` works, and `<script>` / `\x02` / `\x03` payloads produce safe plain-text `SnippetFragment[]`. |
| **M-11** | **Online settings API & retention cleanup** | Integration (Real PG) | `GET`/`PATCH /api/v1/settings` emits diff-gated `app_settings` `upsert`; `retention_cleanup` prunes contiguous `sync_changes` prefix, advances `sync_state.pruned_through_seq`, and prunes `sync_mutations` older than 90 days. |
| **M-12** | **Alembic `0002` upgrade + downgrade round-trip** | Integration (Real PG) | Migrate `base → 0001 → seed rows → 0002` (verifying `ingest_sync_seq` and `search_vector` backfill) → `downgrade 0001` → `upgrade head` cleanly. |

---

## 11. Planned High-Level Doc Alignments (Upon `1b` Approval / Implementation)

In accordance with [development-process.md §4.2](../development-process.md#42-docs-in-the-same-change-rule), the following minor refinements will be reflected in [docs/data-model.md](../data-model.md) in the same commit as `1b` approval/implementation:
- `sync_state` (§4.2): add `pruned_through_seq BIGINT NOT NULL DEFAULT 0`.
- `items` (§4.5): add `ingest_sync_seq BIGINT NOT NULL DEFAULT 0`.
- `sync_mutations` (§4.9): mark `entity_id` nullable (for `scope='all'` / `scope='items'` bulk mark-read) and add `affected_count INT NULL`.
- `feeds` (§4.4): document granular `research_policy.enabled` / `research_policy.fetch_pdfs` keys in `field_updated_at`.

