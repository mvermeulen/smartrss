# Code Review — Milestone `1c` Implementation

- **Date**: 2026-11-25
- **Scope**: Committed branch `milestone/1c`, `main` (`c1a56f0`) → `7566002` (`feat(1c): implement local-first Ubuntu reader UI, offline shell, runner & E2E suite`). Covers `client-runner/`, `frontend/` (sync engine, outbox, reconciliation, lock, probe, Dexie schema, API client and normalizer, sanitizer, URL safety, Service Worker, search, UI components), `deploy/` (Nginx, systemd, desktop), `scripts/` (`install-client.sh`, `check-prereqs.sh`, `check-frontend-coverage.mjs`), `Makefile`, `docker-compose.yml`, the CORS default in `config.py`, the E2E suite, and doc updates. Checked against [`docs/design/1c-client-ui-and-offline-shell.md`](../design/1c-client-ui-and-offline-shell.md) (Approved), `data-model.md` §6.2, ADRs `0002`/`0004`/`0005`/`0006`, and [`review-checklist.md`](../review-checklist.md).
- **Reviewer**: Claude Code (at the project owner's request)
- **Finding prefix**: `V-`
- **Status**: `Addressed (2026-11-25) — awaiting verification`

## Review outcome

The milestone is broad and mostly well built. The hard invariants from the design review hold:

- **Server-base + overlay (`U-01`, checklist `B1`).** Every display write goes through the pure `applyOutboxToItem` / `applyOutboxToFeed` functions over `items_base` / `feeds_base` plus the current outbox: on enqueue, pull, push outcome, quarantine, retry, and bootstrap commit. There is no snapshot rollback anywhere. The `fast-check` property test covers it.
- **Boolean index (`B2`).** `starred_num` is numeric and used in all index queries.
- **Insecure-origin APIs (`B3`).** `uuidv4()` falls back to `getRandomValues`, and the lease lock falls back to a fenced IndexedDB lease. Service Worker registration is skipped on insecure origins. `1c-T15(a)` proves this end to end on `http://smartrss.test`.
- **Single-parse sanitization (`B4`).** DOMPurify runs once with `RETURN_DOM_FRAGMENT`, and `SafeHtml` mounts the result with `replaceChildren()`. There is no `dangerouslySetInnerHTML` (ESLint enforces this), and no JSX `style` prop.
- **Fencing (`U-05-F1`).** Every sync-cycle Dexie write I traced (bootstrap stage/commit/activate, pull apply, push outcome, quarantine, restore rewrite, eviction) calls `assertAndRenewInTx()` inside the same transaction.
- **Push.** Skew-safe bisection (`U-04-F1`) matches the design, including the 4-row probe for "poison pills in both halves". The FIFO packer honours all three caps. `sync_cursor` is never touched by push (`P-03`).
- **Bootstrap and restore.** Bootstrap is two-phase, resumable, and pinned to the page-1 watermark. Restore recovery runs on both `epoch_mismatch` and `cursor_ahead`, with the chunked rewrite and journal promotion (`U-10`/`Q-3`). Promotion is idempotent if bootstrap fails part-way.
- **Runner.** The localhost runner is tight: Host allow-list, `308` canonicalization, origin validation against CSP injection, layered traversal checks plus `resolve()`/`is_relative_to`, and an extension-aware SPA fallback. Nginx `include`s `security-headers.conf` in every `location`.

`make check` is green and the Playwright suite passes 8/8 against the real stack (details in §5).

The findings are:

- **one Major**, confirmed by reproduction: any non-API exception in a sync cycle (a non-JSON `200` from push, a Dexie error) is rethrown out of `triggerSync()`. The engine is left in `syncing`, and on the startup path the 60 s poll timer is never installed (`V-01`).
- a `flushOutbox` loop with no progress guard, which spins forever if a `200` response omits a result (`V-02`, reproduced);
- an IndexedDB lease lock that lets the **same tab** start a second, overlapping cycle (`V-03`, reproduced);
- an unfenced read-modify-write of `client_meta['sync']` (`V-04`);
- request timeouts that do not cover the response body (`V-05`);
- a Service Worker install that "succeeds" with missing precache entries (`V-06`);
- test-matrix and coverage gaps (`V-07`);
- design/doc drift (`V-08`);
- a broken `install-client.sh --from-docker` path (`V-09`);
- a batch of nits (`V-10`).

**Recommendation:** fix `V-01` with a regression test before `Implementation Approved`. `V-02`–`V-07` are small, self-contained fixes that should go in the same pass. `V-08`/`V-09` are doc and script fixes. `V-10` is optional.

| Severity | Findings |
| :--- | :--- |
| Blocker | — |
| Major | `V-01` |
| Minor | `V-02`, `V-03`, `V-04`, `V-05`, `V-06`, `V-07`, `V-08`, `V-09` |
| Nit | `V-10` |

---

## 1. Major

### V-01 — An unexpected exception in a sync cycle wedges the engine in `syncing` and, at startup, prevents the poll timer from ever being installed (checklist `L1`)

**Where:**

- `frontend/src/sync/outbox.ts:738`: `sendRows` rethrows anything that is not `ApiNetworkError` / `ApiHttpError`.
- `frontend/src/sync/engine.ts:536-541`: `tryRunLockedCycle` rethrows anything that is not `SyncLockLostError`.
- `engine.ts:240-253`: `start()` awaits `initializeRuntimeAndAuth()` → `triggerSync()` *before* `setInterval`.
- `frontend/src/App.tsx:183`: `void engine.start()`.

**What happens:** `executeSingleCycle` sets `connectionState = "syncing"` (`engine.ts:594`) and then calls `flushOutbox`. Many failures are not wrapped as `ApiNetworkError` / `ApiHttpError`:

- `response.json()` throwing `SyntaxError` on a `200` with a non-JSON body (captive portal, misconfigured reverse proxy, truncated body);
- `toCanonicalIso()` throwing on an unparseable timestamp;
- an `AbortError` during the body read (`V-05`);
- any Dexie error inside `applyPushSuccessBatch`, `prepareOutboxForRestore` or `evictStaleLocalItems` (`QuotaExceededError`, `ConstraintError`, an `InvalidStateError` after `versionchange` closes the DB).

These propagate out of `triggerSync()`. The pull loop and bootstrap catch everything, but the push, restore-prep, prune and eviction paths do not.

**Consequences:**

1. `connectionState` stays `"syncing"` forever. The top bar shows "Syncing…", and online-only controls stay enabled even if the server is gone.
2. The poll timer only fires in `online` / `detached` / `sync_error` (`engine.ts:246-250`), so it never retries from `syncing`.
3. When this happens during the **startup** cycle, `start()` rejects before `setInterval` runs. The tab has no poll timer for the rest of the session. Every caller uses `void`, so the result is an unhandled promise rejection.

Recovery needs a user action (manual Sync, a visibility change after 15 s, or an `online` event), and even then it only works if the cause has gone away.

**Reproduction (verified):** a temporary vitest case with a seeded auth/cursor, one outbox row, and `/api/v1/sync/push` returning `200` `"<html>proxy</html>"`:

```text
R1 startErr= SyntaxError: Unexpected token '<', "<html>proxy</html>" is not valid JSON
   state= syncing  pollTimer= null
```

**Fix:**

- In `executeSingleCycle` (or `tryRunLockedCycle`), catch every non-`SyncLockLostError` exception. Log it and move to `sync_error` with the message, so the existing backoff/poll path retries.
- Install the poll timer and listeners in `start()` *before* the first `triggerSync()`, or wrap that call in `try/finally`.
- In `requestJson`, map body-parse failures to a typed error (`ApiProtocolError`, or `ApiHttpError(502)`) so `flushOutbox` records `last_error` / `attempts` instead of throwing.

**Regression tests:**

- the push variant above: assert `connectionState === "sync_error"`, the outbox intact, and the poll timer installed;
- a Dexie-throws variant (for example, stub `db.replay_journal.put` to reject inside `applyPushSuccessBatch`);
- `start()` resolves and installs the poll timer even when the first cycle throws.

---

## 2. Minor

### V-02 — `flushOutbox` has no progress guard: a `200` response that omits a mutation's result loops forever while holding the sync lock (checklist `L1`/`L6`)

**Where:** `frontend/src/sync/outbox.ts:875-885` together with `applyPushSuccessBatch` (`:441-452`, which deletes only the rows that have a matching result).

**What happens:** if the server (or a proxy, or a future server version) returns `200` with `results` that miss any `client_mutation_id` in the batch, that row stays in the outbox. The `while (true)` loop then re-packs and re-sends the same batch immediately, with no backoff. Under Web Locks this holds `smartrss-sync-cycle` indefinitely, so every other tab's blocking waiter is starved too. The `1b` contract always returns one result per mutation, so this is defensive, but the failure mode is an unbounded request storm against the LAN server.

**Reproduction (verified):** push returns `{"results": []}`. The engine made 51 push calls in about 50 ms, until the test's escape hatch returned `401`.

**Fix:** after each successful batch, verify that every row in the batch got a result. If none of the rows made progress, record `last_error` on the missing rows and return `sync_error` ("Server response omitted N results"). Also cap the outer loop (for example `ceil(initialCount / MAX_BATCH_MUTATIONS) + MAX_QUARANTINES_PER_CYCLE + 1` iterations).

**Regression test:** the reproduction above, asserting the outcome is `sync_error` after a bounded number of requests.

### V-03 — The IndexedDB lease lock lets the same tab start a second overlapping cycle, aborting the first and re-sending its batch (checklist `C2`, `U-05`)

**Where:** `frontend/src/sync/lock.ts:113`: `if (existing && existing.expires_at > currentNow && existing.owner_tab_id !== tabId) → not acquired`. The engine passes a stable `this.tabId` (`engine.ts:103`, `:515`), and `triggerSync()` has no in-tab `running` guard.

**What happens:** on the LAN fallback, where Web Locks are absent, a second `triggerSync()` in the **same** tab finds the lease owned by itself, re-acquires it, and bumps `fencing_token`. Triggers include the 250 ms debounce after each enqueue, the poll timer, `online`, `visibilitychange` and manual Sync.

- The in-flight cycle's next fenced transaction throws `SyncLockLostError`. That cycle returns silently and discards the push response it already received. The new cycle re-pushes the same rows.
- The first cycle's `finally` sets `this.running = false` while the second cycle is still running.
- Server idempotency keeps data correct, but the design's "one holder" invariant does not hold inside a tab.
- A user who keeps acting more often than one push-plus-pull round trip keeps superseding cycles. This is a livelock risk on a slow LAN.

**Reproduction (verified):** `forceIdbLockFallback: true`, one outbox row, push delayed by 50 ms, and a second `triggerSync()` 20 ms after the first. Result: **2 push requests** for one mutation.

The Web Locks path correctly refuses, because `ifAvailable` sees the lock held by the same tab.

**Fix:** treat a lease held by the same `tabId` as *busy* (drop the `owner_tab_id !== tabId` clause, which a reloaded tab never needs because it mints a new `tabId`). Also add an in-tab `if (this.running) { this.dirty = true; return; }` guard in `triggerSync()`, so that the drain loop picks up the work.

**Regression test:** the reproduction above, asserting exactly one push. Add a lock-level test that a second `withSyncLock` call with the same `tabId` returns `{ acquired: false }` while the first is held.

### V-04 — `persistClockDrift` does an unfenced, non-transactional read-modify-write of `client_meta['sync']` (`U-05-F1`)

**Where:** `frontend/src/sync/engine.ts:600` (`void this.persistClockDrift(...)` from `onServerNow`) and `:679-686`.

**What happens:** this is the one sync-cycle write to `client_meta['sync']` that is neither inside a transaction nor fenced. It reads the meta, then writes `{...syncMeta, last_server_now, clock_drift_ms}` back in a separate request. It is fire-and-forget, so it races any later fenced write that is already in progress. If the push-to-pull hand-off is fast, or another tab takes over the lease, a stale copy of `sync_cursor` / `instance_epoch` / `active_gen` can be written back. The design (§6.2) requires every write during a sync cycle to be fenced.

**Fix:** perform it in a `rw` transaction on `client_meta` that calls `lock.assertAndRenewInTx()`, and `await` it. Alternatively, keep drift in memory and persist it in the pull loop's existing `setSyncMeta` (which already writes `clock_drift_ms`).

**Regression test:** a lease-stolen variant of `1c-T06(d)` in which the push callback must not alter `client_meta['sync']`.

*(Found by code reading.)*

### V-05 — Request timeouts stop at response headers; body reads are unbounded and their `AbortError` is not mapped (checklist `L6`)

**Where:** `frontend/src/api/client.ts:191-211` (timer cleared in `finally` right after `fetch()` resolves) and `:217-242` (`response.json()` / `.text()` run with no timeout).

**What happens:** a server or proxy that sends headers and then stalls the body keeps `requestJson` waiting forever. That holds the Web Lock and blocks every tab. The same pattern appears in `fetchClientRuntimeConfig` and `probe.ts`'s `timedFetch`. If the timer is moved to cover the body, the resulting `DOMException("AbortError")` must also be mapped to `ApiNetworkError`; otherwise it lands in `V-01`.

**Fix:** clear the timer only after the body is consumed, and wrap the body read in the same `try`/`catch` that maps `AbortError` to `ApiNetworkError(isTimeout=true)`.

**Regression test:** a `fetch` stub whose `Response` body is a `ReadableStream` that never closes. Assert `ApiNetworkError` with `isTimeout === true` within the timeout.

*(Found by code reading.)*

### V-06 — Service Worker install silently skips failed precache entries, so offline reload can fail after an "installed" update

**Where:** `frontend/src/sw/sw.ts:25-31`: `if (res.ok) await cache.put(url, res)`. Non-OK responses are skipped, not failed.

**What happens:** suppose the runner briefly serves a `404` for an asset during `install-client.sh`'s `dist` swap, or for `/index.html`. Install still completes, and after activation the old cache is deleted (`:43-47`). The new cache has no `/index.html` or no main bundle, so the next **offline** navigation falls through to `fetch(req)` and fails. This breaks the core guarantee of ADR 0005 and `1c-T10(a)`. Use `cache.addAll()` semantics instead, which fail the install if any entry is missing.

**Fix:** throw on `!res.ok` so that `event.waitUntil` rejects and the browser keeps the previous worker and cache.

**Regression test:** a unit test of the install handler with a stubbed `caches` / `fetch` returning `404` for one manifest URL, asserting that the `waitUntil` promise rejects. Alternatively, an E2E check that removes an asset from `dist` before serving an updated `/sw.js` and asserts that no update banner appears.

*(Found by code reading.)*

### V-07 — Test-matrix and coverage gaps (checklist `T2`/`T3`)

1. **`1c-T04(b)` 1.5 MiB cap is not tested.** The row requires a "> 1.5 MiB payload" case. `outbox.test.ts:118-175` covers only the 500-mutation and 5,000-ID caps. `packOutboxBatch`'s byte-cap `break` (`outbox.ts:86-89`) is never exercised by a large payload. The row's "canonical UTC ISO & lowercase `uuidv4()`" assertion is also missing (the IDs are hard-coded fixtures).
2. **Runner shutdown (`L2`) is not tested.** `smartrss_client_runner.py:427-444` (signal handler, `serve_forever`, `server_close`) is the whole uncovered block in the runner report. The design lists graceful `SIGTERM` handling as a requirement. Add a subprocess test: start the runner, send `SIGTERM`, assert exit `0` within 2 s and that the port is released.
3. **The `U-07` boundary is weakly covered and not gated.** `api/normalize.ts` is at **63 %** lines (`api/client.ts` 59 %). `normalize.ts` is where the `D6` canonical-timestamp invariant lives (`U-07`), yet it is in neither the 90 % nor the 80 % tier. Add it to the 80 % tier with a table-driven test over every `normalize*` function, or justify the omission in §12.1.
4. **The frontend "overall" figure only covers core modules.** `vitest.config.ts` restricts `coverage.include` to `security|db|api|sync|search`. `App.tsx`, `components/**`, `hooks/**` and `sw/**` (about 4,500 LOC) are outside the report, so "90.35 % All files" is not an overall frontend number. This is acceptable if the owner agrees that E2E covers the UI, but say so in §12.1 / `development-process.md` §2.2, and do not quote it as overall coverage.
5. **The `1c-T10(b)` 500-mutation E2E bypasses the enqueue path.** It writes raw IndexedDB rows that lack `entity_key`, `target_absent` and `captured_item_ids`, and it asserts only that the outbox reaches 0. It never checks that 500 mutations reached PostgreSQL. Assert the final server state (for example, `is_starred` of the targeted items matches the last write per item).

### V-08 — Implementation deviations not written back to the approved design (checklist `T4`)

- **§3.1 layout** lists modules that do not exist:
  - `hooks/useSyncEngine.ts`, `useReaderItems.ts`, `useSearch.ts`;
  - `layout/AppShell`;
  - `auth/AuthRevokedBanner`, `auth/OriginChangeBanner` (now part of `AlertStrip`);
  - `reader/ItemRow`, `BulkReadMenu`, `SmartMetadataCard`;
  - `feeds/AddFeedModal`, `EditFeedModal`, `ConflictDiffDialog`, `CategoryManager`, `OpmlPanel`;
  - `health/*Table`, `settings/AppSettingsForm`, `DevicesPanel`, `OutboxInspector`;
  - `common/ToastStack`;
  - `public/icons/icon-192.png`, `icon-512.png`. Only `icon.svg` ships, and the manifest references only that.
- **Lease TTL:** `lock.ts:15` uses `LEASE_TTL_MS = 20_000` ("> 15s push timeout, review §10 note 2"). §5.1 / §6.2 still say `15,000 ms`. Bootstrap requests use a 20 s timeout (`client.ts:296`), equal to the TTL, so the "TTL > longest request" reasoning should be written down too.
- **Makefile target name:** §1 lists a `frontend-e2e` target; the Makefile has `test-e2e`.
- **`runner-test` flags:** the actual `--cov=smartrss_client_runner` with `PYTHONPATH` differs from §3.2's `--cov=../client-runner/smartrss_client_runner`.

**Fix:** update §3.1 / §5.1 / §6.2 / §1 / §3.2 in the same commit as the code fixes.

### V-09 — `install-client.sh --from-docker` cannot work outside a checkout directory named `smartrss`

**Where:** `scripts/install-client.sh:86-88`.

**What happens:** `docker create smartrss-web:latest` assumes Compose's default image name `<project>-web`, where `<project>` is the checkout directory name. When the repo is cloned elsewhere (for example `~/src/smartrss-main`), that fails. The fallback, `docker compose create web | tail -n1`, prints status lines to stderr and nothing useful on stdout, so `CID` is empty or garbage and `docker cp` fails. It also creates the `db` / `api` containers through `depends_on`.

**Fix:** use `docker compose -f … create --no-deps web`, then resolve the container with `docker compose -f … ps -a -q web`. Alternatively, `docker compose build web` with an explicit `image:` name in `docker-compose.yml`.

**Regression test:** shellcheck plus a dry-run test that stubs `docker` on `PATH` and asserts the commands issued.

*(Found by code reading.)*

---

## 3. Nit

### V-10 — Small cleanups

- **Dead branch.** `client-runner/smartrss_client_runner.py:192-199`: `cache_control_for_path`'s `if` branch and the fall-through both return `"no-cache"`.
- **Class spoofing.** `sanitizeHtml.ts:195-199`: feed HTML may keep `class="srss-blocked-img"` and spoof the blocked-image styling. Harmless, but only the hook should add that class. Strip all `class` attributes first and add it inside `buildBlockedImagePlaceholder`, or tag hook-created nodes with a `WeakSet`.
- **Quadratic packing.** `outbox.ts:86-87`: `packOutboxBatch` re-serializes the whole candidate array for every row, which is O(n²) bytes (about 25 MB of `JSON.stringify` for 500 rows). Keep a running byte total: each payload's length plus a comma, plus the wrapper.
- **Cache keying.** `sw.ts:19`: `CACHE_NAME` includes `self.location.href` (the script URL), not the origin tag that §7.2 specifies. An origin-tag-only update therefore reuses and overwrites the *active* worker's cache in place before the user confirms. It is benign today, because the assets are identical, but it differs from the documented model.
- **Search re-fires.** `App.tsx:339-349`: the connected-search effect depends on `items`, so every pull or enqueue re-fires `GET /api/v1/search`.
- **Clocks.** `engine.ts:144`/`:543` use `Date.now()` rather than the injected `this.now`.
- **Origin confirm across tabs.** `engine.ts:352-377`: `confirmOriginChange()` does not broadcast `auth_changed`, so other open tabs stay on the origin-confirm banner until they reload.
- **`resynced` path.** `engine.ts:641-645` skips `pruneReplayJournal` and eviction for that cycle.

---

## 4. Checklist walk (diff only)

| # | Result |
| :--- | :--- |
| S1 | OK. The runner only `read_bytes()` resolved files. The frontend passes no fetched data to path- or URL-accepting APIs. DOMPurify gets a string. |
| S2 | N/A — no new server-side outbound fetches. |
| S3 | OK. The bearer token is only in Dexie and the `Authorization` header. The runner's `log_message` logs only the method and path. No secrets on argv in the scripts. |
| S4 | OK. `resolveSafeHttpUrl` is used for every rendered link I traced (reading pane title, feed site URLs, sanitizer `<a>` and image placeholders). |
| S5 | OK. The sanitizer slices to 64 K chars. The runner layers decode and segment checks plus `resolve()` / `is_relative_to`. |
| S6 | OK in code (`TextEncoder` byte cap). Byte-cap test missing → `V-07`. |
| C1 | OK. Push and pull outcomes are applied against freshly read `*_base` and outbox inside the fenced transaction. |
| C2 | `V-03` (same-tab lease re-entry). |
| C3 | OK. Fenced lease (`owner_tab_id`, `fencing_token`) is checked in every cycle transaction, except `V-04`. |
| C5 | OK. Drift is measured from `server_now`. Outgoing timestamps are canonical UTC. |
| C6–C9 | N/A (server-side classes; no backend logic changes besides the CORS default). |
| L1 | `V-01`, `V-02`. |
| L2 | OK in code (runner `SIGTERM` / `SIGINT`). Untested → `V-07`. |
| L3 | OK. Bootstrap commits in 1,000-row chunks. `applyOutboxToItem` re-sorts the outbox per item; that is fine at the design envelope. `V-10` (packer). |
| L4 | OK. MiniSearch is pruned on evict and delete. Journal pruned at 48 h. |
| L6 | `V-05`. |
| D1–D6 | N/A server-side. D6 (client): `toCanonicalIso` / lowercase UUIDs at the boundary. Weak coverage → `V-07`. |
| E2 | OK. Rejections, quarantines, restore drops and misconfiguration all surface as notifications or banners. |
| T1 | N/A (no fixes yet). |
| T2 | `V-07` (T04(b) byte cap, T10(b) server assertion). Other rows are mapped in §6. |
| T3 | OK. `check-frontend-coverage.mjs` enforces the 6 + 4 modules from §12.1; runner `--cov-fail-under=90`. `normalize.ts` is ungated → `V-07`. |
| T4 | `V-08`. |
| T5 | OK. `npm ci` from the committed `package-lock.json` in `Dockerfile`, `install-client.sh` and the Makefile. |
| T6 | `1c-T06(c)` / `(d)` force their interleavings with promises and fake clocks, and would fail if the invariant broke. No same-tab case → `V-03`. |
| B1 | OK (see outcome). |
| B2 | OK. `starred_num` and `[starred_num+effective_date]`; `dismissed` stored as `0 | 1`. |
| B3 | OK. `uuid.ts`, `lock.ts` fallback, `register.ts` secure-context guard, `navigator.storage?.persist` guarded. |
| B4 | OK. One DOMPurify pass, `RETURN_DOM_FRAGMENT`, `replaceChildren()`. |

## 5. Verification performed

**`make check`** on `7566002`: green.

```text
backend:  167 passed in 19.48s — Total coverage: 91.18% — all critical core modules >= 90%
runner:    28 passed in 0.78s  — smartrss_client_runner.py 90.42% (missing 76,120,122,146,158,162,167-168,184,199,228,427-444)
frontend: eslint + prettier OK, tsc --noEmit OK, vitest 41 passed (5 files)
  [OK] uuid.ts 100/100  urlSafety.ts 96.82/94.44  sanitizeHtml.ts 98.00/90.00
  [OK] reconciliation.ts 97.36/91.11  outbox.ts 96.02/90.41  engine.ts 96.86/90.51
  [OK] schema.ts 91.76/100  clientMeta.ts 100/86.11  queryParser.ts 94.02/84.00  localSearch.ts 97.02/89.47
  (ungated: api/client.ts 59.42 %, api/normalize.ts 63.29 %, sync/probe.ts 84.33 % lines / 62.5 % branches)
All frontend critical (>= 90%) and core (>= 80%) coverage gates passed.
```

**`make test-e2e`** (not part of `make check`): green.

```text
Running 8 tests using 1 worker
  ✓ 1c-T14  ✓ 1c-T15(a)  ✓ 1c-T15(b)&(c)  ✓ 1c-T09  ✓ 1c-T10  ✓ 1c-T11  ✓ 1c-T12  ✓ 1c-T13
  8 passed (28.7s)
```

**Reproductions.** These used a temporary `frontend/src/test/unit/zz_review_repro.test.ts`, which was deleted afterwards (`git status` clean).

| Finding | Method | Result |
| :--- | :--- | :--- |
| `V-01` | Seeded auth/cursor and one outbox row; push returns `200` `"<html>proxy</html>"`; `await engine.start()` | `start()` rejects with `SyntaxError`; `connectionState === "syncing"`; `pollTimer === null` — **reproduced** |
| `V-01` (control) | Same, but pull returns non-JSON `200` | Correctly lands in `sync_error` with the poll timer installed. Only the push, restore-prep and evict paths are affected. |
| `V-02` | Push returns `200` with `results: []` | 51 push requests in about 50 ms until the test's `401` escape hatch — **reproduced** |
| `V-03` | `forceIdbLockFallback: true`, push delayed by 50 ms, second `triggerSync()` after 20 ms | 2 push requests for 1 mutation (first cycle aborted by its own tab) — **reproduced** |

`V-04`, `V-05`, `V-06` and `V-09` were found by code reading.

## 6. Test-matrix mapping (§12.2)

| Row | Test | Notes |
| :--- | :--- | :--- |
| `1c-T01` | `client-runner/tests/test_client_runner.py` (28 tests: host allow-list / `308` incl. `[::1]`, origin validation, CSP/cache headers, origin tag, traversal incl. `%252e` and symlink, SPA fallback, CLI) | OK; shutdown untested → `V-07` |
| `1c-T02` | `security.test.ts` › `sanitizeFeedHtmlToFragment …` (4 tests) + `urlSafety` | OK |
| `1c-T03` | `reconciliation_and_db.test.ts` (a)–(f) incl. `fast-check` property | OK |
| `1c-T04` | `outbox.test.ts` (a), (b), (c-i)…(c-iv), (d), 401/503, scoped/413/cap | 1.5 MiB cap and canonical-ID assertions missing → `V-07` |
| `1c-T05` | `engine_bootstrap_and_restore.test.ts` › `1c-T05 (a)`, `(b)`, plus the epoch-flip and committing-resume coverage tests | OK |
| `1c-T06` | `1c-T06 (a)`–`(e)`; `(f)` in `reconciliation_and_db.test.ts` (`attachVersionChangeHandler`, `isClockDriftExcessive`) | OK; no same-tab lease case → `V-03` |
| `1c-T07` | `search.test.ts` (parser, ranking/facets/incremental, 5,000-item benchmark) | OK |
| `1c-T08` | `search.test.ts` › `1c-T08 …` | OK |
| `1c-T09` | `reader_and_offline.spec.ts` › `1c-T09` | OK |
| `1c-T10` | `reader_and_offline.spec.ts` › `1c-T10` (a), (b), (c) | (b) bypasses enqueue and has no server assertion → `V-07` |
| `1c-T11` | `reader_and_offline.spec.ts` › `1c-T11` | OK |
| `1c-T12` | `security_and_health.spec.ts` › `1c-T12` | OK |
| `1c-T13` | `security_and_health.spec.ts` › `1c-T13` | OK |
| `1c-T14` | `feeds_devices_and_topology.spec.ts` › `1c-T14` | OK |
| `1c-T15` | `feeds_devices_and_topology.spec.ts` › `1c-T15 (a)`, `(b) & (c)` | OK |

## 7. Resolution

All findings (`V-01`–`V-10`, including all 8 sub-items of `V-10`) have been fixed on `milestone/1c` with proven regression tests and synchronized documentation.

- **V-01 (Major)** — Fixed across all three paths:
  1. `frontend/src/api/client.ts:242-255`: mapped invalid JSON/XML body parse failures on `200` responses to `ApiHttpError(502, "Invalid JSON response from server: ...")` so `flushOutbox` records `attempts`/`last_error` cleanly instead of throwing `SyntaxError`.
  2. `frontend/src/sync/engine.ts:552-561`: `tryRunLockedCycle` now catches every non-`SyncLockLostError` exception (Dexie quota/state errors, parser errors, etc.) and transitions `connectionState` to `"sync_error"` with `statusDetail` instead of rethrowing and wedging in `"syncing"`.
  3. `frontend/src/sync/engine.ts:238-262`: `start()` now installs the `60,000 ms` poll timer **before** `initializeRuntimeAndAuth()` and wraps `initializeRuntimeAndAuth()` in `try/catch` → `sync_error`, so startup failures never reject or skip timer installation.
  - **Tests**: `test_v01_push_non_json_200_lands_in_sync_error_and_installs_poll_timer`, `test_v01_dexie_throw_inside_apply_push_lands_in_sync_error_not_wedged`, `test_v01_startup_unexpected_exception_still_installs_poll_timer`, `test_v01_client_maps_invalid_json_200_to_api_http_error_502`, and non-Error throw cases in `test_v02_omitted_results_during_bisection_and_non_error_sync_exception` (`frontend/src/test/unit/review_regressions.test.ts`).
  - **Evidence**:
    - Reverted `client.ts` `ApiHttpError(502)` mapping → `test_v01_client_maps_invalid_json_200_to_api_http_error_502` failed (`expected SyntaxError ... to be an instance of ApiHttpError`); restored → passed.
    - Reverted `engine.ts` `tryRunLockedCycle` catch-all to `throw err` → `test_v01_dexie_throw_inside_apply_push_lands_in_sync_error_not_wedged` failed (`promise rejected "DexieError{ ... }" instead of resolving`); restored → passed.
    - Reverted `engine.ts` `start()` `try/catch` around `initializeRuntimeAndAuth()` → `test_v01_startup_unexpected_exception_still_installs_poll_timer` failed (`promise rejected "Error: Unexpected IDB open failure" instead of resolving`); restored → passed.
    - Fixed lines covered (`engine.ts` 96.42% lines / 90.67% branches; `client.ts` body-parse catch covered).

- **V-02 (Minor)** — Fixed (`frontend/src/sync/outbox.ts:407-585`, `:800-930`). `applyPushSuccessBatch` now returns `{ processedCount, missingRows }`; all four `200` push handling paths (`main`, `left`, `right`, `probe`) route through `applyAndCheckResults`, which records `last_error`/`attempts` on omitted rows and immediately returns `sync_error` (`"Server push response omitted N mutation result(s)."`). The outer `flushOutbox` loop also enforces a bounded iteration cap (`maxIterations`) and a strict progress guard.
  - **Tests**: `test_v02_flush_outbox_omitted_results_halts_with_sync_error_and_bounded_requests`, `test_v02_omitted_results_during_bisection_and_non_error_sync_exception` (`frontend/src/test/unit/review_regressions.test.ts`).
  - **Evidence**: Reverted `applied.missingRows.length > 0` check in `applyAndCheckResults` → `test_v02_flush_outbox_omitted_results_halts_with_sync_error_and_bounded_requests` failed (`expected 'Sync push made no progress...' to contain 'omitted 2'`); restored → passed. Fixed lines covered (`outbox.ts` 95.66% lines / 90.44% branches).

- **V-03 (Minor)** — Fixed (`frontend/src/sync/lock.ts:112-116`, `frontend/src/sync/engine.ts:492-500`). Removed the `existing.owner_tab_id !== tabId` exemption in `withSyncLock` so any live lease (including same-tab) is treated as busy, and added an in-tab `if (this.running) { this.dirty = true; return; }` guard in `SyncEngine.triggerSync()` so overlapping triggers coalesce cleanly into the active drain loop.
  - **Test**: `test_v03_idb_lease_lock_blocks_same_tab_reentry_and_engine_coalesces_overlapping_triggers` (`frontend/src/test/unit/review_regressions.test.ts`).
  - **Evidence**: Reverted `lock.ts` to `&& existing.owner_tab_id !== tabId` → test failed (`innerAcquired: expected true to be false`); restored → passed (lock returns `{ acquired: false }` and overlapping same-tab `triggerSync()` issues exactly `1` push). Fixed lines covered (`lock.ts` 95.58% lines / 91.17% branches; `engine.ts` 96.42% / 90.67%).

- **V-04 (Minor)** — Fixed (`frontend/src/sync/outbox.ts:440-450`, `frontend/src/sync/engine.ts:618-625`, removed unfenced `persistClockDrift`). `last_server_now` and `clock_drift_ms` are now persisted atomically inside `applyPushSuccessBatch`'s existing fenced Dexie `rw` transaction right after `await lock.assertAndRenewInTx()`, eliminating the fire-and-forget unfenced read-modify-write.
  - **Test**: `test_v04_push_clock_drift_persist_is_fenced_and_aborts_on_stolen_lease` (`frontend/src/test/unit/review_regressions.test.ts`).
  - **Evidence**: Reverted to fire-and-forget unfenced `getSyncMeta`/`setSyncMeta` before the fenced tx → test failed (`expected '2026-11-25T13:30:00.000Z' to be '2026-11-25T12:00:00.000Z'`); restored → passed. Fixed lines covered.

- **V-05 (Minor)** — Fixed (`frontend/src/api/client.ts:191-262`, `frontend/src/sync/probe.ts:42-95`). Kept the `AbortController` timeout active across the entire response body read (`response.json()` / `response.text()`, including error responses) with a single outer `finally { clearTimeout(timer); }`, mapping `AbortError` during body reads to `ApiNetworkError(isTimeout=true)`. `probe.ts` cancels unread probe bodies cleanly.
  - **Test**: `test_v05_stalled_response_body_times_out_as_api_network_error` (`frontend/src/test/unit/review_regressions.test.ts`).
  - **Evidence**: Reverted `clearTimeout(timer)` to right after `fetchImpl(...)` → test failed (`Test timed out in 350ms`); restored → passed in ~43 ms with `ApiNetworkError.isTimeout === true`. Fixed lines covered.

- **V-06 (Minor)** — Fixed (`frontend/src/sw/sw.ts:24-38`). Extracted `precacheShellUrls()` and changed non-OK precache responses to throw `Error("Service Worker precache failed for <url>: HTTP <status>")` so `install`'s `event.waitUntil` rejects and the browser retains the previous active worker and cache.
  - **Test**: `test_v06_sw_precache_rejects_on_non_ok_response` (`frontend/src/test/unit/review_regressions.test.ts`).
  - **Evidence**: Reverted `if (!res.ok) throw ...` back to `if (res.ok) await cache.put(...)` → test failed (`promise resolved "undefined" instead of rejecting`); restored → passed.

- **V-07 (Minor)** — Fixed all 5 items:
  1. `1c-T04(b)` 1.5 MiB byte-cap & canonical UTC/UUID assertions added in `test_v07_1_5_mib_byte_cap_and_canonical_enqueue_ids_and_timestamps` (`frontend/src/test/unit/review_regressions.test.ts`).
  2. Runner graceful `SIGTERM` shutdown (`L2`) tested both via real subprocess (`test_v07_runner_sigterm_graceful_shutdown`, asserting exit `0` < 2 s and port release) and in-process `main()` coverage (`test_v07_runner_main_signal_shutdown_inprocess`) in `client-runner/tests/test_client_runner.py`. Reverted `signal.signal(signal.SIGTERM, _shutdown_handler)` → subprocess test failed; restored → passed. Runner coverage rose from `90.42%` to **`94.87%`** (lines `427-444` covered).
  3. Added `src/api/normalize.ts` to the `>= 80%` core coverage tier in `scripts/check-frontend-coverage.mjs` (and documented in `1c` §12.1 and `docs/development-process.md` §2.2), with comprehensive table-driven test `test_v07_normalize_table_driven_all_dtos` (`src/api/normalize.ts` now at **`99.32%` lines / `90.24%` branches**).
  4. Documented the Vitest v8 coverage scope (`src/{security,db,api,sync,search}/**/*.ts`) explicitly in `docs/design/1c-client-ui-and-offline-shell.md` §12.1 and `docs/development-process.md` §2.2.
  5. Updated `1c-T10(b)` in `frontend/src/test/e2e/reader_and_offline.spec.ts` (and `e2e_backend.py` `query-state`) to enqueue complete `OutboxRow` objects with monotonic timestamps and assert PostgreSQL `sync_mutation_count >= 500` and exact per-item `is_starred` final state.

- **V-08 (Minor)** — Fixed (`docs/design/1c-client-ui-and-offline-shell.md` §1, §3.1, §3.2, §6.2, §12.1; `frontend/src/sync/lock.ts:15`). Updated §3.1 module layout & `public/icons/icon.svg`, bumped `LEASE_TTL_MS` to `25_000` (`> 20s` bootstrap timeout and `> 15s` push/pull timeout) and documented it in §6.2, fixed `frontend-e2e` → `test-e2e` in §1, and fixed `runner-test` `PYTHONPATH=client-runner ... --cov=smartrss_client_runner` in §3.2.

- **V-09 (Minor)** — Fixed (`scripts/install-client.sh:85-97`). Replaced `docker create smartrss-web:latest` with `docker compose -f ... build web`, `docker compose -f ... create --no-deps web`, and `CID="$(docker compose -f ... ps -a -q web | head -n1)"` (with non-empty guard and placeholder `POSTGRES_PASSWORD` for laptops without a server `.env`).
  - **Test**: `test_v09_install_client_from_docker_outside_smartrss_dir` (`client-runner/tests/test_client_runner.py`, running from a checkout directory named `smartrss-custom-checkout` with stubbed `docker`/`systemctl`).
  - **Evidence**: Reverted `create --no-deps web` back to `docker create smartrss-web:latest` → test failed (`AssertionError: install-client.sh failed`); restored → passed. `bash -n` clean.

- **V-10 (Nit)** — Fixed all 8 items:
  1. Removed redundant dead `if` branch in `client-runner/smartrss_client_runner.py:187-194` (`cache_control_for_path`).
  2. Replaced `class !== "srss-blocked-img"` allow-check with a `WeakSet<Element>` (`trustedPlaceholders`) in `frontend/src/security/sanitizeHtml.ts:148-200`; proven by `test_v10_sanitize_blocks_spoofed_blocked_img_class_and_origin_confirm_broadcasts` (reverted check → failed `expected true to be false`; restored → passed).
  3. Replaced O(n²) re-serialization in `packOutboxBatch` (`frontend/src/sync/outbox.ts:55-99`) with an O(n) running UTF-8 byte counter; exact equivalence verified in `test_v07_1_5_mib_byte_cap_and_canonical_enqueue_ids_and_timestamps`.
  4. Prepended `self.__SMARTRSS_ORIGIN_TAG__="<tag>";` in `client-runner/smartrss_client_runner.py:341-345` and keyed `CACHE_NAME = "${CACHE_PREFIX}${BUILD_HASH}-${ORIGIN_TAG}"` in `frontend/src/sw/sw.ts:17-21`; verified in `test_v10_sw_origin_tag_sets_global_variable`.
  5. Split local vs. connected search `useEffect` in `frontend/src/App.tsx:262-355` so connected `GET /api/v1/search` no longer depends on `items`.
  6. Replaced `Date.now()` with `this.now()` in `frontend/src/sync/engine.ts:143` and `:562`.
  7. Broadcast `auth_changed` from `confirmOriginChange()` in `frontend/src/sync/engine.ts:374`; verified in `test_v10_sanitize_blocks_spoofed_blocked_img_class_and_origin_confirm_broadcasts`.
  8. Removed the early return on `pullRes === "resynced"` in `frontend/src/sync/engine.ts:655-670` so 48 h journal pruning and local eviction run after restore recovery as well.

### Verification Summary (`make check` + `make test-e2e`)
- **Backend (`make check`)**: `167 passed` (total `91%`, all 16 critical modules `>= 90%`; `ruff` + `mypy` clean).
- **Localhost Runner (`make runner-test`)**: `32 passed`, `smartrss_client_runner.py` **`94.87%`** branch+line coverage (`>= 90%` gate OK).
- **Frontend Core (`make frontend-lint`, `frontend-typecheck`, `frontend-test-cov`)**: `eslint` + `prettier` + `tsc --noEmit` clean; `53 passed` (6 test files); scoped core report (`src/{security,db,api,sync,search}`) `93.05%` lines:
  - Critical 6 (`>= 90%` lines & branches): `uuid.ts` `100/100`, `urlSafety.ts` `96.82/94.73`, `sanitizeHtml.ts` `98.01/90.00`, `reconciliation.ts` `97.36/91.30`, `outbox.ts` `95.66/90.44`, `engine.ts` `96.42/90.67`.
  - Core 5 (`>= 80%` lines & branches): `schema.ts` `91.76/100`, `clientMeta.ts` `100/86.11`, `normalize.ts` `99.32/90.24`, `queryParser.ts` `94.02/84.00`, `localSearch.ts` `97.02/89.47`.
- **Playwright E2E (`make test-e2e`)**: **`8/8 passed`** in `23.2s` (`1c-T09`–`1c-T15`, including PostgreSQL verification of 500-mutation flush in `1c-T10(b)`).
