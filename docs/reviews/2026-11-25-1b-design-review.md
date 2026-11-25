# Design Review — Milestone `1b` Component Design (Sync Engine, Conflict Resolution & Search)

- **Date**: 2026-11-25
- **Scope**: [`docs/design/1b-sync-protocol-and-search.md`](../design/1b-sync-protocol-and-search.md) (status `In Design Review`, commit `819152d` on branch `milestone/1b`), plus the `1a` code it extends (`backend/src/smartrss/db/sync_log.py`, `worker/stages/poll_feed.py`, `worker/stages/maintenance.py`, `worker/stages/expire_items.py`, `api/routes/feeds.py`, `api/routes/categories.py`)
- **Specs checked against**: `requirements.md`, `data-model.md`, `architecture.md`, ADRs `0002`, `0004`, `0006`, `development-process.md` §2.4 and §7, `review-checklist.md`, `1a-backend-ingestion-and-security.md`
- **Reviewer**: Claude Code (Claude Opus 5.5)
- **Finding prefix**: `P` (`P-01`–`P-17`)
- **Status**: `Verified (2026-11-25)` — all 17 findings resolved (incl. follow-up `P-11-F1`); owner accepted residual risks `RR-1`/`RR-2` (§11) and marked `1b` **`Approved`** on 2026-11-25

## Review outcome

The design is in good shape. The sync core follows ADR 0004 closely:

- `bootstrap` page 1 and `pull` each run in one `REPEATABLE READ` snapshot.
- Pages are bounded to `current_seq`, which closes the pull gap.
- `pruned_through_seq` is a better retention-boundary signal than `MIN(seq)`.
- Pinning the pull cursor to page 1's `watermark` makes stateless keyset bootstrap provably gap-free.
- Returning structured snippet fragments instead of `<mark>` HTML removes the XSS surface from the search API completely.
- Stamping `ingest_sync_seq` is the right way to keep bulk mark-read bounds valid after `sync_changes` is pruned.

The main risks are in the claim that conflict resolution converges, and in how the new paths interact with existing `1a` writers:

1. **`item_mark_bulk_read` does not commute** with `item_set_state` (`P-01`). The design's convergence proof and the `hypothesis` test (`M-02`) cover only single-field mutations.
2. **Online `PATCH /feeds` is not an LWW write** (`P-02`). It stamps `field_updated_at` with the Python clock and always overwrites the value. A field's timestamp can therefore move backwards, and an older offline toggle can then beat a newer online edit.
3. **The push response's `watermark` invites a sync gap** (`P-03`). If the `1c` client uses it as its pull cursor, it permanently skips changes from other devices.

There are also some smaller problems:

- The §5.1 lock hierarchy is claimed but not honored by existing `1a` writers, and the 500-mutation push has no retry policy (`P-04`, `P-05`).
- The feed DTO carries fields whose changes never emit a sync event (`P-06`).
- The normative SQL uses a `:param::type` bind syntax that SQLAlchemy does not recognize (`P-07`, verified).

| Severity | Count | IDs |
| :--- | :--- | :--- |
| Blocker | 0 | — |
| Major | 3 | `P-01`, `P-02`, `P-03` |
| Minor | 10 | `P-04`–`P-13` |
| Nit | 4 | `P-14`–`P-17` |

**Recommendation**: Approve once `P-01`–`P-03` are resolved or accepted by the owner. The Minors can be fixed in the design or tracked into implementation. None of them requires changing a B-01…B-07 decision.

**Trade-off confirmations (B-01…B-07)**: I agree with all seven. Notes:
- **B-03**: also see `P-12` on cross-entity ordering after coalescing.
- **B-04**: the tie-break argument holds for single-field mutations. It does not cover bulk mark-read (`P-01`) or online edits (`P-02`).
- **B-07**: see `P-15` on the delimiter.

---

## 1. Major

### P-01 — `item_mark_bulk_read` does not commute with `item_set_state`, so arrival order changes the final state
**Location**: §6.5 item 4 ("Always `items.state = 'unread'`"), §6.5 item 2 ("Proof of Commutativity"), §10.2 `M-02`.

The bulk mutation applies only to rows that are `unread` *at the moment it is applied*. Whether a row matches therefore depends on which other mutations arrived first. This is not per-field LWW.

**Failure scenario**: Item X is `read` (`read_state_updated_at = t0`). Device B goes offline and marks X `unread` at `t2`. Device A, at `t3 > t2` and still seeing X as read, does "Mark feed read" with `up_to_sync_seq` covering X.
- **Order A (A's push arrives first)**: the bulk mutation skips X because it is not unread. Then B's `unread@t2` beats `t0`, so X ends `unread`.
- **Order B (B's push arrives first)**: X becomes `unread@t2`. Then the bulk mutation matches X and `t3 ≥ t2`, so X ends `read`.

The same divergence happens with `expired` items: the bulk mutation skips `expired`, but an `expired → unread` restore that arrives first makes the item eligible. In both cases the final server state depends on network timing, which contradicts the "strict arrival-order-independent convergence" in B-04 and the `development-process.md` §2.4 invariant ("LWW convergence regardless of mutation arrival order").

`M-02` cannot catch this. It tests only the pure functions in `sync_merge.py` with `item_set_state`, `item_set_starred` and `feed_set_field`, and never runs the service-level path (in-batch processing plus SQL `UPDATE`).

**Suggested fix**: choose one of these and record it:
- **(a) Make bulk read a true per-item LWW write.** For every item in scope with `ingest_sync_seq ≤ bound` and `state IN ('unread','read')`, apply `read@effective_ts` under the normal comparator:
  - `unread` rows flip to `read`;
  - `read` rows with an older timestamp only advance `read_state_updated_at`;
  - `expired` stays excluded, and that exclusion is documented as a deliberate non-commuting carve-out.

  Advancing the timestamp on already-read rows emits extra `item` upserts. To limit the cost, restrict it to rows whose `read_state_updated_at < effective_ts`.
- **(b) Keep the current semantics** and have the owner accept the divergence as a residual risk. Then reword B-04 and §6.5 so they no longer claim that bulk read commutes.

**Tests**:
- Extend `M-02` with a real-Postgres `hypothesis` test (small `max_examples`) that permutes mixed batches, including `item_mark_bulk_read`, through `sync/service.py`. Under (a) it asserts identical final rows; under (b) it asserts the documented exception.
- Add the explicit two-order regression above to `M-06`.

### P-02 — Online `PATCH /api/v1/feeds/{id}` (and category delete) overwrite LWW fields with the Python clock, so `field_updated_at` can move backwards and convergence breaks
**Location**: §6.6 last bullet. Existing code: `api/routes/feeds.py:451-550` and `api/routes/categories.py:129-154`.

Both online paths write the value unconditionally and set `field_ts[field] = datetime.now(UTC)`, with these consequences:
- That timestamp can be *earlier* than a stored offline timestamp, because offline timestamps are clamped up to `server_now + 5 min`.
- It comes from the API container's clock, not from PostgreSQL. This violates checklist **C5** for every value compared against `sync/push`'s `SELECT now()`.

§6.6 says the online edits "properly beat older offline toggles", but it never defines the merge rule.

**Failure scenario**: The laptop clock is 4 minutes fast.
1. The laptop pushes `status=muted`, stored with timestamp `12:04`; the server time is `12:00`.
2. At `12:01` the user un-mutes the feed online from the phone. `PATCH` writes `status=active` and moves the timestamp *back* to `12:01`.
3. A tablet's delayed offline `status=paused@12:02` arrives and wins, because `12:02 > 12:01`.

If the tablet's mutation arrives before the `PATCH`, it is superseded by `muted@12:04`, and the `PATCH` then leaves the feed `active`. So the final state depends on arrival order, and the user's most recent explicit online choice can be undone by an older offline toggle.

**Suggested fix**:
- **Online edits.** Specify that online edits of LWW fields (`status`, `category_id`, `expiration_days_override`, `research_policy.*`, `title`):
  - use the DB clock;
  - write `field_updated_at[f] = GREATEST(db_now, stored_ts_f)` (optionally plus 1 µs so the online edit strictly wins);
  - always apply the value.

  This keeps every field's timestamp monotonic, and online edits stay authoritative at the moment they happen.
- **Category delete.** Apply the same rule to the `category_id` stamp in category delete.
- **Shared helper.** Put the helper in `domain/sync_merge.py` so online and push paths share it.

**Tests**: an `M-07` case that replays the scenario above in both arrival orders and asserts the same final `status` and a timestamp that never decreases.

### P-03 — The push response's `watermark` lets a client skip other devices' changes
**Location**: §6.4 response schema (`"watermark": 1260`).

The response returns `current_seq` after the push commits. It includes changes from *every* writer up to that point (poll ingests and other devices' pushes), not just this push's own changes. If a client sets its pull cursor to this value — a natural shortcut, since it looks like the bootstrap/pull `watermark` — then it never pulls the changes between its old cursor and the new value. This permanently violates `NFR-SYNC-01`. The design calls the bootstrap protocol "Normative for `1c` and Phase 2" but says nothing about this field.

**Suggested fix**: Either remove `watermark` from the push response, or rename it (for example `server_seq_after_push`) and add a normative rule: *clients MUST NOT use it as a pull cursor; the pull cursor advances only from `sync/pull.next_seq` or bootstrap page 1.* Also state that clients should pull right after a push, and that the push's own changes coming back in that pull is expected and idempotent.

**Tests**: an `M-03`/`M-04` integration test:
1. Device B pushes.
2. Device A pushes, and its response watermark covers B's change.
3. Device A pulls from its old cursor and receives B's change.

Add a `1c` contract note so the client test suite asserts the cursor is only taken from pull/bootstrap.

---

## 2. Minor

### P-04 — §5.1 lock hierarchy is "MUST" but existing `1a` writers violate it, and `app_settings`/`jobs` are missing from it
**Location**: §5.1, §5.4. Code: `poll_feed.py:757-797` and `maintenance.py`.

There are three gaps:
1. **`poll_feed` updates items out of order.** It updates matched existing items one at a time in *feed-entry order*, not `id ASC`. `sync/push` locks items in `id ASC` without taking a feed lock first (for `item_set_state` and `item_set_starred`). A poll that updates items `B, A` while a push locks `A, B` can deadlock. PostgreSQL resolves it by aborting one of them; if it aborts the push, all 500 mutations fail (see `P-05`).
2. **`app_settings` is locked after `sync_state` in retention cleanup.** The new §5.4 updates `sync_state` (step 2) *before* `app_settings` (step 4). `PATCH /api/v1/settings` (§7) locks `app_settings` and then `sync_state`. These two orders can deadlock. The hierarchy never mentions `app_settings`.
3. **`jobs` is not placed.** `jobs` rows are locked when an `unsubscribed → active` status change enqueues a job inside push. The hierarchy doesn't say where they go.

**Fix**:
- Add `app_settings` and `jobs` to §5.1, with `app_settings` before `sync_state`.
- Reorder §5.4 so `app_settings` is updated before `sync_state`.
- Add to the `1b` scope: sort `matched_existing` by `item.id` in `poll_feed` before the per-item updates.

**Test**: a two-connection integration test in which `poll_feed` updates two existing items while a push locks the same two items, asserting no deadlock. At minimum, add a unit test that asserts the update order is sorted.

### P-05 — No retry policy for transient failures across the whole push batch
**Location**: §6.7.

A single transaction holding 500 mutations is the right choice. But a deadlock (`40P01`), a serialization failure, or a duplicate-key error on `sync_mutations` aborts all 500 mutations.

The duplicate-key case is realistic. A client times out on a large push and retries while the first request is still running. Both requests bulk-load `sync_mutations`, find nothing, and apply. The second request then blocks on the first one's uncommitted primary key and raises `IntegrityError`, which returns HTTP 500. The client retry recovers, but the design doesn't say whether the server or the client is responsible for retrying.

**Fix**: specify a server-side retry (for example up to 3 attempts with jitter) on SQLSTATE `40P01`, `40001` and `23505` for `sync_mutations`. Alternatively, define a `503` with `Retry-After` that the `1c` outbox honors.

**Test**: two concurrent pushes of the same 50-mutation batch from the same device. Both must return 200 with identical outcomes, and the side effects must be applied once.

### P-06 — `SyncFeedDTO` includes fields whose changes never emit a sync event
**Location**: §6.1 `SyncFeedDTO`; `db/sync_log.py:35-52` (`FEED_SYNCED_COLUMNS`).

`poll_interval_minutes`, `last_fetched_at` and `last_success_at` are serialized to clients, but they are not in `FEED_SYNCED_COLUMNS`.
- When `poll_interval_minutes` is changed through `PATCH`, no `feed` upsert is emitted, so other devices never see the change until some unrelated synced field changes.
- `last_*_at` are refreshed only incidentally, so they show arbitrarily stale times.

**Fix**: either remove these fields from the DTO (serve them online-only through `GET /feeds` or System Health), or add `poll_interval_minutes` to `FEED_SYNCED_COLUMNS`. Don't add `last_fetched_at`: an upsert on every poll would flood the log. Then document which DTO fields are "snapshot-only, may be stale".

**Test**: `PATCH poll_interval_minutes` followed by `pull` either emits a feed change or the field is absent from the DTO.

### P-07 — The normative SQL uses `:param::type` casts, which SQLAlchemy does not bind *(verified)*
**Location**: §5.3 (`unnest(:ids::uuid[])`), §8.1 (`:fts_cfg::regconfig`), §8.2.

SQLAlchemy's `text()` treats `:name::type` as a literal rather than a bind parameter. Verified on SQLAlchemy 2.1.9: `text('SELECT to_tsvector(:fts_cfg::regconfig, :x), unnest(:ids::uuid[])')` recognizes only `x` as a bind and leaves `:fts_cfg::regconfig` and `:ids::uuid[]` verbatim in the compiled SQL.

**Fix**: write `CAST(:fts_cfg AS regconfig)` and `CAST(:ids AS uuid[])` in the design, or use `sa.bindparam(..., type_=...)`/`sa.cast`. Implementation tests would catch this right away, but the design should not prescribe broken SQL.

### P-08 — Hydration loads full bodies, and `current_entity` can make push responses very large
**Location**: §6.1 (`lead_excerpt` and `raw_summary` derivation), §6.3 step 7, §6.4 `current_entity` policy, §8.2 (results embed `SyncItemDTO`).

- **Full bodies are loaded.** Building `lead_excerpt` loads all of `extracted_text`, which averages 20–30 KB per item (`data-model.md` §6.3). For a 500-row hydration that is about 15 MB pulled into the API process for each pull page, just to keep 1,500 characters.
- **Push responses can be very large.** A 500-mutation push where most mutations are `superseded` (common when replaying a stale outbox) returns up to 500 `SyncItemDTO`s, each with a `raw_summary` of up to 32 KB, so up to about 16 MB.

**Fix**:
- In SQL, select `LEFT(extracted_text, 8192)` and `LEFT(raw_summary, 32769)`. The extra character detects truncation, and `8192` leaves room for whitespace normalization.
- Omit `raw_summary` from `current_entity` on push, or return only the conflicting fields (`state`, `is_starred`, the timestamps).
- Search results can omit `raw_summary` as well.

**Test**: the `M-08` benchmark asserts the response size of an all-superseded 500-mutation batch.

### P-09 — Unbounded total work per push, and `scope='all'` is not benchmarked
**Location**: §6.4 batch bounds, §6.5 item 4, §9 `S6`.

Per-mutation caps (`item_ids ≤ 1000`) multiply across a batch: 500 × 1,000 = 500,000 IDs in one request and one transaction, with no request-body size limit stated. A `scope='all'` bulk read over a large unread backlog (for example 20,000 items after an OPML import) locks every candidate row and writes 20,000 `sync_changes` rows in one transaction. `M-08` benchmarks neither case.

**Fix**:
- Cap the total number of explicit `item_ids` per push (for example 5,000).
- State the API's maximum request-body size.
- Add an `M-08` variant: `scope='all'` over 20,000 unread items in under 5 s.

### P-10 — Requirements traceability: `FR-SEARCH-01` does not exist, and `FR-STATE-03`/`FR-STATE-04` are not mapped
**Location**: header "Requirements Covered".

`requirements.md` has no `FR-SEARCH-01`. Full-text archive search is `FR-STATE-04`, and bounded bulk read is `FR-STATE-03`, which `1b` implements but doesn't claim. `FR-STATE-04` also requires searching TL;DRs, takeaways and tags, which `1b` can only partly satisfy (they arrive in `1d`).

**Fix**:
- Replace `FR-SEARCH-01` with `FR-STATE-04 (partial: title/author/summary/body; enrichment fields in 1d)`.
- Add `FR-STATE-03 (bounded bulk read; cluster read in 1e)`.

### P-11 — The restore drill protects only *pending* mutations; acknowledged mutations since the last backup are silently lost
**Location**: §6.2 step 5, §10.2 `M-09`; ADR 0004 §3.

Backups are taken nightly (`NFR-OPS-01`). After a restore, every mutation the client pushed and saw `applied` since the backup is gone from the server: the client has already removed it from its outbox, and re-bootstrap overwrites the local state. Read and star state from up to one day is lost, even though the replay would be safe: it is idempotent LWW, and the restored `sync_mutations` table doesn't contain those mutations.

**Fix (owner decision; see Q-1)**: either document this as accepted residual risk, or make it normative for `1c` that clients keep acknowledged mutations for a window (for example 48 h, or the backup interval plus margin) and replay them after an `epoch_mismatch`. `M-09` would then cover the replay.

### P-12 — Coalescing and pagination break cross-entity ordering; the client application rule is not stated
**Location**: §6.3 step 6 (coalescing), §6.2 pages 2..N.

When coalescing keeps the latest `seq` of an entity, the following can happen:
- A feed's earlier upsert moves *after* items that reference it. For example, feed F at seq 1, item I in F at seq 2, and F again at seq 3 produce `[I(2), F(3)]`.
- Bootstrap pages 2..N can return items whose `feed_id` was created after page 1. Page 1's feed list doesn't contain those feeds; the trailing pull delivers them later.

A client that requires a referenced feed to exist before applying an item will fail or drop rows.

**Fix**: add to the normative client protocol: *apply each pull page and the whole bootstrap sequence as one local transaction; tolerate dangling `feed_id`/`category_id` references until the trailing pull completes.*

**Test**: an `M-04` case that asserts this ordering is possible, so `1c` builds against it.

### P-13 — A negation-only query (`-foo`) produces a non-empty `tsquery` that matches nearly the whole archive *(verified)*
**Location**: §8.2 `q` row.

`websearch_to_tsquery('english','-foo')` returns `!'foo'` with `numnode > 0` (verified on pg16). It therefore passes the empty-query check, cannot use the GIN index, and with `sort=relevance` it runs `ts_rank_cd` and `COUNT(*)` over every item.

**Fix**: when the parsed query has no positive lexeme, either return `422` with "query needs at least one search term", or treat it as a filter-only query with `sort=newest` and no rank. Add this case to `M-10`.

---

## 3. Nit

### P-14 — Migration `0002` backfill details
**Location**: §4.3.

- **HTML stripping differs from the Python builder.** The SQL backfill strips tags with `regexp_replace('<[^>]+>')`. The Python builder uses `HTMLParser`, which skips `<script>`/`<style>` content and decodes entities. Backfilled rows therefore get different lexemes from rebuilt rows.
- **`:cfg` has no defined source.** The design doesn't say where `:cfg` comes from inside Alembic.
- **`MIN(seq)` can pick a later update.** Taking `MIN(sync_changes.seq)` per item can select a later *update* event when the original insert event has already been pruned.

**Suggestion**: have the migration only add columns and indexes, and run `smartrss search reindex` as a documented post-migration step (or call the Python builder from the migration). Note the `MIN(seq)` approximation, which is harmless before deployment.

### P-15 — `FragmentDelimiter` must be double-quoted, and fragment boundaries are lost *(verified)*
**Location**: §8.2.

With `FragmentDelimiter= … ` unquoted, the surrounding spaces are dropped. Verified on pg16: the output is `…delta…theta…` with the fragments joined directly. Also, the flat `snippet` list can't distinguish a fragment boundary from an ellipsis that appears in the article text.

**Suggestion**: use `FragmentDelimiter=" … "`, or a third control-character sentinel (for example `chr(4)`, stripped from the input like STX/ETX), and return `snippet` as `list[list[Fragment]]`.

### P-16 — Design-internal IDs collide with review prefixes
**Location**: §2 (`B-01`…`B-07`) and §10.2 (`M-01`…`M-12`).

`B-` and `M-` are already finding prefixes in the 2026-11-22 high-level review, and `development-process.md` §7.2 keeps IDs unique across the repo. A cross-reference like "see B-03" is now ambiguous.

**Suggestion**: rename them, for example `1b-D01` and `1b-T01`.

### P-17 — Small contract gaps
- **Search auth**: §8.2 doesn't say `GET /api/v1/search` requires the bearer token. §6 says it only for `/sync/*`.
- **Search results and the client cache**: say whether `1c` may merge search results (which can be `expired` or old `read` items) into Dexie, or only display them.
- **Mutation payload changes**: `sync_mutations` stores no payload hash. A client bug that reuses a `client_mutation_id` with a different payload silently gets the cached outcome. Consider storing `payload_sha256` and rejecting a mismatch with `mutation_id_conflict`.
- **`PATCH /api/v1/settings`**: state that an explicit `null` for a non-nullable field returns `422`.

---

## 4. Questions for the owner

- **Q-1 (`P-11`)**: Should clients keep acknowledged mutations for a replay window so that a restore loses nothing, or do we accept losing up to one backup interval of read and star state?
- **Q-2 (`P-01`)**: For bulk mark-read, choose option (a), true per-item LWW (more `item` upserts on already-read rows), or option (b), keeping the current semantics as an accepted non-commuting exception.
- **Q-3 (clock lower bound)**: The design clamps only future timestamps. A device whose clock is far in the *past* (dead RTC, or 1970 after a battery pull) has every offline mutation `superseded`, without an error. Should push reject (`clock_skew`) or flag timestamps that are more than, say, 24 h behind `server_now`, so that `1c` can warn the user?
- **Q-4 (status side effects online vs. offline)**: `feed_set_field status: unsubscribed → active` resets `etag`, health and backfill, and enqueues a poll (§6.6). The existing online `PATCH status` (`feeds.py:526-528`) does none of this. Should both go through one shared transition helper? I recommend yes; otherwise the same user action behaves differently depending on connectivity.

---

## 5. Verification performed

| Claim / behavior | How checked | Result |
| :--- | :--- | :--- |
| SQLAlchemy binds `:x::type` | Compiled `sa.text(...)` with the project venv (SQLAlchemy 2.1.9) | **Not bound** → `P-07` |
| `ts_headline` with STX/ETX sentinels | Ran on `smartrss-db-test-1` (pgvector pg16) | Works; unquoted `FragmentDelimiter` drops spaces → `P-15` |
| `websearch_to_tsquery` on stop-words or punctuation only | pg16 | `numnode = 0` plus a NOTICE, as the design assumes |
| `websearch_to_tsquery` on a negation-only query | pg16 | `!'foo'`, `numnode > 0` → `P-13` |
| `1a` online `PATCH` timestamps | Read `api/routes/feeds.py:451-550`, `categories.py:129-154` | Python `datetime.now`, unconditional overwrite → `P-02` |
| `1a` `poll_feed` lock order | Read `poll_feed.py:757-797` | Per-item updates in entry order → `P-04` |
| `1a` `FEED_SYNCED_COLUMNS` vs `SyncFeedDTO` | Read `db/sync_log.py:35-52` | `poll_interval_minutes` and `last_*_at` missing → `P-06` |
| `poll_feed` event list ordering for `ingest_sync_seq` mapping | Read `poll_feed.py:935-944` | Feed event is prepended and update events are interleaved. The implementation must map with `zip(all_events, seqs)` filtered to inserted IDs, not by position in `inserted_ids`. Noted for the implementation review. |
| Performance claims (push in ~50–150 ms, bootstrap partial indexes) | Not measured | Inferred plausible; `M-08` must prove them (`P-09`) |

`make check` was not run: this review covers documentation only, with no code change.

---

## 6. Owner Decisions (2026-11-25)

| Question | Decision | Where Recorded |
| :--- | :--- | :--- |
| **Q-1 (`P-11`)** | **48-hour client acknowledged replay journal** (normative for `1c` and Phase 2): retain recently acknowledged mutations locally for 48 hours and replay both pending outbox and replay journal idempotently after `epoch_mismatch` re-bootstrap so zero post-backup read/star/feed state is lost. | `1b` §6.2 step 5, §10.2 `1b-T09`, §11 |
| **Q-2 (`P-01`)** | **Two-tier bulk mark-read**: true per-item LWW over `state IN ('unread', 'read')` for explicit `scope='items'` (`<= 1,000` IDs, e.g., `mark_cluster_read`), and **Option (b)** (`state = 'unread'` only, documented non-commuting carve-out) for scoped bulk modes (`all`/`feed`/`category`) to avoid locking and emitting tens of thousands of historical archived `read` items. | `1b` §2 `1b-D04`, §6.5 items 2 & 4, §10.2 `1b-T02` & `1b-T06`, §11 |
| **Q-3 (Clock lower bound)** | Return **`server_now`** in `bootstrap`, `pull`, and `push` responses so `1c` warns on `|client_now - server_now| > 5 min` without penalizing legitimate multi-day offline sessions, **and** reject clearly impossible/dead-RTC timestamps (`client_timestamp < server_now - 90 days`) with `outcome="rejected", reason="clock_skew"`. | `1b` §6.2–§6.5, §6.7, §10.2 `1b-T02` & `1b-T05`, §11 |
| **Q-4 (Status side effects)** | **Unified shared helper**: route both online `PATCH /api/v1/feeds/{id}` and offline `feed_set_field` through `apply_feed_status_transition()` so `unsubscribed → active` and `paused → active|muted` trigger identical resets and `poll_feed` enqueueing regardless of connectivity. | `1b` §1, §3, §6.6, §10.2 `1b-T07` |

---

## 7. Resolutions (2026-11-25)

All 17 findings (`P-01`–`P-17`) and 4 owner decisions (`Q-1`–`Q-4`) have been addressed in [`docs/design/1b-sync-protocol-and-search.md`](../design/1b-sync-protocol-and-search.md), [`docs/review-checklist.md`](../review-checklist.md), and [`docs/README.md`](../README.md).

### Major Findings (`P-01`–`P-03`)
- **P-01** — Fixed per owner decision `Q-2`. Split `item_mark_bulk_read` into two explicit tiers: `scope='items'` applies true per-item LWW over `state IN ('unread', 'read')` (strictly commutative with `item_set_state`; `expired` remains excluded as a documented carve-out), while scoped modes (`all`/`feed`/`category`) keep `state = 'unread'` only to prevent archive-wide write/sync amplification and are honestly documented as an owner-accepted non-commuting carve-out (`1b` §2 `1b-D04`, §6.5 items 2 & 4). Extended `1b-T02` with a real-Postgres `hypothesis` mixed-batch permutation test through `sync/service.py` and added the two-order regression scenario to `1b-T06`.
- **P-02** — Fixed. Added shared `compute_online_lww_timestamp(db_now, stored_ts)` in `domain/sync_merge.py` returning `stored_ts + 1µs` when `stored_ts >= db_now` else `db_now` (`1b` §6.6). Online `PATCH /api/v1/feeds/{id}`, `POST /api/v1/feeds` reactivation, and `DELETE /api/v1/categories/{id}` now read `SELECT now()` from PostgreSQL (`C5`) and stamp `field_updated_at` monotonically (SQL `GREATEST(...)` on category delete). Added the fast-laptop two-order replay regression to `1b-T07` and recorded recurring class **`C7`** in [`docs/review-checklist.md`](../review-checklist.md).
- **P-03** — Fixed. Removed `watermark` from `POST /api/v1/sync/push` responses (`SyncPushResponse` returns `{instance_epoch, server_now, results}` only) and added the normative `1c`/Phase 2 rule that pull cursors advance exclusively from `sync/pull.next_seq` or `bootstrap` Page 1 `watermark`, with immediate post-push `sync/pull` (`1b` §6.4). Added the two-device push→pull integration test to `1b-T04`.

### Minor Findings (`P-04`–`P-13`)
- **P-04** — Fixed. Expanded §5.1 global lock hierarchy to `categories → feeds → items → jobs → app_settings → sync_state`; reordered §5.4 (`retention_cleanup`) to update `app_settings` before `sync_state`; added sorting `matched_existing` by `item.id ASC` in `poll_feed.py` (§1, §3, §5.3); added the two-connection `poll_feed` + `sync/push` lock-order test to `1b-T01`.
- **P-05** — Fixed. Added a server-side transient-error retry policy to §6.7: up to 3 attempts with 20–100 ms jittered backoff (rolling back between attempts) on SQLSTATE `40P01`, `40001`, and `23505` (`sync_mutations_pkey`), returning `503 Service Unavailable` with `Retry-After: 1` if retries are exhausted. Added the concurrent duplicate 50-mutation batch test to `1b-T05`.
- **P-06** — Fixed. Added `poll_interval_minutes` to `FEED_SYNCED_COLUMNS` (`1b` §5.2) and removed `last_fetched_at` / `last_success_at` from `SyncFeedDTO` (§6.1, served online-only via `GET /api/v1/feeds` and System Health) so every synced feed field is backed by a sync event. Added the test assertion to `1b-T07`.
- **P-07** — Fixed. Replaced all `:param::type` casts in §5.3, §6.6, §8.1, and §8.2 with `CAST(:param AS type)` (`CAST(:ids AS uuid[])`, `CAST(:seqs AS bigint[])`, `CAST(:fts_cfg AS regconfig)`). Verified on SQLAlchemy 2.1.9: `sa.text(...)` compiles and binds all four parameters `['fts_cfg', 'ids', 'seqs', 'x']`. Recorded recurring class **`D5`** in [`docs/review-checklist.md`](../review-checklist.md).
- **P-08** — Fixed. Projected `LEFT(items.raw_summary, 32769)` and `LEFT(items.extracted_text, 8192)` during `bootstrap` and `pull` hydration (§6.1–§6.3); introduced slim `SyncPushItemSnapshot` / `SyncPushFeedSnapshot` for push `current_entity` (§6.4); omitted `raw_summary`/`summary_truncated` from `SearchItemDTO` (§8.2); added `< 512 KB` response-size assertion for 500 all-superseded mutations to `1b-T08`.
- **P-09** — Fixed. Added a per-request cap of `5,000` total explicit `item_ids` (`422`) and a `2 MiB` HTTP request body limit (`413`) on `POST /api/v1/sync/push` (§6.4, §9 `S6`, `1b-T05`); added the `scope='all'` 20,000-unread-item `< 5.0s` benchmark to `1b-T08`.
- **P-10** — Fixed. Updated the design header's "Requirements Covered" to replace non-existent `FR-SEARCH-01` with `FR-STATE-03 (bounded bulk read; cluster read in 1e)` and `FR-STATE-04 (partial: title/author/summary/body; enrichment TL;DR/takeaways/tags in 1d)`.
- **P-11** — Fixed per owner decision `Q-1`. Made the 48-hour client acknowledged replay journal normative for `1c`/Phase 2 on `epoch_mismatch` (`1b` §6.2 step 5) and extended `1b-T09` to verify recovery of both pending outbox and post-backup acknowledged mutations.
- **P-12** — Fixed. Added the normative `1c`/Phase 2 client application rule to §6.2 and §6.3: apply bootstrap and each pull page atomically in a single Dexie transaction and tolerate temporarily dangling `feed_id`/`category_id` references until the trailing pull completes. Added the `[feed(1), item(2), feed(3)] → [item(2), feed(3)]` coalescing test case to `1b-T04`.
- **P-13** — Fixed. Added PostgreSQL `querytree(tsq) = 'T'` check returning `422 Unprocessable Entity` (`"Search query must include at least one positive required search term"`) alongside `numnode(tsq) = 0` empty-result handling (§8.2). Verified on `smartrss-db-test-1` (PG16): `querytree` returns `'T'` for `-foo`, `-foo -bar`, `-"hello world"`, and `foo OR -bar`, and returns `'foo'` for `foo -bar` and `foo`. Added all cases to `1b-T10`.

### Nit Findings (`P-14`–`P-17`)
- **P-14** — Fixed. Updated §4.3 so migration `0002` resolves `FTS_LANGUAGE` via `os.environ.get("FTS_LANGUAGE", "english")` validated against the allow-list and backfills `items.search_vector` in 500-row batches using the shared Python `HTMLParser` helper (matching `poll_feed`/`extract_content`/`smartrss search reindex`), while documenting the harmless pre-deployment `MIN(seq)` approximation for `ingest_sync_seq`. Updated `1b-T12`.
- **P-15** — Fixed. Switched `ts_headline` to strip `\x02`, `\x03`, and `\x04` and use `'FragmentDelimiter="' || chr(4) || '"'`, returning `snippet` as `list[list[SnippetFragment]]` (§8.2, `1b-T10`). Verified on `smartrss-db-test-1` (PG16): fragments split cleanly on `\x04` with spaces preserved and zero collision with article ellipses.
- **P-16** — Fixed. Renamed `B-01`–`B-07` → `1b-D01`–`1b-D07` and `M-01`–`M-12` → `1b-T01`–`1b-T12` throughout `docs/design/1b-sync-protocol-and-search.md`.
- **P-17** — Fixed all four items: stated bearer auth + `Host`/CORS middleware on `GET /api/v1/search` (§8.2); added normative display-only Dexie cache rule for search results (§8.2); added `payload_sha256 TEXT NOT NULL` to `sync_mutations` rejecting mismatched reuse with `mutation_id_conflict` (§4.1, §6.4, §6.7, `1b-T05`, §11); stated explicit `null` for non-nullable fields on `PATCH /api/v1/settings` returns `422` (§7, `1b-T11`).
- **Verification note (poll_feed `ingest_sync_seq` mapping)**: Incorporated the reviewer's §5 note directly into `1b` §5.3 so `poll_feed` explicitly zips `all_events` with `seqs` filtered to `inserted_ids` rather than indexing by position.

*No application code was modified in this design-review pass (per `AGENTS.md`, `1b` implementation starts only after `1b` design approval).*

---

## 8. Verification pass (2026-11-25)

- **Verifier**: Claude Code (Claude Opus 5.5), fresh pass over commit `53f0dfe`.
- **Method**: Re-read the revised design end-to-end against every finding, independently re-ran the implementer's "verified" claims, and checked for doc drift. This is a design-only pass with no application code, so there are no regression tests to revert.
- **`make check`**: green. 131 passed; total coverage 88.42%; every critical module meets the ≥ 90% gate. No code has changed since `1a`, so this is the expected baseline.

### Independently re-checked claims
| Claim | Re-check | Result |
| :--- | :--- | :--- |
| `CAST(:p AS type)` binds in `sa.text()` (`P-07`) | Compiled the §5.3/§8.1 SQL shape with SQLAlchemy 2.1.9 | ✅ binds `fts_cfg`, `ids`, `seqs`, `x` |
| No `:param::type` left in design | `grep -nE ':[a-z_]+::'` | ✅ none |
| `querytree(tsq) = 'T'` for negation-only queries (`P-13`) | pg16: `-foo`, `-foo -bar`, `-"hello world"`, `foo OR -bar` → `T`; `foo -bar`, `foo` → `'foo'`; `the and`, `!!!` → `numnode = 0` | ✅ matches §8.2 |
| Quoted `chr(4)` `FragmentDelimiter` (`P-15`) | pg16 with an article containing a literal `…` | ✅ fragments split only on `\x04`; spaces kept; article `…` untouched |
| Old `B-0x`/`M-0x` IDs removed (`P-16`) | `grep` | ✅ none remain |
| Checklist classes `C7`, `D5` added | `docs/review-checklist.md:30,51` | ✅ |

### Verdicts
| Finding | Sev | Verdict | Evidence |
| :--- | :--- | :--- | :--- |
| `P-01` | Major | ✅ | §6.5 items 2 and 4 use two tiers per `Q-2`. `scope='items'` uses LWW over `unread`/`read`, consistent with the single-item comparator, including the equal-timestamp rank. The scoped carve-out is documented, and §2 `1b-D04` no longer over-claims. `1b-T02(b)` adds service-level real-PG permutations and `1b-T06` the two-order regression. |
| `P-02` | Major | ✅ | §6.6 `compute_online_lww_timestamp()` reads the DB clock and is strictly monotonic. It covers `PATCH`, `POST` reactivation, and category delete (SQL `GREATEST`). `1b-T07(b)` replays the fast-laptop scenario in both arrival orders. |
| `P-03` | Major | ✅ | §6.4 removes `watermark` from the push response and adds a normative cursor rule. `1b-T04(c)` covers it. |
| `P-04` | Minor | ✅ | §5.1 hierarchy is extended (`jobs`, `app_settings`). §5.4 is reordered so `app_settings` comes first and `sync_state` last. `poll_feed` sorts `matched_existing`, covered by `1b-T01(b)`. *Observation, no follow-up*: `poll_feed` still interleaves `extract_content` `enqueue_job` between item updates, but push only enqueues `poll_feed:feed:*` rows, so the two never contend on the same `jobs` rows and no cycle is possible. |
| `P-05` | Minor | ✅ | §6.7 retries 3× on `40P01`/`40001`/`23505`(`sync_mutations_pkey`), then returns `503` + `Retry-After`. `1b-T05(b)` covers it. |
| `P-06` | Minor | ✅ | `poll_interval_minutes` is added to `FEED_SYNCED_COLUMNS`. `last_*_at` are removed from `SyncFeedDTO` and §11 lists the `data-model.md` change. `1b-T07(d)` covers it. |
| `P-07` | Minor | ✅ | See re-check table. |
| `P-08` | Minor | ✅ | `LEFT(...)` projections in bootstrap and pull, slim push snapshots, and `SearchItemDTO` without `raw_summary`. `1b-T08(b)` asserts responses stay `< 512 KB`. |
| `P-09` | Minor | ✅ | Caps of 5,000 total `item_ids` (`422`) and 2 MiB body (`413`). The 20k `scope='all'` benchmark is `1b-T08(c)`. |
| `P-10` | Minor | ✅ | Header now maps `FR-STATE-03`/`FR-STATE-04` (partial). |
| `P-11` | Minor | ⚠️ | The journal is normative (§6.2 step 5) and `1b-T09` is extended. Replaying scoped bulk reads across an epoch change is unsafe, though; see `P-11-F1`. |
| `P-12` | Minor | ✅ | Normative client rule in §6.2 step 3 and §6.3. `1b-T04(b)` covers it. |
| `P-13` | Minor | ✅ | See re-check table. Covered by `1b-T10`. |
| `P-14` | Nit | ✅ | §4.3 uses the Python builder plus `FTS_LANGUAGE` from env, and documents the `MIN(seq)` note. `1b-T12` covers it. |
| `P-15` | Nit | ✅ | See re-check table. `list[list[SnippetFragment]]`. |
| `P-16` | Nit | ✅ | Renamed to `1b-D0x`/`1b-T0x`. |
| `P-17` | Nit | ✅ | Search auth (§8.2), display-only cache rule, `payload_sha256` (§4.1, §6.7, `1b-T05(a)`), and `null` → `422` on settings (§7). |

**Counts**: 16 ✅, 1 ⚠️, 0 ❌.

### Follow-ups
#### `P-11-F1` (Minor) — Scoped bulk-read bounds from the old epoch mark unseen post-restore items read on replay
**Location**: §6.2 step 5, §6.5 item 4, `1b-T09`.

`up_to_sync_seq` lives in the *old* epoch's sequence space. A restore rewinds `current_seq`, so items ingested after the restore receive seqs that a pre-restore bound already covers. The per-item guard `effective_ts >= read_state_updated_at` protects items fetched after the user's action, but not items fetched between the restore and the action.

**Scenario**:
1. Client goes offline at cursor `W = 1000`.
2. At 10:00 the server is restored to `current_seq = 900` and the epoch rotates.
3. At 10:05 a poll ingests items with seqs 901–950 (`fetched_at` 10:05).
4. At 11:00 the user, still offline, taps "Mark all read" (`up_to_sync_seq = 1000`).
5. On reconnect, `pull` returns `epoch_mismatch` → re-bootstrap → outbox flush. Items 901–950 are marked `read` although the client never saw them.

This violates `FR-STATE-03` / `data-model.md` §5.2 ("never marks unseen items"). The normative 48h replay journal widens exposure, because it re-pushes acknowledged scoped bulk reads after every restore. The same holds for the pending outbox (ADR 0004), so the issue predates `1b`, but `1b` is now the normative contract.

**Expected fix**:
- Scoped `item_mark_bulk_read` (`all`/`feed`/`category`) carries the client's `epoch`. If it differs from the current `instance_epoch`, the server returns `rejected`, `reason = "epoch_mismatch"`, and does nothing.
- The `1c` client either drops these mutations on replay or rewrites them as `scope='items'` over the item IDs it knew pre-restore.
- `scope='items'` and single-field mutations stay epoch-agnostic.
- Add the scenario above to `1b-T09`.
- Note the rule in the §11 ADR 0004 alignment line.

### Residual risks for owner acceptance
- `P-01` / `Q-2`: the scoped bulk mark-read non-commuting carve-out (already owner-decided; restated here for the approval record).
- `expired` exclusion in `scope='items'`: a deliberate carve-out (§6.5 item 2).

### Status
`Addressed — verification found follow-ups` (`P-11-F1`). Hand back to Antigravity (`address-review-findings`). Once `P-11-F1` is addressed, the reviewer re-checks only that item.

---

## 9. Follow-up Resolutions (2026-11-25)

- **P-11-F1** — Fixed. Added required `epoch: UUID` to scoped `item_mark_bulk_read` (`all`/`feed`/`category`) and left `scope='items'` and single-field mutations epoch-agnostic (`1b` §2 `1b-D05`, §6.4 table, §6.5 item 4, §6.7 steps 1/3/4). On push, the server checks `mutation.epoch == sync_state.instance_epoch` (read without `FOR UPDATE` in step 1, preserving §5.1 lock ordering) and immediately returns `outcome = "rejected", reason = "epoch_mismatch", affected_count = None` with zero item updates on mismatch. Updated the normative `1c`/Phase 2 restore protocol (§6.2 step 5) so clients drop or rewrite old-epoch scoped bulk reads as chunked `scope='items'` over pre-restore known IDs before overwriting local state. Added the exact `W = 1000 → restore 900 → post-restore poll 901..950 → offline bulk read` regression to `1b-T09` and recorded the rule in §11 (`ADR 0004` / `data-model.md` §5.2 alignment).

---

## 10. Final verification pass — `P-11-F1` (2026-11-25)

- **Verifier**: Claude Code (Claude Opus 5.5), commit `bc5bd08`.
- **`make check`**: green. 131 passed; total coverage 88.42%; every critical module meets the ≥ 90% gate. Only docs changed.

| Check | Evidence | Result |
| :--- | :--- | :--- |
| Root cause addressed, not just the example | §6.5 item 4: `epoch` is required for `all`/`feed`/`category`, and a mismatch is rejected with zero side effects. Old-epoch sequence bounds can no longer reach post-restore items, whatever the timing. | ✅ |
| All paths guarded | Validation (§6.5 item 4), request schema (§6.4 table), push flow: step 1 reads the epoch without a lock, so §5.1 ordering holds; step 3 locks candidates only for epoch-matching mutations; step 4 rejects. Single-field mutations and `scope='items'` stay correctly epoch-agnostic, because they carry no sequence bound. | ✅ |
| Client contract | §6.2 step 5: the client drops old-epoch scoped bulk reads or rewrites them as `scope='items'` over item IDs known pre-restore, before re-bootstrap overwrites local state. | ✅ |
| Test planned | `1b-T09(b)` replays the exact scenario `W=1000 → restore 900 → 901..950 → offline "mark all read"` and asserts `epoch_mismatch` while sibling mutations still apply. | ✅ |
| Docs synced | `1b-D05`, §11 (ADR 0004 / `data-model.md` §5.2 alignment), resolution §9. | ✅ |

**Verdict**: `P-11-F1` ✅, so `P-11` ✅. All 17 findings are resolved.

**Note carried to the `1c` design (non-blocking)**: when the client rewrites an old-epoch scoped bulk read as `scope='items'`, it must give the rewritten mutations **new** `client_mutation_id`s. Reusing the original ID with a different payload is rejected as `mutation_id_conflict` (`P-17` `payload_sha256`), which would roll back the user's action.

### Residual risks for owner acceptance (needed at approval)
1. **Scoped bulk mark-read is order-dependent** (`P-01` / `Q-2`, §6.5 item 4).
2. **`expired` items are excluded from bulk mark-read**, including `scope='items'` (§6.5 item 4).

### Status
`Verified (2026-11-25)`. The design is ready for the owner's approval decision.

---

## 11. Owner Approval (2026-11-25)

The project owner approved the `1b` component design and accepted both residual risks from §10, recorded as `RR-1` and `RR-2` in design §12:
- **RR-1**: scoped bulk mark-read (`all`/`feed`/`category`) depends on arrival order.
- **RR-2**: `expired` items are excluded from every bulk mark-read mode.

`1b` implementation may now begin. Per design §11, the `data-model.md` / ADR 0004 alignments land in the same commit as the `1b` implementation.
