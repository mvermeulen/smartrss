import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiHttpError, ApiNetworkError, SmartRSSApiClient } from "../../api/client";
import {
  normalizeBootstrapResponse,
  normalizeCategoryResponse,
  normalizeDeadLetterList,
  normalizeDeviceResponse,
  normalizeFeedResponse,
  normalizeItemResponse,
  normalizePullResponse,
  normalizePushResponse,
  normalizeSearchResponse,
  normalizeSyncAppSettings,
  normalizeSyncCategory,
  normalizeSyncCluster,
  normalizeSyncFeed,
  normalizeSyncItem,
  normalizeSystemHealth,
  normalizeSystemMetrics,
  toCanonicalIso,
} from "../../api/normalize";
import { getSyncMeta, setAuthMeta, setSyncMeta } from "../../db/clientMeta";
import { SmartRSSDB, type OutboxRow } from "../../db/schema";
import { sanitizeFeedHtmlToFragment } from "../../security/sanitizeHtml";
import { precacheShellUrls } from "../../sw/sw";
import { SyncEngine } from "../../sync/engine";
import { withSyncLock } from "../../sync/lock";
import {
  enqueueBulkMarkRead,
  enqueueFeedField,
  enqueueItemStarred,
  enqueueItemState,
  flushOutbox,
  MAX_BATCH_UTF8_BYTES,
  measurePushUtf8Bytes,
  packOutboxBatch,
} from "../../sync/outbox";
import { applyOutboxToItem, toLocalItemBase } from "../../sync/reconciliation";
import type { SyncPullResponse, SyncPushRequest, SyncPushResponse } from "../../types/sync";
import { makeCategory, makeFeed, makeSyncItem } from "../fixtures/syncFactories";

const EPOCH_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CANONICAL_ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const LOWERCASE_UUID_V4_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("1c Implementation Review Regressions (V-01..V-10)", () => {
  let db: SmartRSSDB;

  beforeEach(async () => {
    db = new SmartRSSDB(`smartrss_rev_${Math.random().toString(36).slice(2)}`);
    await db.open();
  });

  afterEach(async () => {
    vi.useRealTimers();
    db.close();
    await db.delete();
  });

  async function seedPairedReady(): Promise<{ itemId: string; feedId: string }> {
    const feed = makeFeed();
    const item = makeSyncItem({ id: "33333333-3333-4333-8333-333333333301", feed_id: feed.id });
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
      last_sync_at: "2026-11-25T12:00:00.000Z",
      last_server_now: "2026-11-25T12:00:00.000Z",
      clock_drift_ms: 0,
    });
    await db.feeds_base.put(feed);
    await db.feeds.put(feed);
    const base = toLocalItemBase(item, 1);
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));
    return { itemId: item.id, feedId: feed.id };
  }

  it("test_v01_push_non_json_200_lands_in_sync_error_and_installs_poll_timer", async () => {
    const { itemId } = await seedPairedReady();
    await enqueueItemState(db, itemId, "read");

    let pushCalls = 0;
    const mockFetch: typeof fetch = async (input) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/sync/push") {
        pushCalls += 1;
        return new Response("<html>proxy</html>", {
          status: 200,
          headers: { "Content-Type": "text/html" },
        });
      }
      return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
    };

    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
    });
    try {
      await expect(engine.start()).resolves.toBeUndefined();
      expect(engine.getSnapshot().connectionState).toBe("sync_error");
      expect(await db.outbox.count()).toBe(1);
      // Outbox row recorded attempt & invalid-JSON error
      const row = (await db.outbox.toArray())[0]!;
      expect(row.attempts).toBe(1);
      expect(row.last_error).toContain("Invalid JSON");
      // Poll timer (60_000ms) was installed despite startup push failure
      expect(setIntervalSpy.mock.calls.some((c) => c[1] === 60_000)).toBe(true);
      expect(pushCalls).toBe(1);
    } finally {
      engine.stop();
      setIntervalSpy.mockRestore();
    }
  });

  it("test_v01_dexie_throw_inside_apply_push_lands_in_sync_error_not_wedged", async () => {
    const { itemId } = await seedPairedReady();
    await enqueueItemState(db, itemId, "read");

    const putSpy = vi
      .spyOn(db.replay_journal, "put")
      .mockRejectedValueOnce(new DOMException("QuotaExceededError", "QuotaExceededError"));

    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/client-config.json") {
        return jsonResp({ mode: "localhost", api_origin: "http://localhost:8080" });
      }
      if (url.pathname === "/api/v1/sync/push") {
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:05.000Z",
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
      return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
    };

    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
    });
    try {
      await expect(engine.triggerSync()).resolves.toBeUndefined();
      expect(engine.getSnapshot().connectionState).toBe("sync_error");
      expect(engine.getSnapshot().statusDetail).toContain("QuotaExceededError");
    } finally {
      putSpy.mockRestore();
      engine.stop();
    }
  });

  it("test_v01_startup_unexpected_exception_still_installs_poll_timer", async () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const engine = new SyncEngine({ db, forceIdbLockFallback: true });
    vi.spyOn(engine, "initializeRuntimeAndAuth").mockRejectedValueOnce(
      new Error("Unexpected IDB open failure"),
    );
    try {
      await expect(engine.start()).resolves.toBeUndefined();
      expect(engine.getSnapshot().connectionState).toBe("sync_error");
      expect(engine.getSnapshot().statusDetail).toContain("Unexpected IDB open failure");
      expect(setIntervalSpy.mock.calls.some((c) => c[1] === 60_000)).toBe(true);
    } finally {
      engine.stop();
      setIntervalSpy.mockRestore();
    }
  });

  it("test_v02_flush_outbox_omitted_results_halts_with_sync_error_and_bounded_requests", async () => {
    const { itemId } = await seedPairedReady();
    await enqueueItemState(db, itemId, "read");
    await enqueueItemStarred(db, itemId, true);

    let pushCalls = 0;
    const api = new SmartRSSApiClient("http://localhost:8080", "tok", async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/v1/sync/push") {
        pushCalls += 1;
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:05.000Z",
          results: [], // Server omitted ALL results!
        } satisfies SyncPushResponse);
      }
      return jsonResp({});
    });

    const res = await withSyncLock({ db, tabId: "t-v02", forceIdbFallback: true }, async (lock) =>
      flushOutbox(db, api, lock),
    );
    expect(res.acquired).toBe(true);
    if (res.acquired) {
      expect(res.value.status).toBe("sync_error");
      if (res.value.status === "sync_error") {
        expect(res.value.message).toContain("omitted 2");
      }
    }
    expect(pushCalls).toBe(1);
    const rows = await db.outbox.toArray();
    expect(rows).toHaveLength(2);
    expect(rows[0]!.last_error).toContain("omitted 2");
  });

  it("test_v03_idb_lease_lock_blocks_same_tab_reentry_and_engine_coalesces_overlapping_triggers", async () => {
    // 1) Lock-level test: second withSyncLock with the SAME tabId must return { acquired: false }
    let innerAcquired: boolean | null = null;
    const outer = await withSyncLock(
      { db, tabId: "same-tab-1", forceIdbFallback: true },
      async () => {
        const inner = await withSyncLock(
          { db, tabId: "same-tab-1", forceIdbFallback: true },
          async () => "should-not-run",
        );
        innerAcquired = inner.acquired;
        return "outer-ok";
      },
    );
    expect(outer.acquired).toBe(true);
    expect(innerAcquired).toBe(false);

    // 2) Engine-level reproduction: delayed push + overlapping triggerSync in same tab -> 1 push!
    const { itemId } = await seedPairedReady();
    await enqueueItemState(db, itemId, "read");

    let pushCalls = 0;
    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/api/v1/sync/push") {
        pushCalls += 1;
        await new Promise((r) => setTimeout(r, 50));
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:05.000Z",
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
        return jsonResp({
          resync_required: false,
          resync_reason: null,
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:06.000Z",
          watermark: 10,
          next_seq: 10,
          has_more: false,
          changes: [],
        } satisfies SyncPullResponse);
      }
      return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
    };

    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
    });
    try {
      const p1 = engine.triggerSync();
      await new Promise((r) => setTimeout(r, 15));
      const p2 = engine.triggerSync();
      await Promise.all([p1, p2]);
      expect(pushCalls).toBe(1);
      expect(await db.outbox.count()).toBe(0);
    } finally {
      engine.stop();
    }
  });

  it("test_v04_push_clock_drift_persist_is_fenced_and_aborts_on_stolen_lease", async () => {
    const { itemId } = await seedPairedReady();
    await enqueueItemState(db, itemId, "read");

    const mockFetch: typeof fetch = async (input, init) => {
      const url = new URL(String(input), "http://localhost:4180");
      if (url.pathname === "/api/v1/sync/push") {
        // Tab B steals the lease AND advances sync_cursor while tab A's push is in flight!
        await db.client_meta.put({
          key: "sync_lock",
          value: {
            owner_tab_id: "tab-B",
            fencing_token: 999,
            acquired_at: Date.now(),
            expires_at: Date.now() + 60_000,
          },
        });
        await setSyncMeta(db, {
          instance_epoch: EPOCH_1,
          sync_cursor: 42,
          active_gen: 1,
          last_sync_at: "2026-11-25T12:00:00.000Z",
          last_server_now: "2026-11-25T12:00:00.000Z",
          clock_drift_ms: 0,
        });
        const req = JSON.parse(String(init?.body)) as SyncPushRequest;
        return jsonResp({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T13:30:00.000Z", // +90 min drift that MUST NOT be persisted!
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
      return jsonResp({});
    };

    const engine = new SyncEngine({
      db,
      fetchImpl: mockFetch,
      forceIdbLockFallback: true,
    });
    try {
      await engine.triggerSync();
      // Give any accidental fire-and-forget microtasks time to land
      await new Promise((r) => setTimeout(r, 25));
      const meta = await getSyncMeta(db);
      expect(meta.sync_cursor).toBe(42);
      expect(meta.last_server_now).toBe("2026-11-25T12:00:00.000Z");
      expect(meta.clock_drift_ms).toBe(0);
    } finally {
      engine.stop();
    }
  });

  it("test_v05_stalled_response_body_times_out_as_api_network_error", async () => {
    const stalledFetch: typeof fetch = async (_input, init) => {
      const signal = init?.signal;
      const stream = new ReadableStream({
        start(controller) {
          signal?.addEventListener("abort", () => {
            controller.error(new DOMException("Aborted", "AbortError"));
          });
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };

    const api = new SmartRSSApiClient("http://localhost:8080", "tok", stalledFetch);
    let caught: unknown = null;
    try {
      await api.health(40);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
    expect((caught as ApiNetworkError).isTimeout).toBe(true);
  });

  it("test_v06_sw_precache_rejects_on_non_ok_response", async () => {
    const stored = new Map<string, Response>();
    const fakeCache = {
      put: async (k: string, v: Response) => {
        stored.set(k, v);
      },
    } as unknown as Cache;
    const fakeCaches = {
      open: async () => fakeCache,
    } as unknown as CacheStorage;

    const fakeFetch: typeof fetch = async (input) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.endsWith("/assets/missing.js")) {
        return new Response("Not Found", { status: 404 });
      }
      return new Response("ok", { status: 200 });
    };

    await expect(
      precacheShellUrls(
        "smartrss-shell-test",
        ["http://localhost:4180/index.html", "http://localhost:4180/assets/missing.js"],
        fakeCaches,
        fakeFetch,
      ),
    ).rejects.toThrow(/404/);

    // Succeeds when all URLs return 200
    await expect(
      precacheShellUrls(
        "smartrss-shell-test",
        ["http://localhost:4180/index.html"],
        fakeCaches,
        fakeFetch,
      ),
    ).resolves.toBeUndefined();
    expect(stored.has("http://localhost:4180/index.html")).toBe(true);
  });

  it("test_v07_1_5_mib_byte_cap_and_canonical_enqueue_ids_and_timestamps", async () => {
    // (a) 1.5 MiB UTF-8 byte cap & exact O(n) equivalence with measurePushUtf8Bytes
    const bigIds = Array.from(
      { length: 1_500 },
      (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    );
    const largeRows: OutboxRow[] = Array.from({ length: 30 }, (_, idx) => {
      const id = `11111111-1111-4111-8111-${String(idx).padStart(12, "0")}`;
      return {
        local_seq: idx + 1,
        client_mutation_id: id,
        created_at: "2026-11-25T12:00:00.000Z",
        payload: {
          type: "item_mark_bulk_read",
          client_mutation_id: id,
          client_timestamp: "2026-11-25T12:00:00.000Z",
          scope: "items",
          item_ids: bigIds.slice(0, 1_400),
        },
        entity_key: "bulk",
        target_absent: false,
        captured_item_ids: null,
        display_label: "Large batch",
        attempts: 0,
        last_error: null,
      };
    });

    // Test with realistic > 1.5 MiB total across rows that each fit under 5,000 IDs
    const byteHeavyRows: OutboxRow[] = Array.from({ length: 50 }, (_, idx) => {
      const id = `22222222-2222-4222-8222-${String(idx).padStart(12, "0")}`;
      return {
        local_seq: idx + 1,
        client_mutation_id: id,
        created_at: "2026-11-25T12:00:00.000Z",
        payload: {
          type: "feed_set_field",
          client_mutation_id: id,
          client_timestamp: "2026-11-25T12:00:00.000Z",
          feed_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          field: "status",
          value: "x".repeat(45_000), // ~45 KB each -> 50 rows = ~2.25 MiB > 1.5 MiB!
        },
        entity_key: "feed:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        target_absent: false,
        captured_item_ids: null,
        display_label: "Byte heavy",
        attempts: 0,
        last_error: null,
      };
    });
    expect(measurePushUtf8Bytes(byteHeavyRows.map((r) => r.payload))).toBeGreaterThan(
      MAX_BATCH_UTF8_BYTES,
    );

    const packedBytes = packOutboxBatch(byteHeavyRows);
    expect(packedBytes.length).toBeGreaterThan(0);
    expect(packedBytes.length).toBeLessThan(byteHeavyRows.length);
    const actualBytes = measurePushUtf8Bytes(packedBytes.map((r) => r.payload));
    expect(actualBytes).toBeLessThanOrEqual(MAX_BATCH_UTF8_BYTES);
    // Adding the next row would strictly exceed MAX_BATCH_UTF8_BYTES (proves O(n) matches exact JSON length)
    const withNext = [...packedBytes, byteHeavyRows[packedBytes.length]!];
    expect(measurePushUtf8Bytes(withNext.map((r) => r.payload))).toBeGreaterThan(
      MAX_BATCH_UTF8_BYTES,
    );
    expect(packOutboxBatch(largeRows).length).toBe(3); // 3 * 1400 = 4200 <= 5000, 4th would be 5600 > 5000

    // (b) Canonical 24-char UTC ISO & lowercase UUIDv4 produced by every enqueue helper
    const { itemId, feedId } = await seedPairedReady();
    const r1 = await enqueueItemState(db, itemId, "read");
    const r2 = await enqueueItemStarred(db, itemId, true);
    const r3 = await enqueueFeedField(db, feedId, "status", "muted");
    const r4 = await enqueueBulkMarkRead(db, { scope: "all" });
    for (const row of [r1, r2, r3, ...r4]) {
      expect(row.client_mutation_id).toMatch(LOWERCASE_UUID_V4_RE);
      expect(row.payload.client_mutation_id).toBe(row.client_mutation_id);
      expect(row.payload.client_timestamp).toMatch(CANONICAL_ISO_RE);
      expect(row.created_at).toMatch(CANONICAL_ISO_RE);
    }
  });

  it("test_v07_normalize_table_driven_all_dtos", () => {
    const UPPER = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA";
    const LOWER = UPPER.toLowerCase();
    const TS_RAW = "2026-11-25T12:34:56.500000+00:00";
    const TS_CANON = "2026-11-25T12:34:56.500Z";

    expect(() => toCanonicalIso("not-a-timestamp")).toThrow(/Invalid ISO timestamp/);

    const cat = normalizeSyncCategory({
      ...makeCategory({ id: UPPER }),
      updated_at: TS_RAW,
    });
    expect(cat.id).toBe(LOWER);
    expect(cat.updated_at).toBe(TS_CANON);

    const feed = normalizeSyncFeed({
      ...makeFeed({ id: UPPER, category_id: UPPER }),
      active_feed_instructions: [{ id: UPPER, reason_text: "Focus", created_at: TS_RAW }],
      field_updated_at: { status: TS_RAW },
      updated_at: TS_RAW,
    });
    expect(feed.id).toBe(LOWER);
    expect(feed.category_id).toBe(LOWER);
    expect(feed.active_feed_instructions[0]!.id).toBe(LOWER);
    expect(feed.active_feed_instructions[0]!.created_at).toBe(TS_CANON);
    expect(feed.field_updated_at.status).toBe(TS_CANON);

    const feedNullCat = normalizeSyncFeed({
      ...makeFeed({ id: UPPER, category_id: null }),
      field_updated_at: undefined as unknown as Record<string, string>,
      active_feed_instructions: undefined as unknown as SyncFeedDTO["active_feed_instructions"],
    });
    expect(feedNullCat.category_id).toBeNull();
    expect(feedNullCat.field_updated_at).toEqual({});

    const item = normalizeSyncItem({
      ...makeSyncItem({ id: UPPER, feed_id: UPPER, cluster_id: UPPER }),
      published_at: TS_RAW,
      fetched_at: TS_RAW,
      read_at: TS_RAW,
      starred_at: TS_RAW,
      expired_at: null,
      unexpired_at: TS_RAW,
      read_state_updated_at: TS_RAW,
      starred_updated_at: TS_RAW,
      updated_at: TS_RAW,
      user_feedback: { feedback_type: "thumbs_up", reason_text: "Great", created_at: TS_RAW },
    });
    expect(item.id).toBe(LOWER);
    expect(item.cluster_id).toBe(LOWER);
    expect(item.user_feedback?.created_at).toBe(TS_CANON);

    const cluster = normalizeSyncCluster({
      id: UPPER,
      representative_item_id: UPPER,
      title: "Cluster",
      max_relevance_tier: 4,
      item_count: 2,
      last_seen_at: TS_RAW,
    });
    expect(cluster.id).toBe(LOWER);
    expect(cluster.representative_item_id).toBe(LOWER);
    expect(cluster.last_seen_at).toBe(TS_CANON);
    expect(
      normalizeSyncCluster({ ...cluster, representative_item_id: null }).representative_item_id,
    ).toBeNull();

    expect(
      normalizeSyncAppSettings({
        default_expiration_days: 30,
        backfill_cap: 10,
        default_poll_interval_minutes: 60,
        updated_at: TS_RAW,
      }).updated_at,
    ).toBe(TS_CANON);

    const boot = normalizeBootstrapResponse({
      instance_epoch: UPPER,
      server_now: TS_RAW,
      watermark: 5,
      categories: [cat],
      feeds: [feed],
      clusters: [cluster],
      interest_profile: null,
      app_settings: {
        default_expiration_days: 30,
        backfill_cap: 10,
        default_poll_interval_minutes: 60,
        updated_at: TS_RAW,
      },
      items: [item],
      next_cursor: UPPER,
      has_more: true,
    });
    expect(boot.instance_epoch).toBe(LOWER);
    expect(boot.next_cursor).toBe(LOWER);

    const pull = normalizePullResponse({
      resync_required: false,
      resync_reason: null,
      instance_epoch: UPPER,
      server_now: TS_RAW,
      watermark: 10,
      next_seq: 10,
      has_more: false,
      changes: [
        {
          seq: 1,
          entity_type: "category",
          entity_id: UPPER,
          op: "upsert",
          changed_at: TS_RAW,
          data: cat,
        },
        {
          seq: 2,
          entity_type: "feed",
          entity_id: UPPER,
          op: "upsert",
          changed_at: TS_RAW,
          data: feed,
        },
        {
          seq: 3,
          entity_type: "item",
          entity_id: UPPER,
          op: "upsert",
          changed_at: TS_RAW,
          data: item,
        },
        {
          seq: 4,
          entity_type: "cluster",
          entity_id: UPPER,
          op: "upsert",
          changed_at: TS_RAW,
          data: cluster,
        },
        {
          seq: 5,
          entity_type: "app_settings",
          entity_id: UPPER,
          op: "upsert",
          changed_at: TS_RAW,
          data: {
            default_expiration_days: 30,
            backfill_cap: 10,
            default_poll_interval_minutes: 60,
            updated_at: TS_RAW,
          },
        },
        {
          seq: 6,
          entity_type: "item",
          entity_id: UPPER,
          op: "delete",
          changed_at: TS_RAW,
          data: null,
        },
        {
          seq: 7,
          entity_type: "unknown_future" as unknown as "item",
          entity_id: UPPER,
          op: "upsert",
          changed_at: TS_RAW,
          data: { ok: true } as unknown as SyncFeedDTO,
        },
      ],
    });
    expect(pull.changes).toHaveLength(7);
    expect(pull.changes[0]!.changed_at).toBe(TS_CANON);

    const push = normalizePushResponse({
      instance_epoch: UPPER,
      server_now: TS_RAW,
      results: [
        {
          client_mutation_id: UPPER,
          outcome: "superseded",
          reason: null,
          affected_count: null,
          current_entity_type: "item",
          current_entity: {
            id: UPPER,
            state: "read",
            is_starred: true,
            read_at: TS_RAW,
            starred_at: TS_RAW,
            expired_at: null,
            unexpired_at: null,
            read_state_updated_at: TS_RAW,
            starred_updated_at: TS_RAW,
            updated_at: TS_RAW,
          },
        },
        {
          client_mutation_id: UPPER,
          outcome: "rejected",
          reason: "category_not_found",
          affected_count: null,
          current_entity_type: "feed",
          current_entity: {
            id: UPPER,
            status: "active",
            category_id: UPPER,
            expiration_days_override: null,
            research_policy: { enabled: false, fetch_pdfs: false, guidance: "", max_hops: 1 },
            config_version: 1,
            field_updated_at: { category_id: TS_RAW },
            updated_at: TS_RAW,
          },
        },
        {
          client_mutation_id: UPPER,
          outcome: "applied",
          reason: null,
          affected_count: null,
          current_entity_type: null,
          current_entity: null,
        },
      ],
    });
    expect(push.results[0]!.client_mutation_id).toBe(LOWER);

    const search = normalizeSearchResponse({
      query: "test",
      sort: "relevance",
      total: 1,
      limit: 20,
      offset: 0,
      results: [
        {
          rank: 1,
          snippet: [[{ text: "hi", highlight: true }]],
          item: {
            ...item,
            id: UPPER,
            feed_id: UPPER,
            cluster_id: UPPER,
          },
        },
      ],
    });
    expect(search.results[0]!.item.id).toBe(LOWER);

    const feedResp = normalizeFeedResponse({
      id: UPPER,
      category_id: UPPER,
      feed_url: "https://example.com/rss",
      site_url: "https://example.com",
      title: "Feed",
      status: "active",
      health_state: "healthy",
      poll_interval_minutes: 60,
      last_fetched_at: TS_RAW,
      last_success_at: TS_RAW,
      next_fetch_at: null,
      consecutive_errors: 0,
      last_error: null,
      initial_backfill_done: true,
      expiration_days_override: null,
      fetch_full_text: true,
      research_policy: { enabled: false, fetch_pdfs: false, guidance: "", max_hops: 1 },
      curation_instructions: "",
      field_updated_at: { title: TS_RAW },
      config_version: 1,
      created_at: TS_RAW,
      updated_at: TS_RAW,
    });
    expect(feedResp.id).toBe(LOWER);
    expect(feedResp.last_fetched_at).toBe(TS_CANON);

    expect(
      normalizeCategoryResponse({
        id: UPPER,
        name: "Cat",
        sort_order: 0,
        created_at: TS_RAW,
        updated_at: TS_RAW,
      }).created_at,
    ).toBe(TS_CANON);

    expect(
      normalizeItemResponse({
        id: UPPER,
        feed_id: UPPER,
        guid: "g1",
        raw_url: "https://example.com/1",
        canonical_url: "https://example.com/1",
        title: "T",
        author: null,
        published_at: TS_RAW,
        fetched_at: TS_RAW,
        raw_summary: null,
        extracted_text: "body",
        extraction_status: "ok",
        entry_hash: null,
        content_hash: null,
        state: "unread",
        is_starred: false,
        read_at: null,
        starred_at: null,
        expired_at: null,
        read_state_updated_at: TS_RAW,
        starred_updated_at: TS_RAW,
        unexpired_at: null,
        created_at: TS_RAW,
        updated_at: TS_RAW,
      }).fetched_at,
    ).toBe(TS_CANON);

    expect(
      normalizeDeviceResponse({
        id: UPPER,
        name: "Laptop",
        platform: "ubuntu",
        created_at: TS_RAW,
        last_seen_at: TS_RAW,
        revoked_at: null,
      }).last_seen_at,
    ).toBe(TS_CANON);

    const sysHealth = normalizeSystemHealth({
      db: "ok",
      instance_epoch: UPPER,
      current_seq: 10,
      queue_counts: { poll_feed: { queued: 1 } },
      dead_letter_count: 1,
      recent_runs: [
        {
          id: UPPER,
          trigger: "manual",
          stages: ["poll_feed"],
          status: "completed",
          started_at: TS_RAW,
          finished_at: TS_RAW,
          duration_ms: 100,
          items_polled: 1,
          items_extracted: 1,
          items_clustered: 0,
          items_researched: 0,
          items_enriched: 0,
          jobs_failed: 0,
          total_llm_ms: 0,
          notes: null,
        },
      ],
    });
    expect(sysHealth.instance_epoch).toBe(LOWER);
    expect(sysHealth.recent_runs[0]!.started_at).toBe(TS_CANON);

    const sysMetrics = normalizeSystemMetrics({
      window: "24h",
      since: TS_RAW,
      stages: [],
      per_feed: [
        {
          feed_id: UPPER,
          feed_title: "F",
          total_jobs: 1,
          failed_jobs: 0,
          avg_duration_ms: 10,
          total_duration_ms: 10,
        },
      ],
    });
    expect(sysMetrics.since).toBe(TS_CANON);
    expect(sysMetrics.per_feed[0]!.feed_id).toBe(LOWER);

    const dl = normalizeDeadLetterList({
      total: 2,
      limit: 25,
      offset: 0,
      jobs: [
        {
          id: 1,
          job_type: "poll_feed",
          dedupe_key: "k1",
          payload: {},
          feed_id: UPPER,
          item_id: UPPER,
          run_id: UPPER,
          priority: 50,
          status: "failed",
          attempts: 5,
          max_attempts: 5,
          last_error: "err",
          finished_at: TS_RAW,
          created_at: TS_RAW,
        },
        {
          id: 2,
          job_type: "poll_feed",
          dedupe_key: "k2",
          payload: {},
          feed_id: null,
          item_id: null,
          run_id: null,
          priority: 50,
          status: "failed",
          attempts: 5,
          max_attempts: 5,
          last_error: null,
          finished_at: null,
          created_at: TS_RAW,
        },
      ],
    });
    expect(dl.jobs[0]!.feed_id).toBe(LOWER);
    expect(dl.jobs[1]!.feed_id).toBeNull();
  });

  it("test_v10_sanitize_blocks_spoofed_blocked_img_class_and_origin_confirm_broadcasts", async () => {
    // 1) Untrusted HTML cannot spoof class="srss-blocked-img", while hook-created placeholder keeps it
    const frag = sanitizeFeedHtmlToFragment(
      '<span class="srss-blocked-img">Spoofed</span><img src="https://example.com/a.png" alt="Real">',
      "https://example.com",
    );
    const wrapper = document.createElement("div");
    wrapper.appendChild(frag);
    const spans = Array.from(wrapper.querySelectorAll("span"));
    const spoofed = spans.find((s) => s.textContent === "Spoofed");
    const real = spans.find((s) => s.textContent?.includes("Real"));
    expect(spoofed?.hasAttribute("class")).toBe(false);
    expect(real?.getAttribute("class")).toBe("srss-blocked-img");

    // 2) confirmOriginChange broadcasts auth_changed to peer tabs
    const received: unknown[] = [];
    const peer = new BroadcastChannel("smartrss-sync");
    peer.onmessage = (e) => received.push(e.data);

    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://old.lan:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });

    const engine = new SyncEngine({
      db,
      fetchImpl: async (input) => {
        const url = new URL(String(input), "http://localhost:4180");
        if (url.pathname === "/client-config.json") {
          return jsonResp({ mode: "localhost", api_origin: "http://new.lan:8080" });
        }
        return jsonResp({ status: "ok", db: "ok", version: "0.1.0" });
      },
      forceIdbLockFallback: true,
    });
    try {
      await engine.start();
      expect(engine.getSnapshot().connectionState).toBe("origin_confirm");
      await engine.confirmOriginChange();
      await new Promise((r) => setTimeout(r, 15));
      expect(
        received.some(
          (m) =>
            typeof m === "object" && m !== null && (m as { type?: string }).type === "auth_changed",
        ),
      ).toBe(true);
    } finally {
      engine.stop();
      peer.close();
    }
  });

  it("test_v01_client_maps_invalid_json_200_to_api_http_error_502", async () => {
    const api = new SmartRSSApiClient("http://localhost:8080", "tok", async () => {
      return new Response("<html>not json</html>", { status: 200 });
    });
    await expect(api.health()).rejects.toBeInstanceOf(ApiHttpError);
  });

  it("test_v02_omitted_results_during_bisection_and_non_error_sync_exception", async () => {
    const { itemId } = await seedPairedReady();
    await enqueueItemState(db, itemId, "read");
    await enqueueItemStarred(db, itemId, true);

    // 1) Initial 2-row push returns 422; left-half 1-row push returns 200 with omitted result -> halts sync_error!
    let calls = 0;
    const api = new SmartRSSApiClient("http://localhost:8080", "tok", async () => {
      calls += 1;
      if (calls === 1) {
        return jsonResp({ detail: "validation error" }, 422);
      }
      return jsonResp({
        instance_epoch: EPOCH_1,
        server_now: "2026-11-25T12:00:05.000Z",
        results: [],
      } satisfies SyncPushResponse);
    });
    const res = await withSyncLock(
      { db, tabId: "t-v02-bisect", forceIdbFallback: true },
      async (lock) => flushOutbox(db, api, lock),
    );
    expect(res.acquired).toBe(true);
    if (res.acquired) {
      expect(res.value.status).toBe("sync_error");
    }
    expect(calls).toBe(2);

    // 2) Non-Error thrown inside cycle and startup still lands in sync_error cleanly
    const engine = new SyncEngine({ db, forceIdbLockFallback: true });
    vi.spyOn(engine, "initializeRuntimeAndAuth").mockRejectedValueOnce("string-startup-boom");
    try {
      await expect(engine.start()).resolves.toBeUndefined();
      expect(engine.getSnapshot().connectionState).toBe("sync_error");
      expect(engine.getSnapshot().statusDetail).toBe("string-startup-boom");
    } finally {
      engine.stop();
    }

    const engine2 = new SyncEngine({
      db,
      fetchImpl: async () => {
        throw "raw-non-error-failure";
      },
      forceIdbLockFallback: true,
    });
    const putSpy = vi.spyOn(db.outbox, "count").mockRejectedValueOnce("string-cycle-boom");
    try {
      await expect(engine2.triggerSync()).resolves.toBeUndefined();
      expect(engine2.getSnapshot().connectionState).toBe("sync_error");
      expect(engine2.getSnapshot().statusDetail).toBe("string-cycle-boom");
    } finally {
      putSpy.mockRestore();
      engine2.stop();
    }
  });
});

// Ensure type import used cleanly
type SyncFeedDTO = Parameters<typeof normalizeSyncFeed>[0];
