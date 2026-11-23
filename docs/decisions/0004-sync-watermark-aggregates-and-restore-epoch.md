# 0004: Committed Sync Watermark, Aggregate Sync Surface & Restore Epoch

- **Status**: Accepted
- **Date**: 2026-11-22 (accepted 2026-11-23, with the transaction-boundary clarification above)

## Context
The initial design allocated `sync_seq` using a PostgreSQL `SEQUENCE` (`nextval()`). Because concurrent transactions can allocate sequence numbers in one order and commit in another, a client pulling between commits could advance its cursor past a late-committing transaction and permanently miss records. Furthermore, child entities (`item_enrichments`, `item_tags`, `research_sources`, `feedback_events`) and deletions/cluster removals lacked explicit sync contracts, and restoring PostgreSQL from a backup could rewind `sync_seq` behind a client's stored cursor.

## Decision
1. **Append-only `sync_changes` log with serialized commit-ordered watermark**:
   - Maintain a single-row `sync_state(id=1, current_seq BIGINT, instance_epoch UUID)` table and an append-only `sync_changes(seq BIGINT PK, entity_type TEXT, entity_id UUID, op TEXT, changed_at TIMESTAMPTZ)` table.
   - At the **end** of any write transaction that mutates synced state, acquire `SELECT current_seq FROM sync_state WHERE id = 1 FOR UPDATE`, increment `current_seq`, and insert the corresponding `sync_changes` row(s) before committing.
   - Because the row lock serializes commit ordering of synced writes (taking sub-millisecond time at SmartRSS's ~200 items/day single-user scale), no committed row can ever become visible with a `seq` lower than an already-committed higher `seq`. Pull queries (`WHERE seq > :since_seq ORDER BY seq ASC LIMIT :page_size`) can never skip rows.
   - **Transaction boundary is critical**: any slow I/O — above all the `LLMAdapter` call in `enrich_item`, but also outbound HTTP fetches — must complete *before* opening this final write transaction. The transaction that takes the `sync_state FOR UPDATE` lock should contain only the already-computed result write, the watermark increment, and the `sync_changes` insert, then commit immediately. Wrapping the LLM call itself inside this transaction would serialize every other synced write (item ingestion, read/star mutations, feed config changes) behind LLM latency (~10–25s/item), violating the 2-minute visibility and 5-second outbox-flush targets in `NFR-SYNC-02` — a correctness bug in the implementation, not in this design.
   - *(Alternative considered: `pg_snapshot_xmin(pg_current_snapshot())` stable-watermark filtering; rejected as more complex to reason about and test when single-user write concurrency is very low.)*
2. **Aggregate-shaped Sync Surface & Explicit Tombstones**:
   - Synced entities are explicitly enumerated in `docs/data-model.md` §5.
   - `item` is synced as a **self-contained aggregate** embedding its current `item_enrichment`, LLM `tags`, `research_sources` summaries, `cluster_id`, and current user `feedback` state. Any change to a child table records an `upsert` event for the parent `item` in `sync_changes`.
   - Removals (soft-deleted `category`, emptied/merged `cluster`, unsubscribed/purged feed visibility) emit explicit `delete` (tombstone) rows in `sync_changes`.
3. **Bootstrap Snapshot, Cache Eviction & Restore `instance_epoch`**:
   - New clients (or clients requiring resync) call `GET /api/v1/sync/bootstrap`, receiving the active working set (all feeds, categories, active interest profile, app settings, all `unread` + `starred` items, and `read` items from the last 14 days) along with the exact `watermark` (`current_seq`) and `instance_epoch`.
   - Every `bootstrap` and `pull` response includes `instance_epoch`. On PostgreSQL backup restore (or DB re-init), the restore procedure rotates `instance_epoch` (and clients also detect `client_cursor > server_watermark`).
   - If `instance_epoch` changes or cursor regresses, the client retains its local unsynced mutation outbox, re-bootstraps cleanly from the server, and then flushes the outbox idempotently.
   - Clients locally evict `read`/`expired` unstarred items older than 14 days (while ignoring incoming deltas for evicted out-of-window items unless the item transitions back to `unread` or `starred`).

## Consequences
- **Positive**: Provably gap-free, strictly commit-ordered delta sync; dramatically simpler client state handling via aggregate item payloads; clean tombstone propagation; resilient to database backup restores.
- **Trade-offs**: Write transactions that touch synced state serialize briefly at commit time (negligible at single-user scale); `sync_changes` requires periodic retention pruning (e.g., 90 days—any client offline longer than the `sync_changes` retention window receives a `resync_required` signal and falls back to `bootstrap`).
