# Design Review — Milestone `1c` Component Design (Client UI, Offline Shell & System Health)

- **Date**: 2026-11-25
- **Scope**: [`docs/design/1c-client-ui-and-offline-shell.md`](../design/1c-client-ui-and-offline-shell.md) (status `In Design Review`, commit `1ae1444` on branch `milestone/1c`), plus the `1a`/`1b` code it consumes (`backend/src/smartrss/api/app.py`, `api/routes/system.py`, `sync/schemas.py`, `config.py`, `deploy/nginx/nginx.conf`)
- **Specs checked against**: `requirements.md` (§4.5–§4.7, §5, §6 `1c` row), `data-model.md` §6.2, `architecture.md`, ADRs `0002`, `0004`, `0005`, `0006`, `development-process.md` §2 and §7, `review-checklist.md`, `1a-backend-ingestion-and-security.md` §4, §6, §10, `1b-sync-protocol-and-search.md` §6–§8
- **Reviewer**: Claude Code (Claude Opus 5.5)
- **Finding prefix**: `U` (`U-01`–`U-21`)
- **Status**: `Addressed (2026-11-25) — awaiting verification`

## Review outcome

The design is thorough and follows the `1b` normative client rules closely:

- The bootstrap pins page 1's `watermark`, restarts on an epoch flip, and runs a trailing pull.
- The push response never moves the cursor (`P-03`).
- Scoped bulk reads are rewritten to `scope='items'` before re-bootstrap (`P-11-F1`).
- Connected search results stay display-only (`P-17`).
- Batching respects all three server caps and measures serialized UTF-8 bytes (`S6`).
- Snippets render as React text nodes, and remote images are blocked by default.
- The runner has a `Host` allow-list, canonicalizes `127.0.0.1` to `localhost`, and validates `SMARTRSS_API_ORIGIN` before putting it in the CSP header.
- Every Nginx `location` includes the security headers, which handles the `add_header` inheritance trap correctly.

The main risks are in the client's local state model, not in the wire protocol:

1. **Rollback restores a stale local snapshot** (`U-01`). The design keeps no server-confirmed base copy of each row. Once a pull has moved the cursor past a newer server version, restoring `previous_snapshot` leaves Dexie wrong until that entity happens to change again.
2. **`in_flight` outbox rows have no recovery path** (`U-02`). If a tab closes mid-push, those mutations are never sent again.
3. **`is_starred` cannot be indexed** (`U-03`). IndexedDB does not accept booleans as keys, so the `is_starred` and `[is_starred+fetched_at]` indexes stay empty.
4. **Systematic `422`s quarantine the whole outbox** (`U-04`). Bisection cannot tell one bad mutation from a client/server version mismatch. A stale Service Worker build would roll back and quarantine every offline action, with no way to retry.
5. **The LAN fallback is not a secure context** (`U-05`). `crypto.randomUUID()` and `navigator.locks` are not available there, so every read/star action in the Docker `web` fallback throws.
6. **Bootstrap holds the whole working set in memory** (`U-06`) and writes it in one transaction. The working set has no upper bound.

| Severity | Count | IDs |
| :--- | :--- | :--- |
| Blocker | 0 | — |
| Major | 6 | `U-01`–`U-06` |
| Minor | 12 | `U-07`–`U-18` |
| Nit | 3 | `U-19`–`U-21` |

**Recommendation**: Approve once `U-01`–`U-06` are resolved in the design or accepted by the owner. Most Majors have small, local fixes:

- `U-01` and `U-02` share one fix: a server-base shadow plus a defined outbox lifecycle.
- `U-03` is a storage encoding change.
- `U-05` needs a UUID fallback and a decision on what the fallback mode supports.

The Minors can be fixed in the design or carried into implementation as tracked items.

**Trade-off confirmations (`1c-D01`…`1c-D08`)**: I agree with all eight. Notes:
- **D01**: sound. See `U-16` on the port choice and `U-18` on how `dist/` is installed and updated.
- **D03**: the conclusion holds, but the stated reason is partly wrong (`U-19`).
- **D05**: this decision does not cover what happens when the API origin changes after pairing (`U-08`).
- **D06**: see `U-14`. The update flow contradicts the network-first navigation rule.
- **D08**: the 401-preservation idea is right. The re-pair path needs `U-11`.

**Verified vs inferred**:
- **Verified** by running code against the repo:
  - Pydantic `2.14.4` serializes UTC as `…:00Z` with no fraction, or as `….123456Z` with six fractional digits. `feeds.field_updated_at` values come from Python `isoformat()` (`+00:00`).
  - A whole-second timestamp sorts lexically *after* a `.500Z` timestamp in the same second (`U-07`).
  - `app.py` adds `HostAllowListMiddleware` last, so it is outermost. A `Host`-rejected response therefore carries no CORS headers (`U-17`).
  - `system/health` returns `current_seq` and `instance_epoch`.
  - Node is **not installed** on this host (see question Q-4).
- **From spec or platform knowledge, not run here**:
  - IndexedDB key validity (booleans are not valid keys).
  - `[SecureContext]` on `crypto.randomUUID`, Web Locks and `StorageManager`.
  - CSP `style-src` does not govern CSSOM writes.
  - Nginx `add_header` inheritance.

---

## 1. Major

### U-01 — Rollback and overlay work from stale local snapshots; there is no server-base copy
**Location**: §6.4 "Enqueuing" step 2 (`previous_snapshot`), §6.4 push `422` bisection ("roll back … using `previous_snapshot`"), §6.4 `rejected` third branch, §6.5 overlay.

Each outbox row records the row's state *at enqueue time*. Later pulls overwrite the Dexie row with newer server state and re-apply the overlay, but `previous_snapshot` is never refreshed. Rollback then restores the old snapshot:

- **Stale-snapshot divergence**: offline, the user marks item X read (snapshot `S0`: unread, unstarred). A push gets `503`, and the cycle continues to pull, which delivers another device's star (`S1`: unread, starred) and advances the cursor. The overlay correctly shows read and starred. Next cycle, the mutation is quarantined as a poison pill (`422`), and the rollback restores `S0`. X now shows **unstarred**, and no later pull will re-deliver `S1` because the cursor has already passed it. Dexie stays wrong until X changes on the server again.
- **Cross-field clobber**: the item snapshot covers *both* `state` and `is_starred`. Rolling back `M1 = item_set_state(read)` after a later pending `M2 = item_set_starred(true)` restores `is_starred=false`. Only the first two `rejected` branches say "re-apply later pending mutations". The `previous_snapshot` branch and the poison-pill path do not.

**Suggested fix**: keep the server's version of each row separately from the optimistic overlay. For example, add a `server_base` field on `items`/`feeds` rows, or a parallel `items_base` table. `bootstrap`, `pull`, and `current_entity` merges write **only** the base. The displayed row is always `base + fold(pending outbox mutations for this entity, local_seq ASC)`. Rollback or quarantine then means deleting the outbox row and recomputing from the base. You no longer need `previous_snapshot` or `captured_item_snapshots` for correctness, though they can stay for UI text. Make the recompute one pure function in `reconciliation.ts` (property-test it with `fast-check`: any interleaving of pulls and outbox removals gives `display == base ⊕ pending`).
**Tests**:
- The `503` → pull → `422` scenario above ends with X starred.
- Rolling back `M1` keeps `M2`'s star.

### U-02 — `in_flight` outbox status has no defined lifecycle
**Location**: §5.1 `outbox.status: 'pending' | 'in_flight'`, §6.4 "Read pending `outbox` rows", §6.5 "Load all remaining pending `outbox` rows", §5.3 step 1.

The design never says when a row becomes `in_flight` or when it goes back to `pending`. If rows are marked `in_flight` before the `fetch` and the tab is closed, crashes, or loses the lock mid-request, those rows are never `pending` again. Every later flush reads only `pending`, so the mutations stay stuck forever, even though the outbox count badge keeps showing them. The same ambiguity affects the overlay (§6.5 reads "pending") and eviction protection: an `in_flight` mutation's optimistic state could be reverted by a pull, or its item evicted.

**Failure scenario**: The user presses `m` and closes the laptop lid during the debounced push. The tab is discarded, and the `item_set_state` never reaches the server. This is checklist **L1** ("work that can never complete").

**Suggested fix**:
- Either drop `in_flight` and keep rows `pending` until the response is processed (the lock already prevents a double flush), or reset every `in_flight` row to `pending` as the first step after acquiring `smartrss-sync-cycle`.
- Define the overlay and eviction protection over **all** outbox rows, regardless of status.
- Increment `attempts` on transport failure, so the Outbox Inspector can show stuck rows.

**Test**: Mark a batch `in_flight`, drop the engine without processing the response, start a new engine, and assert the rows are re-sent with the same `client_mutation_id` (the server replays idempotently).

### U-03 — `is_starred` is a boolean, which IndexedDB cannot index
**Location**: §5.1 `items` indexes `is_starred`, `[is_starred+fetched_at]`.

IndexedDB keys may be numbers, strings, dates, binary values or arrays, but **not booleans**. A record whose indexed key path resolves to an invalid key is simply left out of that index. Every item would be missing from `is_starred` and from `[is_starred+fetched_at]`. In Dexie, `where('is_starred').equals(true)` throws `DataError`. `fake-indexeddb` follows the spec, so tests would fail, but the design as written does not work.

**Failure scenario**: The **Starred** smart filter and the count of starred items, both meant to come from that index, are empty or throw. Code that falls back to a full scan works but is slow, which defeats the index.

**Suggested fix**: In Dexie, store a numeric `starred` field (`0 | 1`) next to the DTO boolean, or store the boolean as `0/1` and convert at the DTO boundary. Index `starred` and `[starred+fetched_at]`. While you are there, add the `effective_date` index that §5.1 says exists but does not list (`U-20`).
**Test**: A `fake-indexeddb` test that puts starred and unstarred items and queries the compound index.

### U-04 — Poison-pill bisection treats a systematic `422` as N poison pills, irreversibly
**Location**: §6.4 push error handling (`422`/`400`/`413` bisection), §5.1 `failed_mutations`, §10.5 (quarantine offers only "dismiss").

A `422` affecting every mutation is far more likely than one malformed mutation. The client builds every payload itself, from typed code. The realistic cause is **version skew**:

- The Service Worker deliberately keeps serving the old bundle until the user clicks "Update available".
- The laptop can be offline for days while the server is upgraded.
- In Phase 2, Android clients will lag behind the server.

Bisection then works down to single mutations. Each one fails, so *every* pending mutation is quarantined and its optimistic state rolled back. The user sees their whole offline session undone and has no way to re-send it.

**Failure scenario**: The server adds a required field to `item_set_starred` in `1g`. A laptop with 120 offline actions reconnects on the stale `1c` bundle. 120 quarantines follow, 120 rollbacks, and 120 error toasts. The outbox is empty, and the work is lost.

**Suggested fix**:
- Quarantine a single mutation only when its **sibling half succeeded** (true isolation).
- If both halves of a split fail, or more than *k* (for example 3) leaves fail in one cycle, stop. Move to `sync_error` (`"Server rejected this client's changes — update the app"`), keep the outbox intact, and prompt an update or reload.
- Add **Retry** next to Dismiss for `failed_mutations` (re-enqueue with the same `client_mutation_id`).
- Optionally, include the client build version in the `health` probe response and compare it with an API version.

**Test**: A server stub returning `422` for every request leaves the outbox unchanged and the state machine in `sync_error`. One bad mutation among nine good ones is still isolated.

### U-05 — The LAN fallback origin is not a secure context: `crypto.randomUUID()` and `navigator.locks` are missing
**Location**: §6.4 "Enqueuing" step 1 (`crypto.randomUUID()`), §6.2 (`navigator.locks`), §5.2/§7.2 (which handle only `persist()` and the Service Worker for insecure origins), §10.3 (online mode also routes read/star through the outbox).

`crypto.randomUUID()` and the Web Locks API are both `[SecureContext]`. On `http://smartrss.lan:8080` (the Docker `web` fallback, per ADR 0005), `crypto.randomUUID` is `undefined` and `navigator.locks` is `undefined`. Every read, star and feed toggle goes through the outbox enqueue, so **every one throws** in fallback mode. The lock falls back to the in-process mutex (§6.2), so two fallback tabs flush at the same time without coordination.

**Failure scenario**: The owner opens the LAN URL from the desktop to triage quickly, presses `m`, and gets an uncaught `TypeError`. No test catches this, because `1c-T09`–`T14` all run on `http://localhost:4173`, which is a secure context.

**Suggested fix**:
- Add a `uuidv4()` helper that uses `crypto.randomUUID` when present and otherwise builds a v4 UUID from `crypto.getRandomValues` (available on insecure origins).
- When `navigator.locks` is missing, either use a `BroadcastChannel`/IndexedDB lease-based lock, or state that fallback mode is single-tab and detect a second tab.
- Decide and document what fallback mode supports (see question Q-2).

**Test**: Add a Playwright project that runs the reader smoke flow against the Nginx `web` container on a non-localhost hostname (e.g. `http://smartrss.test:8080` mapped through `--host-resolver-rules`). That also covers the CORS and `Host` setup for real LAN hostnames (`U-15`).

### U-06 — Bootstrap accumulates the entire working set in memory and commits it in one transaction
**Location**: §6.3 steps 1–3 (`accumulatedItems = [...]` across all pages, then one Dexie `rw` transaction), §1c-D04 ("2,000–10,000 working-set items").

The working set is **unbounded**. It holds every unread item up to the expiration TTL (default 30 days, `0` = never), plus all starred items, plus 14 days of read items. For example, 100 feeds × 10 items/day × 30 days ≈ 30,000 unread items, and starred items accumulate forever. Each `SyncItemDTO` carries a `raw_summary` of up to 32,768 characters plus a 1,500-character `lead_excerpt`. At a modest 6 KB average, that is about 180 MB of UTF-8 in a JS heap, roughly double as UTF-16 strings. All of it is held until the final transaction, which then writes it all at once.

**Failure scenarios**:
- The tab runs out of memory or the transaction aborts on a large library.
- A network drop on page 59 of 60 discards all 58 completed pages, and the next attempt starts again from zero.
- On a laptop that is often offline, an `epoch_mismatch` re-bootstrap might never complete.

**Suggested fix**: Stream pages into a staging area:
- Write each page into a `bootstrap_items` staging table (or tag rows with a `bootstrap_generation`) in its own short transaction.
- Persist `{page1Epoch, page1Watermark, next_cursor}` so the bootstrap can resume.
- After the last page, do a short atomic swap: delete the old generation, move the staged rows, write `client_meta['sync']`, and recompute the overlay.

Also put a number on the expected maximum working set in §5 so the `< 50 ms` MiniSearch hydration claim has a stated bound (`U-20`).
**Test**: A bootstrap of 3 pages that fails on page 3 and resumes from page 3's cursor. Peak heap is bounded by one page.

---

## 2. Minor

### U-07 — Timestamps are not normalized at the DTO boundary, but are compared as strings
**Location**: §5.1 ("all timestamps stored as canonical UTC ISO-8601 … lexicographically sortable"), §5.3 (`read_at < cutoffIso`), §5.3 step 5, §6.5 (`items.put(record)` with no transform), `effective_date` sorting.

The server emits three formats (verified on Pydantic 2.14.4): `2026-11-25T12:00:00Z` with no fraction, `…:00.123456Z` with six fractional digits, and Python `isoformat()` with `+00:00` inside `field_updated_at`. String comparison is wrong across these formats. For example, `"…12:00:00Z" > "…12:00:00.500Z"` is **true**, because `'Z'` sorts after `'.'`. The design asserts the canonical form, but §6.3 and §6.5 store `data` as received.

**Failure scenario**: The date sort misorders items in the same second. The eviction cutoff and the out-of-window guard can misclassify items exactly at the boundary. The effect is small, but the bug is silent.

**Suggested fix**: Add one `toCanonicalIso()` converter, applied in `api/client.ts` DTO parsing to every timestamp field (including the nested `field_updated_at` values, `server_now` and `changed_at`). Truncate to milliseconds and use `Z`. This is checklist **D6** applied on the client side.
**Test**: Feed DTOs with all three formats and assert the stored strings and their sort order.

### U-08 — The API origin can change after pairing, and the design does not say which origin wins
**Location**: §1c-D05 ("preserves IndexedDB if the server LAN IP changes later"), §5.1 `client_meta['auth'].api_origin`, §7.1 (`/client-config.json` network-only, with a fallback to the Dexie origin when offline), §7.1 navigation fallback.

Two origins exist: the runner's `/client-config.json` and the origin stored at pairing time. The design does not say which one is used for requests, or whether the bearer token may be sent to a *different* origin than the one it was paired with. If the user points `SMARTRSS_API_ORIGIN` at another server, for example a test stack, the production token and outbox are sent there.

A second problem: the precached `/index.html` response stores the CSP header from install time. After the origin changes, an offline-loaded page has `connect-src` pinned to the *old* origin. When the network returns, every sync is CSP-blocked until the page is reloaded.

**Suggested fix**:
- Make the config origin authoritative.
- If it differs from `auth.api_origin`, pause sync and ask the user: *"Server address changed from A to B — same server?"* On confirm, update `auth.api_origin`; verify that `instance_epoch` matches on the first pull, and otherwise re-pair.
- Refresh the cached `/index.html` on every successful network navigation, or at least on a config/CSP change.

**Test**: A config-origin mismatch pauses sync and sends no `Authorization` header.

### U-09 — Multi-tab coordination can lose a wake-up
**Location**: §6.2 (`ifAvailable: true`; the skipping tab "listens for … `outbox_enqueued`").

Tab B enqueues a mutation while tab A holds the lock and has already finished its "loop until outbox empty" check. Tab B's sync attempt finds the lock taken and skips. The design does not say who acts on `outbox_enqueued`. The mutation waits for the next 60 s timer, and if every tab is hidden it waits indefinitely, because the timer is paused while `document.hidden`.

**Suggested fix**: Do one of the following:
- After releasing the lock, the holder re-checks the outbox and loops again if it is non-empty.
- The skipping tab queues a non-`ifAvailable` lock request, collapsed to at most one waiter.
- The holder handles `outbox_enqueued` by setting a `dirty` flag that it checks before releasing.

**Test**: Enqueue between the holder's last outbox read and its lock release, and assert that a push follows.

### U-10 — Acknowledged scoped bulk reads are lost on restore, and `cursor_ahead` skips journal promotion
**Location**: §5.1 `replay_journal` (holds only epoch-agnostic types), §6.4 `applied`, §6.6 step 2 (runs only for `epoch_mismatch`).

- An *acknowledged* "Mark all read" after the last backup is not journaled. After a restore, potentially thousands of items come back unread. This is the most common bulk action. `1b` allowed dropping these entries, but the client already holds `captured_item_ids`, so it can preserve them cheaply.
- §6.6 treats `cursor_ahead` as a restore for the purpose of rewriting outbox entries. That is correct: it means the database was restored without an epoch rotation. But the journal is promoted only on `epoch_mismatch`, so post-backup acknowledged actions are lost in exactly the case where the runbook step was skipped.

**Suggested fix**:
- On `applied` of a scoped bulk read with `captured_item_ids.length ≤ 5,000`, journal its rewritten `scope='items'` form (chunks of ≤ 1,000, fresh IDs generated once and stored).
- Promote the journal on `cursor_ahead` too.
- If dropping these entries is intended, record the gap as an accepted residual risk (question Q-3).

### U-11 — Re-pairing changes `device_id`, so previously sent mutations come back as `mutation_id_conflict`
**Location**: §6.1 (`auth_revoked → syncing`: re-pair preserves the outbox), §6.6 step 2; `1b` §6.7 step 2 ("Different device_id … → `mutation_id_conflict`").

The server reserves a `client_mutation_id` per device. After a re-pair, any mutation the old device already sent gets `rejected / mutation_id_conflict` for the new device. That covers a push that committed but whose response was lost to a timeout, and every journal entry replayed after a restore. The client then shows a "change rejected" toast and runs a rollback, for changes that actually took effect. `current_entity` limits the state damage, but the noise is real, and with `U-01` a rollback can still restore the wrong state.

**Suggested fix**: On a successful re-pair, mint fresh `client_mutation_id`s for every outbox and journal row. This is safe: every single-field and `scope='items'` mutation is idempotent by value under LWW. Alternatively, treat `mutation_id_conflict` after a re-pair as `applied`, with no notification.
**Test**: Push times out, the device is revoked and re-paired, and the re-flush produces no rejection notification.

### U-12 — Mutating an archive-only search result has no defined enqueue path
**Location**: §8.1 ("Clicking Star or Restore … on an archive-only result enqueues the normal mutation … optimistically updating the search-result row in React state"), §6.4 step 2 ("Read the current target row(s) in Dexie").

The enqueue algorithm assumes a Dexie row exists, to snapshot it and apply the change optimistically. For archive-only items there is none. The design does not specify:
- what `previous_snapshot` holds,
- whether eviction protection matters,
- how a `rejected` result rolls back a React-state row,
- what `superseded` does when there is no row to merge into.

**Suggested fix**: Specify a `target_absent` enqueue variant. Snapshot from the `SearchItemDTO`, leave Dexie untouched, and route rejection or supersession to the search-result state plus a notification. With `U-01`'s base model, this simply becomes "no base row; the overlay applies only to display state".
**Test**: Extend `1c-T08` with a `rejected` star on an archive-only result.

### U-13 — The sanitizer parses twice, which adds a mutation-XSS (mXSS) surface; relative links and `extracted_text` rendering are unspecified
**Location**: §9.2 stages 2–3, §10.2 "Load full extracted text".

- Stage 2 parses with `DOMParser`, serializes, and stage 3 re-parses inside DOMPurify, which serializes again for `innerHTML`. Each serialize/parse round trip is the classic mXSS vector, and DOMPurify's guarantees assume it sees the markup that will finally be parsed. Do the `<img>` → placeholder rewrite **inside** DOMPurify, in an `uponSanitizeElement` hook, so there is one parse and one serialization. Alternatively, pass the node with `RETURN_DOM_FRAGMENT` and append it directly instead of using `dangerouslySetInnerHTML`.
- `<a href>` values are checked with `isSafeHttpUrl()` and no base URL, so every relative link in feed HTML (for example `/2026/11/post`) is stripped. Resolve against `baseUrl` first, as stage 2 does for images.
- `extracted_text` from `GET /api/v1/items/{id}` is missing from the plain-text-only list in §9.2. State that it is rendered as text, never as HTML.

**Test**: Add DOMPurify's known mXSS fixtures (namespace confusion: `<math><mtext><table><mglyph><style>…`, `<noscript>`, `<form><math>`) to `1c-T02`.

### U-14 — The Service Worker update model contradicts itself, and there is no IndexedDB `versionchange` handling
**Location**: §7.1 (navigations are network-first), §7.2 (`skipWaiting` only after the user clicks).

Because navigations are network-first, any reload while online already loads the **new** `index.html` and new hashed JS, still under the old Service Worker. That makes the user-gated `SKIP_WAITING` mostly moot, and it can run new and old code in different tabs at the same time. When a later milestone bumps the Dexie schema version (for example `1d`/`1e` indexes on `enrichment` or `clusters`), an old tab blocks the upgrade, and nothing handles `versionchange`.

**Suggested fix**: Choose one model and document it. The simplest is to serve navigations cache-first from the precache, so the user-gated update is real. Add `db.on('versionchange', () => { db.close(); prompt reload })` now, while the schema is v1.
**Test**: Two tabs; one triggers a schema upgrade; the other closes cleanly.

### U-15 — Test-plan gaps
**Location**: §12.2.

The `T2` claim in §11 ("every matrix row maps 1:1") holds, but the matrix leaves out these behaviors:
- **`NFR-SYNC-02`**: "500-mutation flush < 5 s on LAN" has no test. Add a timed E2E or integration assertion.
- **Real deployment topology**: every E2E uses a loopback API origin. None exercises a non-localhost API hostname (CORS plus `ALLOWED_HOSTS`, CSP `connect-src` pinning) or the Nginx `web` fallback (`U-05`).
- There are no tests for `U-01`, `U-02`, `U-09` and `U-11` (outbox lifecycle and re-pair), for the overlay of a scoped bulk read during a pull (it must use `captured_item_ids`, not re-run the scope predicate; the design is silent on this), for 48 h journal pruning, or for the Service Worker update flow.
- **CSP pinning**: add a negative test asserting that `fetch('https://other.example')` raises a `securitypolicyviolation`, so the pinning is proven and not just asserted.
- **Runner**: `[::1]:4173` `Host` → `308`; `HEAD`; percent-encoded traversal (`%2e%2e%2f`, double-encoded).

### U-16 — Port 4173 is Vite's `vite preview` default
**Location**: §4.1, §1c-D01.

`npm run preview` binds `localhost:4173` by default. A developer previewing a build on the same laptop gets the **production origin**: the same IndexedDB with the real bearer token and outbox, and the same Service Worker registration, now controlled by a build that may point at a dev API. The two can also collide on the port.

**Suggested fix**: Pick a different port (for example 4180), or set `preview.port` in `vite.config.ts` to something else. Note it in §3.2.

### U-17 — The reachability probe cannot tell "server rejects this origin" from "offline"
**Location**: §6.2 probe, §6.1 `detached`.

If `CORS_ALLOW_ORIGINS` lacks `http://localhost:4173`, or `ALLOWED_HOSTS` lacks the LAN hostname, every request fails as an opaque `TypeError`. Verified: `HostAllowListMiddleware` is outermost in `app.py`, so its `400` has no CORS headers. The client sits in `Detached` forever with no hint. Only the pairing screen mentions the misconfiguration.

**Suggested fix**: On a probe `TypeError`, send one `fetch(…, {mode: 'no-cors'})`. If that resolves, the server is reachable and the origin is rejected, so show a distinct `Misconfigured` pill with the same `.env` hint.

### U-18 — Installing and updating the laptop runner is unspecified
**Location**: §4.3.

The unit file expects `~/.local/share/smartrss/{dist,smartrss_client_runner.py}`, but the design never says how they get there: a build on the laptop, a copy from the server's `web` image, or a `make install-client` target. It also omits how updates and rollbacks work, and how users enable the unit (`systemctl --user enable --now`, `WantedBy=default.target`). Updates matter here because they drive the Service Worker update flow (`U-14`).

**Suggested fix**: Add a `make install-client` / `scripts/install-client.sh` step that builds or copies `dist/`, installs the unit and the `.desktop` file, and is idempotent. Document the update steps in `README.md`.

---

## 3. Nit

### U-19 — The CSP reason for banning React `style` props is inaccurate
**Location**: §1c-D03, §4.2.

CSP `style-src` without `'unsafe-inline'` blocks `style="…"` *attributes* in parsed markup and `setAttribute('style', …)`. It does **not** govern CSSOM writes (`el.style.x = …`), which is how React applies `style` props. The ESLint ban is a fine style rule, and the DOMPurify `style` ban is what actually matters, but the stated reason is wrong. The ban also rules out windowing libraries that position rows with inline transforms, which may matter for "thousands of items" (§10.2). Reword the rationale, or allow `style` in one audited virtual-list component.

### U-20 — Index list and performance claims are inconsistent
**Location**: §5.1, §1c-D04, §8.2.

- §5.1 says `effective_date` is "indexed for fast date sorting" but does not list it among the indexes.
- D04 claims sub-5 ms search over "2,000–10,000" items, while §8.2 claims hydration in "< 50 ms for 2,000–5,000". Neither figure is measured, and MiniSearch indexing about 1.7 KB per document is probably slower than that.

Pick the stated bound (see `U-06`) and add a `vitest` benchmark. Also note that MiniSearch does not stem by default while PostgreSQL `english` does, so connected and detached results will differ for queries like "running" vs "run". Either add a stemmer or accept and document the difference.

### U-21 — Small wording and lifecycle points
- **§6.6 step 2**: "ordered before newer pending outbox mutations" cannot be done with the `++local_seq` auto-increment key, and it is not needed, because every journaled type commutes under LWW. Delete the clause or specify how rows are renumbered.
- **§9.3**: "Sign Out & Clear Local Cache" should also call `POST /api/v1/devices/{self}/revoke` when online, so the discarded token stops being valid server-side.
- **§10.3 `EditFeedModal`**: state that `PATCH` sends only the fields that changed (checklist **D3**). Otherwise an unchanged `status`/`category_id` re-sent online gets a fresh monotonic timestamp and silently supersedes a pending offline `feed_set_field`.

---

## 4. Questions for the project owner

- **Q-1 (coverage threshold)**: `development-process.md` §2.2 sets **80%** for frontend core. That is what the design uses, so it is not a finding. However, `reconciliation.ts`, `outbox.ts`, `engine.ts`, `sanitizeHtml.ts` and `urlSafety.ts` are the client halves of modules the backend holds to **90%** (sync reconciliation, security). Should these five be held to 90%?
- **Q-2 (fallback scope)**: Should the Docker `web` LAN fallback support the full outbox (read/star/feed toggles), which requires the `U-05` fixes, or should it be an explicitly read-mostly admin view (health, feeds, settings) with mutations disabled?
- **Q-3 (restore semantics)**: Should acknowledged scoped bulk reads after the last backup survive a restore (`U-10`), or is that loss accepted as a residual risk?
- **Q-4 (toolchain)**: `make check` will now need Node 22 plus the Playwright browsers on the dev host. Node is not installed on this machine. Should the frontend checks run on the host as designed, or inside a pinned `node:22` container from `make check`, so the gate is reproducible (checklist **T5**)?

---

## 5. Next step

Antigravity runs `address-review-findings` against `U-01`–`U-21`, updates the design (and `data-model.md` §6.2 if the base/overlay model changes the documented client cache), and marks this review `Addressed`. The reviewer then runs a verification pass in a fresh session.

---

## 6. Owner Decisions (`Q-1`–`Q-4`, Recorded 2026-11-25)

- **Q-1 (Frontend coverage threshold)**: **Two-tier frontend gate confirmed.** Hold the critical client sync & security modules (`src/security/uuid.ts`, `src/security/urlSafety.ts`, `src/security/sanitizeHtml.ts`, `src/sync/reconciliation.ts`, `src/sync/outbox.ts`, `src/sync/engine.ts`) to **`>= 90%`** line+branch coverage (matching the backend critical gate), and the remaining 4 core modules (`src/db/schema.ts`, `src/db/clientMeta.ts`, `src/search/queryParser.ts`, `src/search/localSearch.ts`) to **`>= 80%`**. Recorded in `docs/design/1c-client-ui-and-offline-shell.md` §12.1 and `docs/development-process.md` §2.2.
- **Q-2 (Docker `web` LAN fallback scope — `U-05`)**: **Full connected reading & mutations confirmed.** Plain-HTTP LAN fallback supports full reading, starring, bulk mark-read, and offline-capable feed toggles using a `crypto.getRandomValues` UUIDv4 fallback (`src/security/uuid.ts`) and an IndexedDB lease lock fallback (`src/sync/lock.ts`), verified by a Playwright smoke test against Nginx `web` on a non-localhost hostname (`1c-T15`).
- **Q-3 (Restore semantics — `U-10`)**: **Preserve acknowledged scoped bulk reads confirmed.** On `applied` of a scoped bulk mark-read with `<= 5,000` `captured_item_ids`, store its chunked `scope='items'` rewrite (`<= 1,000` IDs/chunk, stable minted UUIDs) in the 48h `replay_journal`; promote `replay_journal` on **both** `epoch_mismatch` and `cursor_ahead`. Recorded in `1c` §6.4/§6.6 and `docs/data-model.md` §6.2.
- **Q-4 (Frontend toolchain & preflight checks)**: **Host-native `make check` + preflight dependency checks confirmed.** Node `v22.22.1` + `npm` are installed on the Ubuntu host; `scripts/check-prereqs.sh --mode <dev|server|client|e2e>` is wired into `make check`, `make up`, `make install-client`, and `make test-e2e` to check exact tool versions upfront with actionable install hints (`FRONTEND_RUNTIME=docker` optional override; `package-lock.json` + `npm ci` + pinned `node:22-alpine` in `frontend/Dockerfile` for `T5`).

---

## 7. Resolutions (Addressed 2026-11-25)

All 6 Majors (`U-01`–`U-06`), 12 Minors (`U-07`–`U-18`), and 3 Nits (`U-19`–`U-21`) are resolved in [`docs/design/1c-client-ui-and-offline-shell.md`](../design/1c-client-ui-and-offline-shell.md), with aligned updates in [`docs/data-model.md`](../data-model.md), [`docs/development-process.md`](../development-process.md), [`docs/review-checklist.md`](../review-checklist.md), [`docs/design/README.md`](../design/README.md), and [`docs/README.md`](../README.md).

### Major
- **U-01** — Fixed. Separated authoritative server-confirmed base tables (`items_base`, `feeds_base`) from indexed display tables (`items`, `feeds`) in `1c` §5.1, §6.4, §6.5 and `docs/data-model.md` §6.2. `bootstrap`, `pull`, and `current_entity` merges write only `*_base`; display rows are always computed by pure `applyOutboxToItem` / `applyOutboxToFeed` (`display = base ⊕ fold(outbox)`), removing `previous_snapshot` / `captured_item_snapshots` as correctness mechanisms. Added `B1` to `docs/review-checklist.md` and `fast-check` + `503 → pull → 422` + cross-field rollback tests in `1c-T03`.
- **U-02** — Fixed. Dropped `outbox.status: 'in_flight'` completely (`1c` §5.1, §6.4, §6.5, §5.3); rows remain in `outbox` until server outcome or isolated quarantine, with `attempts` incremented on transport/5xx failure. Overlay recomputation and eviction protection always scan all `outbox` rows. Added mid-push crash recovery test in `1c-T04`.
- **U-03** — Fixed. Replaced boolean IndexedDB indexing with derived numeric `starred_num: 0 | 1` indexed as `starred_num` and `[starred_num+effective_date]` (`1c` §5.1, §5.3, §6.4, §10.2). Added `B2` to `docs/review-checklist.md` and `fake-indexeddb` compound-index query test in `1c-T03`.
- **U-04** — Fixed. Made `422`/`400`/`413` bisection skew-safe (`1c` §6.4, §10.5): a single mutation is quarantined into `failed_mutations` only if its sibling half succeeded (or standalone `413`), capped at `MAX_QUARANTINES_PER_CYCLE = 3`. If both halves fail (or a standalone `422`/`400` has no succeeding sibling), sync halts immediately in `sync_error` leaving `outbox` intact with zero rollbacks. Added **Retry** alongside Dismiss in Outbox Inspector and both systematic-`422` and isolated-poison-pill tests in `1c-T04`.
- **U-05** — Fixed (per Owner Decision `Q-2`). Added `src/security/uuid.ts` (`crypto.randomUUID` → `crypto.getRandomValues` fallback) and `src/sync/lock.ts` (`navigator.locks` → Dexie `client_meta['sync_lock']` lease-lock fallback with 15 s TTL / 5 s heartbeat) in `1c` §1c-D01, §3.1, §5.1, §6.2. Added `B3` to `docs/review-checklist.md`, unit tests in `1c-T06`, and Playwright non-localhost Nginx `web` fallback project (`http://smartrss.test:8080`) in `1c-T15`.
- **U-06** — Fixed. Replaced in-memory bootstrap accumulation with two-phase streaming resumable bootstrap using Dexie staging tables `bootstrap_meta` (`phase: 'fetching' | 'committing'`) and `bootstrap_items` (`<= 500` rows/tx fetch, resumable by `next_cursor`; `1,000`-row chunked swap with `bootstrap_gen` atomic activation) in `1c` §5.1, §6.3, and documented the working-set envelope (2,000–6,000 typical, tested up to 20,000). Added 3-page fail-and-resume test in `1c-T05`.

### Minor
- **U-07** — Fixed. Added `src/api/normalize.ts` (`toCanonicalIso()`) normalizing every incoming DTO timestamp (including nested `feeds.field_updated_at` values) to fixed-width 24-char UTC `YYYY-MM-DDTHH:mm:ss.sssZ` at the `api/client.ts` boundary (`1c` §3.1, §5.1, §6.3–§6.5). Added 3-format normalization & sort-order test in `1c-T03`.
- **U-08** — Fixed. Made `/client-config.json` `api_origin` authoritative; mismatch vs `auth.api_origin` transitions immediately to `origin_confirm` pausing all authenticated requests until user confirms (`1c` §6.1, §6.2). Runner appends deterministic `// smartrss-origin-tag:<sha256[:12]>` to `/sw.js` and SW installs with `{ cache: 'reload' }` so origin changes refresh precached `/index.html`'s CSP header automatically (`1c` §4.1, §7.1–§7.2). Added tests in `1c-T01` and `1c-T06`.
- **U-09** — Fixed. Added lock-holder `dirty` flag (set on `BroadcastChannel` `outbox_enqueued` or local enqueue) and pre-release `outbox.count()` re-check loop (capped at `MAX_DRAIN_LOOPS = 5`) in `1c` §6.2 so wake-ups between last read and lock release are never lost. Added race test in `1c-T06`.
- **U-10** — Fixed (per Owner Decision `Q-3`). On `applied` of scoped `item_mark_bulk_read` (`<= 5,000` `captured_item_ids`), journal chunked `scope='items'` (`<= 1,000` IDs/chunk, stable UUIDs) into the 48h `replay_journal`, and promote `replay_journal` on **both** `epoch_mismatch` and `cursor_ahead` (`1c` §6.4, §6.6; `docs/data-model.md` §6.2). Added tests + 48h pruning check in `1c-T05`.
- **U-11** — Fixed. On successful re-pair after `auth_revoked`, mint fresh `uuidv4()` `client_mutation_id`s for every row in `outbox` and `replay_journal` in one Dexie `rw` transaction before resuming sync (`1c` §6.7), preventing false `mutation_id_conflict` rejection toasts. Added test in `1c-T06`.
- **U-12** — Fixed. Defined the `target_absent: true` enqueue path for archive-only search results (`1c` §5.1, §6.4, §8.1): Dexie `items_base`/`items` stay untouched (`P-17`), while the Search view computes `applyOutboxToItem(searchItem, outboxForItem)` reactively and rolls back / patches cleanly on `rejected` / `superseded`. Extended `1c-T08`.
- **U-13** — Fixed. Replaced two-pass `DOMParser` + `dangerouslySetInnerHTML` with a single-pass DOMPurify `RETURN_DOM_FRAGMENT: true` pipeline using `uponSanitizeElement` for `<img>` → placeholder replacement and `resolveSafeHttpUrl(href, baseUrl)` for relative links, mounted directly via `replaceChildren()` (`1c` §9.1–§9.2). Explicitly added `extracted_text` to the plain-text-only list (`1c` §9.2, §10.2). Added `B4` to `docs/review-checklist.md` and mXSS + relative-link fixtures to `1c-T02`.
- **U-14** — Fixed. Switched SW navigations to **cache-first** from precached `/index.html` (`1c` §7.1–§7.2) so user-gated `SKIP_WAITING` updates keep all tabs coherent with the active SW, and added `db.on('versionchange', ...)` closing Dexie handles cleanly from v1 (`1c` §5.2). Added `versionchange` test (`1c-T06`) and SW update E2E (`1c-T10`).
- **U-15** — Fixed. Expanded `1c` §12.2 (`1c-T01`–`1c-T15`) to cover every identified gap: timed 500-mutation `< 5 s` flush (`1c-T10`), non-localhost deployment topology & insecure Nginx `web` fallback (`1c-T15`), negative CSP `connect-src` pinning proof (`1c-T12`), scoped bulk-read overlay strictly using `captured_item_ids` (`1c-T03`), 48h journal pruning (`1c-T05`), SW update flow (`1c-T10`), runner `[::1]` `308` / `HEAD` / `%2e%2e%2f` / `%252e%252e%252f` / `/sw.js` origin-tag (`1c-T01`), and all `U-01`/`U-02`/`U-09`/`U-11` tests.
- **U-16** — Fixed. Changed the fixed Phase 1 localhost runner port from `4173` (Vite preview default) to **`4180`** (`http://localhost:4180`) throughout `1c` (and `docs/design/README.md`), pinning `vite.config.ts` `server.port = 5173` and `preview.port = 4173` so dev/preview never shares an origin with production.
- **U-17** — Fixed. Added `no-cors` follow-up probe in `src/sync/probe.ts` (`1c` §6.1, §6.2, §10.1): a CORS `TypeError` followed by a resolving `no-cors` fetch transitions to distinct `misconfigured` state with actionable `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS` guidance. Added test in `1c-T06`.
- **U-18** — Fixed (per Owner Decision `Q-4`). Specified `scripts/check-prereqs.sh --mode <dev|server|client|e2e>` and idempotent `scripts/install-client.sh` / `make install-client` (`1c` §3.2, §4.3) supporting local build, `--from-dist`, `--from-docker`, atomic `dist.prev` rollback (`--rollback`), systemd `WantedBy=default.target` enable/restart, and `README.md` update docs.

### Nit
- **U-19** — Fixed. Corrected CSP `style-src` rationale (`1c` §1c-D03, §4.2) to accurately distinguish parsed `style="..."` attributes / `setAttribute('style', ...)` (blocked by `style-src 'self'`) from CSSOM writes (not blocked), keeping ESLint's `style={...}` ban as a style rule with an explicit audited carve-out for a virtualized list if needed.
- **U-20** — Fixed. Added `effective_date` and compound `effective_date` indexes to the `1c` §5.1 Dexie `items` table, removed unmeasured `< 5 ms` / `< 50 ms` MiniSearch claims in favor of a 5,000-item benchmark in `1c-T07`, and explicitly documented non-stemming behavior vs PostgreSQL `english` FTS in §8.2.
- **U-21** — Fixed. (a) Removed the impossible *"ordered before newer pending outbox mutations"* clause in §6.6 step 2 (journaled mutations commute under LWW); (b) updated Sign Out (§9.3, §10.5) to issue best-effort `POST /api/v1/devices/{self}/revoke` online before clearing local Dexie state; (c) required `EditFeedModal` (§10.3, `1c-T14`) to send diff-only `PATCH` payloads (`D3`).

### Verification (`make check`)
- `make check` passed cleanly on `milestone/1c`: `167 passed`, total backend coverage `91.14%`, all 16 critical core modules `>= 90%` (lowest: `src/smartrss/security/middleware.py` at `90.80%`). *(Design-only diff; `1c` frontend/runner implementation begins only after owner design approval.)*
