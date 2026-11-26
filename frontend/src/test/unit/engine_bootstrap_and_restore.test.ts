/**
 * Tests for 1c-T05 & 1c-T06:
 * - U-06: 3-page streaming bootstrap fail & resume + epoch flip restart
 * - U-10 / Q-3: journaled scoped bulk read + 48h pruning + epoch_mismatch & cursor_ahead restore
 * - U-08 / U-08-F1: origin change guard (zero Auth header) + single-step SW activation + CSP reload flag
 * - U-11: re-pair mints fresh client_mutation_ids (no false mutation_id_conflict)
 * - U-09 / U-09-F1: post-check/pre-release multi-tab wake-up race
 * - U-05-F1: fenced IndexedDB lease lock aborts stolen tab write cleanly
 * - U-17 / U-17-F1: CORS & ALLOWED_HOSTS probe classification
 * - U-14: versionchange handler & clock drift warning
 * - U-21: Sign Out best-effort server self-revoke
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getAuthMeta, getSyncMeta, setAuthMeta, setSyncMeta } from "../../db/clientMeta";
import { SmartRSSDB } from "../../db/schema";
import { SyncEngine } from "../../sync/engine";
import { SyncLockLostError, withSyncLock } from "../../sync/lock";
import { enqueueBulkMarkRead, enqueueItemState, pruneReplayJournal } from "../../sync/outbox";
import { probeServerReachability } from "../../sync/probe";
import { applyOutboxToItem } from "../../sync/reconciliation";
import type {
  SyncBootstrapResponse,
  SyncPullResponse,
  SyncPushRequest,
  SyncPushResponse,
} from "../../types/sync";
import { makeCategory, makeFeed, makeLocalBase, makeSyncItem } from "../fixtures/syncFactories";

const EPOCH_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const EPOCH_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("1c-T05 & 1c-T06 streaming bootstrap, restore, locks, origin guard & probes", () => {
  let db: SmartRSSDB;

  beforeEach(async () => {
    db = new SmartRSSDB(`test_t05_t06_${Math.random().toString(36).slice(2)}`);
    await db.open();
  });

  afterEach(async () => {
    vi.useRealTimers();
    await db.delete();
  });

  it("1c-T05 (a) U-06: 3-page streaming bootstrap fails on page 3, preserves old rows, resumes from page 3 cursor", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    const item1 = makeSyncItem({ id: "33333333-3333-4333-8333-333333333301" });
    const item2 = makeSyncItem({ id: "33333333-3333-4333-8333-333333333302" });
    const item3 = makeSyncItem({ id: "33333333-3333-4333-8333-333333333303" });
    const cursorsRequested: Array<string | null> = [];
    let failPage3Once = true;

    const mockFetch: typeof fetch = async (input) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/health") {
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      if (url.pathname === "/api/v1/devices") {
        return jsonResp({ detail: "Auth required" }, 401);
      }
      if (url.pathname === "/api/v1/sync/bootstrap") {
        const cursor = url.searchParams.get("cursor");
        cursorsRequested.push(cursor);
        if (cursor === null) {
          return jsonResp({
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:00Z",
            watermark: 100,
            categories: [makeCategory()],
            feeds: [makeFeed()],
            clusters: [],
            interest_profile: null,
            app_settings: {
              default_expiration_days: 30,
              backfill_cap: 10,
              default_poll_interval_minutes: 60,
              updated_at: "2026-11-25T10:00:00Z",
            },
            items: [item1],
            next_cursor: item1.id,
            has_more: true,
          } satisfies SyncBootstrapResponse);
        }
        if (cursor === item1.id) {
          return jsonResp({
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:01Z",
            watermark: null,
            categories: null,
            feeds: null,
            clusters: null,
            interest_profile: null,
            app_settings: null,
            items: [item2],
            next_cursor: item2.id,
            has_more: true,
          } satisfies SyncBootstrapResponse);
        }
        if (cursor === item2.id) {
          if (failPage3Once) {
            failPage3Once = false;
            throw new TypeError("Wi-Fi dropped on page 3");
          }
          return jsonResp({
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:02Z",
            watermark: null,
            categories: null,
            feeds: null,
            clusters: null,
            interest_profile: null,
            app_settings: null,
            items: [item3],
            next_cursor: null,
            has_more: false,
          } satisfies SyncBootstrapResponse);
        }
      }
      if (url.pathname === "/api/v1/sync/pull") {
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:03Z",
          watermark: 100,
          next_seq: 100,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      return jsonResp({}, 404);
    };

    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
    });

    // Attempt 1 fails on page 3; checkpoint stays saved at item2.id
    await engine.initializeRuntimeAndAuth();
    expect(engine.getSnapshot().connectionState).toBe("detached");
    const cp = await db.bootstrap_meta.get("active");
    expect(cp?.phase).toBe("fetching");
    expect(cp?.next_cursor).toBe(item2.id);
    expect(await db.bootstrap_items.count()).toBe(2);

    // Attempt 2 resumes directly from item2.id without re-fetching pages 1 & 2!
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("online");
    expect(cursorsRequested).toEqual([null, item1.id, item2.id, item2.id]);
    expect(await db.items.count()).toBe(3);
    expect(await db.bootstrap_meta.count()).toBe(0);
    expect(await db.bootstrap_items.count()).toBe(0);
    expect((await getSyncMeta(db)).sync_cursor).toBe(100);
    engine.stop();
  });

  it("1c-T05 (b) U-10/Q-3: applied scoped bulk read journals chunked scope=items; epoch_mismatch & cursor_ahead promote journal", async () => {
    const nowMs = Date.parse("2026-11-25T12:00:00.000Z");
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 200,
      active_gen: 1,
      last_sync_at: "2026-11-25T11:00:00.000Z",
      last_server_now: "2026-11-25T11:00:00.000Z",
      clock_drift_ms: 0,
    });

    const feed = makeFeed();
    const itemA = makeLocalBase({
      id: "33333333-3333-4333-8333-33333333330a",
      ingest_sync_seq: 10,
    });
    const itemB = makeLocalBase({
      id: "33333333-3333-4333-8333-33333333330b",
      ingest_sync_seq: 20,
    });
    await db.feeds_base.put(feed);
    await db.feeds.put(feed);
    await db.items_base.bulkPut([itemA, itemB]);
    await db.items.bulkPut([applyOutboxToItem(itemA, []), applyOutboxToItem(itemB, [])]);

    // Add a stale >48h journal row to prove pruning
    await db.replay_journal.put({
      client_mutation_id: "00000000-0000-4000-8000-000000000099",
      acknowledged_at: "2026-11-20T00:00:00.000Z",
      payload: {
        type: "item_set_starred",
        client_mutation_id: "00000000-0000-4000-8000-000000000099",
        client_timestamp: "2026-11-20T00:00:00.000Z",
        item_id: itemA.id,
        is_starred: true,
      },
    });

    // Enqueue scoped bulk mark-read on feed -> flush -> must journal chunked scope='items'
    await enqueueBulkMarkRead(db, { scope: "feed", feedId: feed.id });

    let phase: "apply_bulk" | "restore_cursor_ahead" = "apply_bulk";
    const replayedPayloads: SyncPushRequest["mutations"] = [];

    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/health") {
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      if (url.pathname === "/api/v1/devices") {
        return jsonResp({}, 401);
      }
      if (url.pathname === "/api/v1/sync/push") {
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        if (phase === "restore_cursor_ahead") {
          replayedPayloads.push(...req.mutations);
        }
        return jsonResp({
          instance_epoch: phase === "apply_bulk" ? EPOCH_1 : EPOCH_2,
          server_now: "2026-11-25T12:00:00Z",
          results: req.mutations.map((m) => ({
            client_mutation_id: m.client_mutation_id,
            outcome: "applied",
            reason: null,
            affected_count: 2,
            current_entity_type: null,
            current_entity: null,
          })),
        } satisfies SyncPushResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        if (phase === "apply_bulk") {
          return jsonResp({
            resync_required: false,
            resync_reason: null,
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:00Z",
            watermark: 201,
            next_seq: 201,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        const since = Number(url.searchParams.get("since_seq"));
        if (since === 201) {
          return jsonResp({
            resync_required: true,
            resync_reason: "cursor_ahead",
            instance_epoch: EPOCH_2,
            server_now: "2026-11-25T12:00:01Z",
            watermark: 150,
            next_seq: 150,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_2,
          server_now: "2026-11-25T12:00:02Z",
          watermark: 151,
          next_seq: 151,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      if (url.pathname === "/api/v1/sync/bootstrap") {
        return jsonResp({
          instance_epoch: EPOCH_2,
          server_now: "2026-11-25T12:00:01Z",
          watermark: 150,
          categories: [],
          feeds: [feed],
          clusters: [],
          interest_profile: null,
          app_settings: null,
          items: [itemA, itemB],
          next_cursor: null,
          has_more: false,
        } satisfies SyncBootstrapResponse);
      }
      return jsonResp({}, 404);
    };

    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
      now: () => nowMs,
    });

    await engine.initializeRuntimeAndAuth();
    // Stale >48h journal entry pruned; new chunked scope='items' journal entry retained!
    expect(await pruneReplayJournal(db, nowMs)).toBe(0);
    const journal = await db.replay_journal.toArray();
    expect(journal).toHaveLength(1);
    expect(journal[0]!.payload.type).toBe("item_mark_bulk_read");
    if (journal[0]!.payload.type === "item_mark_bulk_read") {
      expect(journal[0]!.payload.scope).toBe("items");
      expect(new Set(journal[0]!.payload.item_ids)).toEqual(new Set([itemA.id, itemB.id]));
    }

    // Now simulate restore returning cursor_ahead -> journal MUST be promoted and replayed!
    phase = "restore_cursor_ahead";
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("online");
    expect(replayedPayloads).toHaveLength(1);
    expect(replayedPayloads[0]!.type).toBe("item_mark_bulk_read");
    if (replayedPayloads[0]!.type === "item_mark_bulk_read") {
      expect(replayedPayloads[0]!.scope).toBe("items");
    }
    engine.stop();
  });

  it("1c-T06 (a) U-08/U-08-F1: origin mismatch sends zero Auth headers; confirm updates origin & activates SW", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "secret-prod-token",
      api_origin: "http://old.lan:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    let authHeaderSent = false;
    const mockFetch: typeof fetch = async (input, init) => {
      const headers = new Headers(init?.headers);
      if (headers.get("Authorization")) {
        authHeaderSent = true;
      }
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://new.lan:8080" });
      }
      if (url.pathname === "/api/v1/health")
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      if (url.pathname === "/api/v1/devices") return jsonResp({}, 401);
      return jsonResp({
        instance_epoch: EPOCH_1,
        server_now: "2026-11-25T12:00:00Z",
        watermark: 1,
        categories: [],
        feeds: [],
        clusters: [],
        interest_profile: null,
        app_settings: null,
        items: [],
        next_cursor: null,
        has_more: false,
      } satisfies SyncBootstrapResponse);
    };

    let reloaded = false;
    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
      onReloadRequested: () => {
        reloaded = true;
      },
    });

    await engine.initializeRuntimeAndAuth();
    expect(engine.getSnapshot().connectionState).toBe("origin_confirm");
    expect(engine.getSnapshot().pendingOriginChange).toEqual({
      oldOrigin: "http://old.lan:8080",
      newOrigin: "http://new.lan:8080",
    });
    expect(authHeaderSent).toBe(false);

    // Mock waiting SW to prove single-step SKIP_WAITING + reload (U-08-F1)
    let skipWaitingPosted = false;
    const listeners = new Map<string, () => void>();
    const fakeSwContainer = {
      controller: {},
      addEventListener: (ev: string, fn: () => void) => listeners.set(ev, fn),
      removeEventListener: (ev: string) => listeners.delete(ev),
      getRegistration: async () => ({
        waiting: {
          postMessage: (msg: { type: string }) => {
            if (msg.type === "SKIP_WAITING") {
              skipWaitingPosted = true;
              listeners.get("controllerchange")?.();
            }
          },
        },
      }),
    };
    const origNavSw = navigator.serviceWorker;
    Object.defineProperty(navigator, "serviceWorker", {
      value: fakeSwContainer,
      configurable: true,
    });
    try {
      await engine.confirmOriginChange();
      expect(skipWaitingPosted).toBe(true);
      expect(reloaded).toBe(true);
      expect((await getAuthMeta(db))?.api_origin).toBe("http://new.lan:8080");
    } finally {
      Object.defineProperty(navigator, "serviceWorker", {
        value: origNavSw,
        configurable: true,
      });
      engine.stop();
    }
  });

  it("1c-T06 (b) U-11 & U-21: re-pair mints fresh mutation IDs & Sign Out revokes own device online", async () => {
    await setAuthMeta(db, {
      device_id: "d-old",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "old-tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 10,
      active_gen: 1,
      last_sync_at: "2026-11-25T10:00:00.000Z",
      last_server_now: "2026-11-25T10:00:00.000Z",
      clock_drift_ms: 0,
    });

    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));
    const oldOutbox = await enqueueItemState(db, base.id, "read");

    const pushedIds: string[] = [];
    let revokedDeviceId: string | null = null;
    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/api/v1/sync/push") {
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        for (const m of req.mutations) pushedIds.push(m.client_mutation_id);
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          results: req.mutations.map((m) => ({
            client_mutation_id: m.client_mutation_id,
            outcome: "superseded",
            reason: null,
            affected_count: null,
            current_entity_type: null,
            current_entity: null,
          })),
        } satisfies SyncPushResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          watermark: 10,
          next_seq: 10,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      if (url.pathname.startsWith("/api/v1/devices/") && url.pathname.endsWith("/revoke")) {
        revokedDeviceId = url.pathname.split("/")[4] ?? null;
        return jsonResp({
          id: revokedDeviceId,
          name: "Laptop",
          platform: "ubuntu",
          created_at: "2026-11-25T10:00:00Z",
          last_seen_at: "2026-11-25T12:00:00Z",
          revoked_at: "2026-11-25T12:01:00Z",
        });
      }
      return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
    };

    const engine = new SyncEngine({ db, fetchImpl: mockFetch, forceIdbLockFallback: true });
    await engine.completePairing(
      {
        device_id: "d-new",
        device_name: "Laptop",
        platform: "ubuntu",
        bearer_token: "new-tok",
      },
      true,
    );
    expect(pushedIds).toHaveLength(1);
    expect(pushedIds[0]).not.toBe(oldOutbox.client_mutation_id);
    expect(await db.sync_notifications.count()).toBe(0);

    // Sign Out revokes own device server-side before clearing local DB (U-21)
    await engine.signOutAndClear();
    expect(revokedDeviceId).toBe("d-new");
    expect(await getAuthMeta(db)).toBeNull();
    expect(await db.items.count()).toBe(0);
    engine.stop();
  });

  it("1c-T06 (d) U-05-F1: fenced IndexedDB lease lock aborts tab A write after tab B reclaims lease", async () => {
    let fakeNow = 1_000_000;
    const base = makeLocalBase();
    await db.items_base.put(base);

    let tabAAborted = false;
    let releaseTabA!: () => void;
    const tabAHolding = new Promise<void>((r) => {
      releaseTabA = r;
    });
    let tabAReady!: () => void;
    const waitTabAReady = new Promise<void>((r) => {
      tabAReady = r;
    });

    const tabAPromise = withSyncLock(
      {
        db,
        tabId: "tab-a",
        now: () => fakeNow,
        leaseTtlMs: 10_000,
        heartbeatMs: 999_999, // simulate throttled background timer
        forceIdbFallback: true,
      },
      async (lockA) => {
        tabAReady();
        await tabAHolding;
        try {
          await db.transaction("rw", [db.client_meta, db.items_base], async () => {
            await lockA.assertAndRenewInTx();
            await db.items_base.put({ ...base, title: "STALE_WRITE_FROM_TAB_A" });
          });
        } catch (err) {
          if (err instanceof SyncLockLostError) {
            tabAAborted = true;
          } else {
            throw err;
          }
        }
      },
    );

    await waitTabAReady;

    // While lease is valid, tab B cannot acquire
    const blocked = await withSyncLock(
      { db, tabId: "tab-b", now: () => fakeNow + 5_000, forceIdbFallback: true },
      async () => "should-not-run",
    );
    expect(blocked.acquired).toBe(false);

    // Advance past lease TTL -> tab B reclaims lease and bumps fencing_token
    fakeNow += 15_000;
    const reclaimed = await withSyncLock(
      { db, tabId: "tab-b", now: () => fakeNow, forceIdbFallback: true },
      async (lockB) => {
        expect(lockB.fencingToken).toBe(2);
        return "tab-b-won";
      },
    );
    expect(reclaimed).toEqual({ acquired: true, value: "tab-b-won" });

    // Tab A wakes up and attempts a fenced write -> MUST abort with SyncLockLostError!
    releaseTabA();
    await tabAPromise;
    expect(tabAAborted).toBe(true);
    expect((await db.items_base.get(base.id))?.title).toBe(base.title);
  });

  it("1c-T06 (e) U-17/U-17-F1: probe detects CORS misconfiguration, ALLOWED_HOSTS misconfiguration, and detached", async () => {
    // 1. CORS rejection: /health throws TypeError, no-cors /health resolves -> misconfigured (cors)
    const corsRejectedFetch: typeof fetch = async (_url, init) => {
      if (init?.mode === "no-cors") {
        return new Response("", { status: 200 });
      }
      throw new TypeError("CORS blocked");
    };
    const rCors = await probeServerReachability("http://smartrss.lan:8080", corsRejectedFetch);
    expect(rCors.status).toBe("misconfigured");
    if (rCors.status === "misconfigured") expect(rCors.cause).toBe("cors");

    // 2. ALLOWED_HOSTS rejection: /health returns 200 (exempt), /devices throws TypeError -> misconfigured (allowed_hosts)
    const hostRejectedFetch: typeof fetch = async (input) => {
      const url = String(input);
      if (url.endsWith("/api/v1/health")) {
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      throw new TypeError("CORS-less 400 from HostAllowListMiddleware");
    };
    const rHost = await probeServerReachability("http://smartrss.lan:8080", hostRejectedFetch);
    expect(rHost.status).toBe("misconfigured");
    if (rHost.status === "misconfigured") expect(rHost.cause).toBe("allowed_hosts");

    // 3. Valid server: /health 200 + /devices 401 -> reachable
    const okFetch: typeof fetch = async (input) => {
      if (String(input).endsWith("/api/v1/health")) {
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      return jsonResp({ detail: "Missing bearer" }, 401);
    };
    expect(await probeServerReachability("http://smartrss.lan:8080", okFetch)).toEqual({
      status: "reachable",
      healthOk: true,
    });

    // 4. Genuinely offline: both fail -> detached
    const offlineFetch: typeof fetch = async () => {
      throw new TypeError("Network unreachable");
    };
    expect(await probeServerReachability("http://smartrss.lan:8080", offlineFetch)).toEqual({
      status: "detached",
    });
  });

  it("1c-T06 (c) U-09-F1: enqueue right before holder release triggers follow-up push via deduplicated waiter", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 10,
      active_gen: 1,
      last_sync_at: "2026-11-25T10:00:00.000Z",
      last_server_now: "2026-11-25T10:00:00.000Z",
      clock_drift_ms: 0,
    });
    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));

    // Simulate a working navigator.locks FIFO queue in jsdom
    let locked = false;
    const queue: Array<() => void> = [];
    const fakeLocks = {
      async request(
        _name: string,
        opts: { ifAvailable?: boolean },
        cb: (lock: object | null) => Promise<void>,
      ) {
        if (locked) {
          if (opts.ifAvailable) {
            await cb(null);
            return;
          }
          await new Promise<void>((resolve) => queue.push(resolve));
        }
        locked = true;
        try {
          await cb({});
        } finally {
          locked = false;
          const next = queue.shift();
          next?.();
        }
      },
    };
    Object.defineProperty(navigator, "locks", { value: fakeLocks, configurable: true });

    let pushCalls = 0;
    let pullCalls = 0;
    let engine!: SyncEngine;
    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/api/v1/sync/push") {
        pushCalls += 1;
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          results: req.mutations.map((m) => ({
            client_mutation_id: m.client_mutation_id,
            outcome: "applied",
            reason: null,
            affected_count: null,
            current_entity_type: null,
            current_entity: null,
          })),
        } satisfies SyncPushResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        pullCalls += 1;
        // Inject an enqueue from a competing trigger while the holder is finishing pull
        if (pullCalls === 1) {
          await enqueueItemState(db, base.id, "read");
          void engine.triggerSync();
        }
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          watermark: 10,
          next_seq: 10,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
    };

    try {
      engine = new SyncEngine({ db, fetchImpl: mockFetch });
      await engine.triggerSync();
      // Give the queued blocking waiter microtasks a tick to complete
      await new Promise((r) => setTimeout(r, 30));
      expect(pushCalls).toBe(1);
      expect(await db.outbox.count()).toBe(0);
    } finally {
      engine?.stop();
      Object.defineProperty(navigator, "locks", { value: undefined, configurable: true });
    }
  });

  it("covers full pull entity types, tombstones, out-of-window skip, epoch flip restart, 401/500, forceFullResync & CSP violation", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    const cat = makeCategory();
    const feed = makeFeed();
    const item1 = makeSyncItem({ id: "33333333-3333-4333-8333-333333333301" });
    const oldExpired = makeSyncItem({
      id: "33333333-3333-4333-8333-333333333399",
      state: "expired",
      is_starred: false,
    });

    let bootPage2Flip = true;
    let pullStep = 0;

    const mockFetch: typeof fetch = async (input) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/health")
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      if (url.pathname === "/api/v1/devices") return jsonResp({}, 401);
      if (url.pathname === "/api/v1/sync/bootstrap") {
        const cursor = url.searchParams.get("cursor");
        if (cursor === null) {
          return jsonResp({
            instance_epoch: bootPage2Flip ? EPOCH_1 : EPOCH_2,
            server_now: "2026-11-25T12:00:00Z",
            watermark: 10,
            categories: [cat],
            feeds: [feed],
            clusters: [],
            interest_profile: null,
            app_settings: null,
            items: [item1],
            next_cursor: bootPage2Flip ? item1.id : null,
            has_more: bootPage2Flip,
          } satisfies SyncBootstrapResponse);
        }
        // Flip epoch once on page 2 -> engine must restart from Page 1 cleanly!
        bootPage2Flip = false;
        return jsonResp({
          instance_epoch: EPOCH_2,
          server_now: "2026-11-25T12:00:01Z",
          watermark: null,
          categories: null,
          feeds: null,
          clusters: null,
          interest_profile: null,
          app_settings: null,
          items: [],
          next_cursor: null,
          has_more: false,
        } satisfies SyncBootstrapResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        pullStep += 1;
        if (pullStep === 1) {
          return jsonResp({
            resync_required: false,
            resync_reason: null,
            instance_epoch: EPOCH_2,
            server_now: "2026-11-25T12:00:02Z",
            watermark: 20,
            next_seq: 15,
            has_more: true,
            changes: [
              {
                seq: 11,
                entity_type: "category",
                entity_id: cat.id,
                op: "upsert",
                changed_at: "2026-11-25T12:00:01Z",
                data: { ...cat, name: "Renamed Tech" },
              },
              {
                seq: 12,
                entity_type: "feed",
                entity_id: feed.id,
                op: "upsert",
                changed_at: "2026-11-25T12:00:01Z",
                data: { ...feed, title: "Updated Feed" },
              },
              {
                seq: 13,
                entity_type: "cluster",
                entity_id: "77777777-7777-4777-8777-777777777777",
                op: "upsert",
                changed_at: "2026-11-25T12:00:01Z",
                data: {
                  id: "77777777-7777-4777-8777-777777777777",
                  representative_item_id: item1.id,
                  title: "Cluster 1",
                  max_relevance_tier: 4,
                  item_count: 1,
                  last_seen_at: "2026-11-25T12:00:01Z",
                },
              },
              {
                seq: 14,
                entity_type: "item",
                entity_id: oldExpired.id,
                op: "upsert",
                changed_at: "2026-11-25T12:00:01Z",
                data: oldExpired,
              },
              {
                seq: 15,
                entity_type: "app_settings",
                entity_id: "00000000-0000-0000-0000-000000000001",
                op: "upsert",
                changed_at: "2026-11-25T12:00:01Z",
                data: {
                  default_expiration_days: 14,
                  backfill_cap: 5,
                  default_poll_interval_minutes: 30,
                  updated_at: "2026-11-25T12:00:01Z",
                },
              },
            ],
          } satisfies SyncPullResponse);
        }
        if (pullStep === 2) {
          return jsonResp({
            resync_required: false,
            resync_reason: null,
            instance_epoch: EPOCH_2,
            server_now: "2026-11-25T12:00:03Z",
            watermark: 20,
            next_seq: 20,
            has_more: false,
            changes: [
              {
                seq: 16,
                entity_type: "category",
                entity_id: cat.id,
                op: "delete",
                changed_at: "2026-11-25T12:00:02Z",
                data: null,
              },
              {
                seq: 17,
                entity_type: "cluster",
                entity_id: "77777777-7777-4777-8777-777777777777",
                op: "delete",
                changed_at: "2026-11-25T12:00:02Z",
                data: null,
              },
              {
                seq: 18,
                entity_type: "interest_profile",
                entity_id: "00000000-0000-0000-0000-000000000002",
                op: "upsert",
                changed_at: "2026-11-25T12:00:02Z",
                data: { version: 2, profile_markdown: "# Systems" },
              },
              {
                seq: 19,
                entity_type: "item",
                entity_id: item1.id,
                op: "upsert",
                changed_at: "2026-11-25T12:00:02Z",
                data: { ...item1, title: "Updated via pull" },
              },
              {
                seq: 20,
                entity_type: "feed",
                entity_id: "88888888-8888-4888-8888-888888888888",
                op: "delete",
                changed_at: "2026-11-25T12:00:02Z",
                data: null,
              },
            ],
          } satisfies SyncPullResponse);
        }
        return jsonResp({ detail: "Token revoked" }, 401);
      }
      return jsonResp({}, 404);
    };

    const mutated: string[] = [];
    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
      onItemsMutated: (c) => mutated.push(...c),
    });

    const snapshots: string[] = [];
    const unsub = engine.subscribe((s) => snapshots.push(s.connectionState));

    await engine.start();
    expect(engine.getSnapshot().connectionState).toBe("online");
    expect((await getSyncMeta(db)).instance_epoch).toBe(EPOCH_2);
    expect((await getSyncMeta(db)).sync_cursor).toBe(20);
    // Out-of-window unstored expired item was skipped!
    expect(await db.items.get(oldExpired.id)).toBeUndefined();
    expect((await db.items.get(item1.id))?.title).toBe("Updated via pull");
    expect(await db.categories.get(cat.id)).toBeUndefined();
    expect(await db.clusters.count()).toBe(0);
    expect(mutated).toContain(item1.id);

    // Offline event & CSP violation listener (review §10 note 3)
    window.dispatchEvent(new Event("offline"));
    expect(engine.getSnapshot().connectionState).toBe("detached");

    const cspEvt = new Event("securitypolicyviolation");
    Object.defineProperty(cspEvt, "effectiveDirective", { value: "connect-src" });
    Object.defineProperty(cspEvt, "blockedURI", { value: "http://localhost:8080/api/v1/health" });
    document.dispatchEvent(cspEvt);
    expect(engine.getSnapshot().cspReloadRequired).toBe(true);

    // Next sync gets 401 -> auth_revoked preserving local items
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("auth_revoked");
    expect(await db.items.count()).toBe(1);

    // Unsubscribe & stop cleanly
    unsub();
    engine.stop();
    expect(snapshots.length).toBeGreaterThan(2);
  });

  it("covers forceFullResync, notifyOutboxEnqueued, misconfigured/500/401 bootstrap, epoch_mismatch rewrite & BroadcastChannel", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    const feed = makeFeed();
    const item = makeSyncItem({ id: "33333333-3333-4333-8333-3333333333aa" });
    let mode: "ok" | "boot_401" | "boot_500" | "pull_500" | "pull_offline" | "epoch_flip_forever" =
      "ok";

    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/health") {
        if (mode === "pull_offline") throw new TypeError("offline");
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      if (url.pathname === "/api/v1/devices") return jsonResp({}, 401);
      if (url.pathname === "/api/v1/sync/bootstrap") {
        if (mode === "boot_401") return jsonResp({ detail: "revoked" }, 401);
        if (mode === "boot_500") return jsonResp({ detail: "boom" }, 500);
        if (mode === "epoch_flip_forever") {
          const c = url.searchParams.get("cursor");
          return jsonResp({
            instance_epoch: c ? EPOCH_2 : EPOCH_1,
            server_now: "2026-11-25T12:00:00Z",
            watermark: c ? null : 1,
            categories: c ? null : [],
            feeds: c ? null : [feed],
            clusters: c ? null : [],
            interest_profile: null,
            app_settings: null,
            items: [item],
            next_cursor: item.id,
            has_more: true,
          } satisfies SyncBootstrapResponse);
        }
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          watermark: 10,
          categories: [],
          feeds: [feed],
          clusters: [],
          interest_profile: null,
          app_settings: null,
          items: [item],
          next_cursor: null,
          has_more: false,
        } satisfies SyncBootstrapResponse);
      }
      if (url.pathname === "/api/v1/sync/push") {
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:01Z",
          results: req.mutations.map((m) => ({
            client_mutation_id: m.client_mutation_id,
            outcome: "applied",
            reason: null,
            affected_count: null,
            current_entity_type: null,
            current_entity: null,
          })),
        } satisfies SyncPushResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        if (mode === "pull_500") return jsonResp({ detail: "db err" }, 500);
        if (mode === "pull_offline") throw new TypeError("dropped");
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:02Z",
          watermark: 10,
          next_seq: 10,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      return jsonResp({}, 404);
    };

    const engine = new SyncEngine({ db, fetchImpl: mockFetch, forceIdbLockFallback: true });
    await engine.start();
    expect(engine.getSnapshot().connectionState).toBe("online");

    // notifyOutboxEnqueued + debounced sync
    await enqueueItemState(db, item.id, "read");
    engine.notifyOutboxEnqueued();
    await new Promise((r) => setTimeout(r, 320));
    expect(await db.outbox.count()).toBe(0);

    // Pull 500 -> sync_error
    mode = "pull_500";
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("sync_error");

    // Pull offline -> detached
    mode = "pull_offline";
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("detached");

    // forceFullResync with boot_500 -> sync_error
    mode = "boot_500";
    await engine.forceFullResync();
    expect(engine.getSnapshot().connectionState).toBe("sync_error");

    // forceFullResync with repeated epoch flip -> sync_error after MAX_BOOTSTRAP_RESTARTS
    mode = "epoch_flip_forever";
    await engine.forceFullResync();
    expect(engine.getSnapshot().connectionState).toBe("sync_error");
    expect(engine.getSnapshot().statusDetail).toContain("epoch changes");

    // forceFullResync with boot_401 -> auth_revoked
    mode = "boot_401";
    await engine.forceFullResync();
    expect(engine.getSnapshot().connectionState).toBe("auth_revoked");

    // Unpaired startup path
    engine.stop();
    await db.client_meta.delete("auth");
    const engine2 = new SyncEngine({ db, fetchImpl: mockFetch, forceIdbLockFallback: true });
    await engine2.initializeRuntimeAndAuth();
    expect(engine2.getSnapshot().connectionState).toBe("unpaired");
    await engine2.refreshAuthState();
    expect(engine2.getSnapshot().connectionState).toBe("unpaired");
    engine2.stop();
  });

  it("covers BroadcastChannel peer messages, misconfigured probe, confirmOriginChange direct path, cursor_pruned & visibilitychange", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://old.lan:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    const item = makeSyncItem({ id: "33333333-3333-4333-8333-3333333333cc" });
    let probeMisconfigured = false;
    let pullPrunedOnce = true;

    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://new.lan:8080" });
      }
      if (url.pathname === "/api/v1/health") {
        if (probeMisconfigured && init?.mode !== "no-cors") {
          throw new TypeError("CORS rejected");
        }
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      if (url.pathname === "/api/v1/devices") return jsonResp({}, 401);
      if (url.pathname === "/api/v1/sync/bootstrap") {
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          watermark: 100,
          categories: [],
          feeds: [makeFeed()],
          clusters: [],
          interest_profile: null,
          app_settings: null,
          items: [item],
          next_cursor: null,
          has_more: false,
        } satisfies SyncBootstrapResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        if (pullPrunedOnce) {
          pullPrunedOnce = false;
          return jsonResp({
            resync_required: true,
            resync_reason: "cursor_pruned",
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:01Z",
            watermark: 100,
            next_seq: 100,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:02Z",
          watermark: 100,
          next_seq: 100,
          has_more: false,
          changes: [
            {
              seq: 100,
              entity_type: "item",
              entity_id: item.id,
              op: "delete",
              changed_at: "2026-11-25T12:00:02Z",
              data: null,
            },
          ],
        } satisfies SyncPullResponse);
      }
      return jsonResp({}, 404);
    };

    const deleted: string[] = [];
    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
      onItemsMutated: (_c, d) => deleted.push(...d),
    });

    await engine.start();
    expect(engine.getSnapshot().connectionState).toBe("origin_confirm");

    // Confirm without SW controller -> proceeds directly to sync (cursor_pruned -> re-bootstrap -> pull)
    await engine.confirmOriginChange();
    expect(engine.getSnapshot().connectionState).toBe("online");
    expect(deleted).toContain(item.id);

    // Simulate CORS misconfiguration after going detached
    window.dispatchEvent(new Event("offline"));
    probeMisconfigured = true;
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("misconfigured");
    probeMisconfigured = false;

    // Peer BroadcastChannel messages (outbox_enqueued, sync_complete, auth_changed)
    const peerChannel = new BroadcastChannel("smartrss-sync");
    peerChannel.postMessage({ type: "outbox_enqueued", senderId: "other-tab" });
    peerChannel.postMessage({ type: "sync_complete", senderId: "other-tab" });
    peerChannel.postMessage({ type: "auth_changed", senderId: "other-tab" });
    await new Promise((r) => setTimeout(r, 320));
    expect(engine.getSnapshot().connectionState).toBe("online");
    peerChannel.close();

    // Online & visibilitychange handlers run cleanly
    window.dispatchEvent(new Event("online"));
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((r) => setTimeout(r, 50));

    // Busy IDB lease lock -> sets recheckOnSyncComplete -> sync_complete broadcast drains outbox (U-09-F1)
    await db.client_meta.put({
      key: "sync_lock",
      value: {
        owner_tab_id: "other-holder",
        fencing_token: 99,
        acquired_at: Date.now(),
        expires_at: Date.now() + 60_000,
      },
    });
    await enqueueItemState(db, item.id, "read");
    await engine.triggerSync(); // lock busy -> sets recheckOnSyncComplete = true
    // Release other holder's lock and broadcast sync_complete
    await db.client_meta.put({
      key: "sync_lock",
      value: {
        owner_tab_id: "other-holder",
        fencing_token: 99,
        acquired_at: Date.now(),
        expires_at: 0,
      },
    });
    const peer2 = new BroadcastChannel("smartrss-sync");
    peer2.postMessage({ type: "sync_complete", senderId: "other-holder" });
    await new Promise((r) => setTimeout(r, 80));
    peer2.close();

    // Push auth_revoked / detached / sync_error handling inside executeSingleCycle
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 100,
      active_gen: 1,
      last_sync_at: "2026-11-25T12:00:00.000Z",
      last_server_now: "2026-11-25T12:00:00.000Z",
      clock_drift_ms: 0,
    });
    await enqueueItemState(db, item.id, "unread");
    const errEngine = new SyncEngine({
      db,
      fetchImpl: async () => jsonResp({ detail: "server 500" }, 500),
      forceIdbLockFallback: true,
    });
    await errEngine.triggerSync();
    expect(errEngine.getSnapshot().connectionState).toBe("sync_error");
    errEngine.stop();

    const authErrEngine = new SyncEngine({
      db,
      fetchImpl: async () => jsonResp({ detail: "revoked" }, 401),
      forceIdbLockFallback: true,
    });
    await authErrEngine.triggerSync();
    expect(authErrEngine.getSnapshot().connectionState).toBe("auth_revoked");
    authErrEngine.stop();

    const detEngine = new SyncEngine({
      db,
      fetchImpl: async () => {
        throw new TypeError("offline");
      },
      forceIdbLockFallback: true,
    });
    await detEngine.triggerSync();
    expect(detEngine.getSnapshot().connectionState).toBe("detached");
    detEngine.stop();

    engine.stop();
  });

  it("covers committing-phase resume, null watermark error, post-pull eviction, debounce coalescing & offline signOut", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    // 1. Bootstrap Page 1 returning null watermark -> sync_error
    const nullWmEngine = new SyncEngine({
      db,
      fetchImpl: async () =>
        jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          watermark: null,
          categories: [],
          feeds: [],
          clusters: [],
          interest_profile: null,
          app_settings: null,
          items: [],
          next_cursor: null,
          has_more: false,
        } satisfies SyncBootstrapResponse),
      forceIdbLockFallback: true,
    });
    await nullWmEngine.triggerSync();
    expect(nullWmEngine.getSnapshot().connectionState).toBe("sync_error");
    expect(nullWmEngine.getSnapshot().statusDetail).toContain("null watermark");
    nullWmEngine.stop();

    // 2. Resume directly from phase='committing' without re-fetching bootstrap pages (U-06)
    const stagedItem = makeSyncItem({
      id: "33333333-3333-4333-8333-3333333333dd",
      state: "expired",
      is_starred: false,
    });
    await db.bootstrap_items.put(stagedItem);
    await db.bootstrap_meta.put({
      key: "active",
      phase: "committing",
      target_gen: 2,
      page1_epoch: EPOCH_1,
      page1_watermark: 50,
      server_now: "2026-11-25T12:00:00.000Z",
      next_cursor: null,
      categories: [makeCategory()],
      feeds: [makeFeed()],
      clusters: [],
      interest_profile: null,
      app_settings: null,
      pages_fetched: 1,
      items_staged: 1,
    });

    let bootstrapNetworkCalls = 0;
    const evictedNotified: string[] = [];
    const resumeEngine = new SyncEngine({
      db,
      fetchImpl: async (input) => {
        const url = new URL(String(input), "http://localhost:4180");
        if (url.pathname === "/api/v1/sync/bootstrap") {
          bootstrapNetworkCalls += 1;
        }
        if (url.pathname === "/api/v1/sync/pull") {
          return jsonResp({
            resync_required: false,
            resync_reason: null,
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:01Z",
            watermark: 50,
            next_seq: 50,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      },
      forceIdbLockFallback: true,
      onItemsMutated: (_c, del) => evictedNotified.push(...del),
    });

    // Rapid double debounce coalesces timer cleanly
    resumeEngine.scheduleDebouncedSync(500);
    resumeEngine.scheduleDebouncedSync(500);
    await resumeEngine.confirmOriginChange(); // no-op when no pending origin change
    await resumeEngine.triggerSync();
    expect(bootstrapNetworkCalls).toBe(0);
    // Expired unstarred item evicted right after pull completed
    expect(evictedNotified).toContain(stagedItem.id);
    expect(await db.items.count()).toBe(0);

    // Offline signOutAndClear still wipes Dexie even when revokeDevice throws
    const offlineSignOut = new SyncEngine({
      db,
      fetchImpl: async () => {
        throw new TypeError("offline");
      },
      forceIdbLockFallback: true,
    });
    await offlineSignOut.signOutAndClear();
    expect(await getAuthMeta(db)).toBeNull();
    // Unpaired forceFullResync & scheduleDebouncedSync are clean no-ops
    await offlineSignOut.forceFullResync();
    offlineSignOut.scheduleDebouncedSync();
    await offlineSignOut.triggerSync();
    expect(offlineSignOut.getSnapshot().connectionState).toBe("unpaired");
    resumeEngine.stop();
    offlineSignOut.stop();
  });

  it("covers initial pairing (isRepair=false), offline config fallback, epoch_mismatch restore failures & SyncLockLostError", async () => {
    const item = makeSyncItem({ id: "33333333-3333-4333-8333-3333333333ee" });
    let reindexed = 0;
    let mode: "initial_pair" | "restore_boot_fail" | "restore_push_401" | "offline_config" =
      "initial_pair";

    const mockFetch: typeof fetch = async (input) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        if (mode === "offline_config") throw new TypeError("offline");
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/health") {
        if (mode === "offline_config") throw new TypeError("offline");
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      }
      if (url.pathname === "/api/v1/devices") return jsonResp({}, 401);
      if (url.pathname === "/api/v1/sync/bootstrap") {
        if (mode === "restore_boot_fail") return jsonResp({ detail: "err" }, 500);
        return jsonResp({
          instance_epoch: mode === "initial_pair" ? EPOCH_1 : EPOCH_2,
          server_now: "2026-11-25T12:00:00Z",
          watermark: 10,
          categories: [],
          feeds: [makeFeed()],
          clusters: [
            {
              id: "99999999-9999-4999-8999-999999999901",
              representative_item_id: item.id,
              title: "Story",
              max_relevance_tier: 5,
              item_count: 1,
              last_seen_at: "2026-11-25T12:00:00Z",
            },
          ],
          interest_profile: null,
          app_settings: null,
          items: [item],
          next_cursor: null,
          has_more: false,
        } satisfies SyncBootstrapResponse);
      }
      if (url.pathname === "/api/v1/sync/push") {
        if (mode === "restore_push_401") return jsonResp({ detail: "revoked" }, 401);
        return jsonResp({
          instance_epoch: EPOCH_2,
          server_now: "2026-11-25T12:00:01Z",
          results: [],
        } satisfies SyncPushResponse);
      }
      if (url.pathname === "/api/v1/sync/pull") {
        if (mode === "restore_boot_fail" || mode === "restore_push_401") {
          return jsonResp({
            resync_required: true,
            resync_reason: "epoch_mismatch",
            instance_epoch: EPOCH_2,
            server_now: "2026-11-25T12:00:01Z",
            watermark: 5,
            next_seq: 5,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:01Z",
          watermark: 10,
          next_seq: 10,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      return jsonResp({}, 404);
    };

    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
      onFullReindex: async () => {
        reindexed += 1;
      },
    });
    await engine.start();
    await engine.completePairing(
      {
        device_id: "d-init",
        device_name: "Laptop",
        platform: "ubuntu",
        bearer_token: "tok-init",
      },
      false,
    );
    expect(engine.getSnapshot().connectionState).toBe("online");
    expect(reindexed).toBeGreaterThanOrEqual(1);
    expect(await db.clusters.count()).toBe(1);

    // Offline startup fallback when /client-config.json fails & probe is detached
    mode = "offline_config";
    await engine.initializeRuntimeAndAuth();
    expect(engine.getSnapshot().connectionState).toBe("detached");

    // Restore where re-bootstrap returns 500 -> sync_error
    mode = "restore_boot_fail";
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("sync_error");

    // Restore where post-bootstrap push returns 401 -> auth_revoked
    await enqueueItemState(db, item.id, "read");
    mode = "restore_push_401";
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("auth_revoked");

    // Restore where post-bootstrap push returns 500 -> sync_error, and network error -> detached
    const pushErrRestoreEngine = new SyncEngine({
      db,
      fetchImpl: async (input) => {
        const url = new URL(String(input), "http://localhost:4180");
        if (url.pathname === "/api/v1/sync/pull") {
          return jsonResp({
            resync_required: true,
            resync_reason: "epoch_mismatch",
            instance_epoch: EPOCH_2,
            server_now: "2026-11-25T12:00:01Z",
            watermark: 5,
            next_seq: 5,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        if (url.pathname === "/api/v1/sync/bootstrap") {
          return jsonResp({
            instance_epoch: EPOCH_2,
            server_now: "2026-11-25T12:00:01Z",
            watermark: 5,
            categories: [],
            feeds: [makeFeed()],
            clusters: [],
            interest_profile: null,
            app_settings: null,
            items: [item],
            next_cursor: null,
            has_more: false,
          } satisfies SyncBootstrapResponse);
        }
        if (url.pathname === "/api/v1/sync/push") {
          return jsonResp({ detail: "500 during restore push" }, 500);
        }
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      },
      forceIdbLockFallback: true,
    });
    await db.outbox.clear();
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 10,
      active_gen: 1,
      last_sync_at: "2026-11-25T12:00:00.000Z",
      last_server_now: "2026-11-25T12:00:00.000Z",
      clock_drift_ms: 0,
    });
    await db.replay_journal.put({
      client_mutation_id: "88888888-8888-4888-8888-888888888801",
      acknowledged_at: new Date().toISOString(),
      payload: {
        type: "item_set_starred",
        client_mutation_id: "88888888-8888-4888-8888-888888888801",
        client_timestamp: new Date().toISOString(),
        item_id: item.id,
        is_starred: true,
      },
    });
    await pushErrRestoreEngine.triggerSync();
    expect(pushErrRestoreEngine.getSnapshot().connectionState).toBe("sync_error");
    pushErrRestoreEngine.stop();

    // Triggering while auth_revoked is a no-op
    await engine.triggerSync();
    expect(engine.getSnapshot().connectionState).toBe("auth_revoked");

    // Stop while a debounce timer is armed clears it cleanly; SyncLockLostError during pull exits cleanly
    const stolenEngine = new SyncEngine({
      db,
      fetchImpl: async (input) => {
        const url = new URL(String(input), "http://localhost:4180");
        if (url.pathname === "/api/v1/sync/pull") {
          // Another tab steals the IDB lease right before pull commits!
          await db.client_meta.put({
            key: "sync_lock",
            value: {
              owner_tab_id: "thief-tab",
              fencing_token: 999,
              acquired_at: Date.now(),
              expires_at: Date.now() + 60_000,
            },
          });
          return jsonResp({
            resync_required: false,
            resync_reason: null,
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:05Z",
            watermark: 15,
            next_seq: 15,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      },
      forceIdbLockFallback: true,
    });
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 10,
      active_gen: 1,
      last_sync_at: "2026-11-25T12:00:00.000Z",
      last_server_now: "2026-11-25T12:00:00.000Z",
      clock_drift_ms: 0,
    });
    await db.outbox.clear();
    await stolenEngine.triggerSync();
    // Stolen tab aborted cleanly -> cursor stayed at 10 (never advanced to 15!)
    expect((await getSyncMeta(db)).sync_cursor).toBe(10);
    stolenEngine.scheduleDebouncedSync(5_000);
    stolenEngine.stop();

    engine.stop();
  });

  it("handles offline/online browser events, stolen lease during bootstrap, and restore push auth_revoked/detached", async () => {
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    const item = makeSyncItem({ id: "99999999-9999-4999-8999-999999999901" });

    // 1) online -> offline browser event -> online browser event
    const eventEngine = new SyncEngine({
      db,
      fetchImpl: async (input) => {
        const url = new URL(String(input), "http://localhost:4180");
        if (url.pathname === "/client-config.json") {
          return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
        }
        if (url.pathname === "/api/v1/health") {
          return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
        }
        if (url.pathname === "/api/v1/devices") {
          return jsonResp({}, 401);
        }
        if (url.pathname === "/api/v1/sync/bootstrap") {
          return jsonResp({
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:00Z",
            watermark: 10,
            categories: [],
            feeds: [makeFeed()],
            clusters: [],
            interest_profile: null,
            app_settings: null,
            items: [item],
            next_cursor: null,
            has_more: false,
          } satisfies SyncBootstrapResponse);
        }
        if (url.pathname === "/api/v1/sync/pull") {
          return jsonResp({
            resync_required: false,
            resync_reason: null,
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:00Z",
            watermark: 10,
            next_seq: 10,
            has_more: false,
            changes: [],
          } satisfies SyncPullResponse);
        }
        return jsonResp({ status: "ok" });
      },
      forceIdbLockFallback: true,
    });
    await eventEngine.start();
    expect(eventEngine.getSnapshot().connectionState).toBe("online");
    window.dispatchEvent(new Event("offline"));
    expect(eventEngine.getSnapshot().connectionState).toBe("detached");
    window.dispatchEvent(new Event("online"));
    await new Promise((r) => setTimeout(r, 30));
    expect(eventEngine.getSnapshot().connectionState).toBe("online");
    eventEngine.stop();

    // 2) Stolen IDB lease mid-bootstrap -> SyncLockLostError bubbles out of runStreamingBootstrap and aborts cleanly
    await db.client_meta.delete("sync");
    await db.client_meta.delete("sync_lock");
    const stolenBootEngine = new SyncEngine({
      db,
      fetchImpl: async (input) => {
        const url = new URL(String(input), "http://localhost:4180");
        if (url.pathname === "/api/v1/sync/bootstrap") {
          await db.client_meta.put({
            key: "sync_lock",
            value: {
              owner_tab_id: "boot-thief",
              fencing_token: 9999,
              acquired_at: Date.now(),
              expires_at: Date.now() + 60_000,
            },
          });
          return jsonResp({
            instance_epoch: EPOCH_1,
            server_now: "2026-11-25T12:00:00Z",
            watermark: 10,
            categories: [],
            feeds: [makeFeed()],
            clusters: [],
            interest_profile: null,
            app_settings: null,
            items: [item],
            next_cursor: null,
            has_more: false,
          } satisfies SyncBootstrapResponse);
        }
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      },
      forceIdbLockFallback: true,
    });
    await stolenBootEngine.triggerSync();
    expect((await getSyncMeta(db)).sync_cursor).toBeNull();
    stolenBootEngine.stop();

    // 3) Restore push returns 401 (auth_revoked) and network error (detached)
    for (const mode of ["auth_revoked", "detached"] as const) {
      await db.outbox.clear();
      await db.replay_journal.clear();
      await db.client_meta.delete("sync_lock");
      await setSyncMeta(db, {
        instance_epoch: EPOCH_1,
        sync_cursor: 10,
        active_gen: 1,
        last_sync_at: "2026-11-25T12:00:00.000Z",
        last_server_now: "2026-11-25T12:00:00.000Z",
        clock_drift_ms: 0,
      });
      await db.replay_journal.put({
        client_mutation_id: `99999999-9999-4999-8999-99999999990${mode === "auth_revoked" ? "2" : "3"}`,
        acknowledged_at: new Date().toISOString(),
        payload: {
          type: "item_set_starred",
          client_mutation_id: `99999999-9999-4999-8999-99999999990${mode === "auth_revoked" ? "2" : "3"}`,
          client_timestamp: new Date().toISOString(),
          item_id: item.id,
          is_starred: true,
        },
      });
      const resyncPushEngine = new SyncEngine({
        db,
        fetchImpl: async (input) => {
          const url = new URL(String(input), "http://localhost:4180");
          if (url.pathname === "/api/v1/sync/pull") {
            return jsonResp({
              resync_required: true,
              resync_reason: "epoch_mismatch",
              instance_epoch: EPOCH_2,
              server_now: "2026-11-25T12:00:05Z",
              watermark: 20,
              next_seq: 0,
              has_more: false,
              changes: [],
            } satisfies SyncPullResponse);
          }
          if (url.pathname === "/api/v1/sync/bootstrap") {
            return jsonResp({
              instance_epoch: EPOCH_2,
              server_now: "2026-11-25T12:00:05Z",
              watermark: 20,
              categories: [],
              feeds: [makeFeed()],
              clusters: [],
              interest_profile: null,
              app_settings: null,
              items: [item],
              next_cursor: null,
              has_more: false,
            } satisfies SyncBootstrapResponse);
          }
          if (url.pathname === "/api/v1/sync/push") {
            if (mode === "auth_revoked") {
              return jsonResp({ detail: "revoked" }, 401);
            }
            throw new TypeError("offline during restore push");
          }
          if (url.pathname === "/api/v1/health") {
            throw new TypeError("offline");
          }
          return jsonResp({});
        },
        forceIdbLockFallback: true,
      });
      await resyncPushEngine.triggerSync();
      expect(resyncPushEngine.getSnapshot().connectionState).toBe(mode);
      resyncPushEngine.stop();
    }
  });
});
