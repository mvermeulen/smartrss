/**
 * Tests for 1c-T03:
 * - Server-base + overlay reconciliation (`U-01`) + fast-check property test
 * - Numeric `starred_num` compound index in fake-indexeddb (`U-03`)
 * - 3-format timestamp normalization & sort order (`U-07`)
 * - Local 14-day eviction & outbox protection (`data-model.md` §6.2)
 * - Dexie `clientMeta` helpers & `clearAllLocalData`
 */

import Dexie from "dexie";
import fc from "fast-check";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { normalizeSyncFeed, normalizeSyncItem, toCanonicalIso } from "../../api/normalize";
import {
  getAppSettingsMeta,
  getAuthMeta,
  getInterestProfileMeta,
  getStorageEstimate,
  getSyncMeta,
  getUiPrefs,
  requestStoragePersistence,
  setAppSettingsMeta,
  setAuthMeta,
  setInterestProfileMeta,
  setSyncMeta,
  setUiPrefs,
} from "../../db/clientMeta";
import {
  attachVersionChangeHandler,
  clearAllLocalData,
  SmartRSSDB,
  type OutboxRow,
} from "../../db/schema";
import {
  applyOutboxToFeed,
  applyOutboxToItem,
  computeClockDriftMs,
  evictStaleLocalItems,
  humanizeRejectionReason,
  isClockDriftExcessive,
  isItemOutOfWindow,
  mergeFeedSnapshotIntoBase,
  mergeItemSnapshotIntoBase,
} from "../../sync/reconciliation";
import { makeFeed, makeLocalBase, makeLocalItem, makeSyncItem } from "../fixtures/syncFactories";

describe("1c-T03 reconciliation, Dexie indexes, timestamp normalization & eviction", () => {
  let db: SmartRSSDB;

  beforeEach(async () => {
    db = new SmartRSSDB(`test_t03_${Math.random().toString(36).slice(2)}`);
    await db.open();
  });

  afterEach(async () => {
    await db.delete();
  });

  it("(a) fast-check property: interleaved pulls and outbox removals always satisfy display === base ⊕ outbox", () => {
    const mutationArb = fc.oneof(
      fc.record({
        kind: fc.constant("state" as const),
        state: fc.constantFrom("read" as const, "unread" as const),
        ts: fc.integer({ min: 1_700_000_000_000, max: 1_800_000_000_000 }),
      }),
      fc.record({
        kind: fc.constant("star" as const),
        is_starred: fc.boolean(),
        ts: fc.integer({ min: 1_700_000_000_000, max: 1_800_000_000_000 }),
      }),
    );

    fc.assert(
      fc.property(
        fc.boolean(),
        fc.constantFrom("unread" as const, "read" as const, "expired" as const),
        fc.array(mutationArb, { minLength: 0, maxLength: 10 }),
        fc.integer({ min: 0, max: 10 }),
        (baseStarred, baseState, ops, removeIdx) => {
          const base = makeLocalBase({
            state: baseState,
            is_starred: baseStarred,
            expired_at: baseState === "expired" ? "2026-11-20T00:00:00.000Z" : null,
          });
          const outboxRows: OutboxRow[] = ops.map((op, idx) => {
            const iso = new Date(op.ts).toISOString();
            const payload =
              op.kind === "state"
                ? {
                    type: "item_set_state" as const,
                    client_mutation_id: `00000000-0000-4000-8000-${String(idx).padStart(12, "0")}`,
                    client_timestamp: iso,
                    item_id: base.id,
                    state: op.state,
                  }
                : {
                    type: "item_set_starred" as const,
                    client_mutation_id: `00000000-0000-4000-8000-${String(idx).padStart(12, "0")}`,
                    client_timestamp: iso,
                    item_id: base.id,
                    is_starred: op.is_starred,
                  };
            return {
              local_seq: idx + 1,
              client_mutation_id: payload.client_mutation_id,
              created_at: iso,
              payload,
              entity_key: `item:${base.id}`,
              target_absent: false,
              captured_item_ids: null,
              display_label: "Article",
              attempts: 0,
              last_error: null,
            };
          });

          // Simulate removing one outbox row (e.g. rollback/quarantine) and pulling a newer base
          const remaining = outboxRows.filter((_, i) => i !== removeIdx);
          const newerBase = makeLocalBase({
            state: baseState,
            is_starred: !baseStarred,
            expired_at: baseState === "expired" ? "2026-11-20T00:00:00.000Z" : null,
          });

          // Recomputing from shuffled remaining rows must be identical and deterministic
          const d1 = applyOutboxToItem(newerBase, remaining);
          const d2 = applyOutboxToItem(newerBase, [...remaining].reverse());
          expect(d1).toEqual(d2);
          expect(d1.starred_num).toBe(d1.is_starred ? 1 : 0);

          // If no star mutation remains, display star MUST equal newerBase.is_starred (U-01)
          const hasStarOp = remaining.some((r) => r.payload.type === "item_set_starred");
          if (!hasStarOp) {
            expect(d1.is_starred).toBe(newerBase.is_starred);
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("(b) U-01 regressions: 503->pull(starred)->422 rollback keeps star; rolling back M1 keeps M2 star", () => {
    const base0 = makeLocalBase({ state: "unread", is_starred: false });
    const m1Read: OutboxRow = {
      local_seq: 1,
      client_mutation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      created_at: "2026-11-25T11:00:00.000Z",
      payload: {
        type: "item_set_state",
        client_mutation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        client_timestamp: "2026-11-25T11:00:00.000Z",
        item_id: base0.id,
        state: "read",
      },
      entity_key: `item:${base0.id}`,
      target_absent: false,
      captured_item_ids: null,
      display_label: "Article",
      attempts: 0,
      last_error: null,
    };

    // Pull delivers server star while M1 is pending
    const base1 = makeLocalBase({
      state: "unread",
      is_starred: true,
      starred_at: "2026-11-25T11:05:00.000Z",
    });
    const whilePending = applyOutboxToItem(base1, [m1Read]);
    expect(whilePending.state).toBe("read");
    expect(whilePending.is_starred).toBe(true);

    // M1 is quarantined -> recompute from base1 keeps server star!
    const afterQuarantine = applyOutboxToItem(base1, []);
    expect(afterQuarantine.state).toBe("unread");
    expect(afterQuarantine.is_starred).toBe(true);

    // Cross-field rollback: M1 (read) + M2 (star) pending; rolling back M1 preserves M2!
    const m2Star: OutboxRow = {
      local_seq: 2,
      client_mutation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      created_at: "2026-11-25T11:01:00.000Z",
      payload: {
        type: "item_set_starred",
        client_mutation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        client_timestamp: "2026-11-25T11:01:00.000Z",
        item_id: base0.id,
        is_starred: true,
      },
      entity_key: `item:${base0.id}`,
      target_absent: false,
      captured_item_ids: null,
      display_label: "Article",
      attempts: 0,
      last_error: null,
    };
    const afterM1Rollback = applyOutboxToItem(base0, [m2Star]);
    expect(afterM1Rollback.state).toBe("unread");
    expect(afterM1Rollback.is_starred).toBe(true);
    expect(afterM1Rollback.starred_num).toBe(1);
  });

  it("(c) scoped bulk-read overlay marks ONLY captured_item_ids, never newly pulled items (U-15)", () => {
    const oldItem = makeLocalBase({ id: "33333333-3333-4333-8333-333333333301", state: "unread" });
    const newPulledItem = makeLocalBase({
      id: "33333333-3333-4333-8333-333333333302",
      state: "unread",
      ingest_sync_seq: 99,
    });
    const scopedBulk: OutboxRow = {
      local_seq: 1,
      client_mutation_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      created_at: "2026-11-25T11:00:00.000Z",
      payload: {
        type: "item_mark_bulk_read",
        client_mutation_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        client_timestamp: "2026-11-25T11:00:00.000Z",
        scope: "feed",
        feed_id: oldItem.feed_id,
        up_to_sync_seq: 10,
        epoch: "99999999-9999-4999-8999-999999999999",
      },
      entity_key: "bulk",
      target_absent: false,
      captured_item_ids: [oldItem.id],
      display_label: "Mark feed read",
      attempts: 0,
      last_error: null,
    };

    expect(applyOutboxToItem(oldItem, [scopedBulk]).state).toBe("read");
    expect(applyOutboxToItem(newPulledItem, [scopedBulk]).state).toBe("unread");
  });

  it("(d) U-03: starred_num and [starred_num+effective_date] compound index work in fake-indexeddb", async () => {
    const starred1 = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333311",
      is_starred: true,
      published_at: "2026-11-25T08:00:00.000Z",
    });
    const starred2 = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333312",
      is_starred: true,
      published_at: "2026-11-25T12:00:00.000Z",
    });
    const unstarred = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333313",
      is_starred: false,
      published_at: "2026-11-25T11:00:00.000Z",
    });

    await db.items.bulkPut([starred1, starred2, unstarred]);

    const starredRows = await db.items
      .where("[starred_num+effective_date]")
      .between([1, Dexie.minKey], [1, Dexie.maxKey])
      .reverse()
      .toArray();
    expect(starredRows.map((r) => r.id)).toEqual([starred2.id, starred1.id]);

    const countStarred = await db.items.where("starred_num").equals(1).count();
    expect(countStarred).toBe(2);
  });

  it("(e) U-07: toCanonicalIso normalizes Z, 6-digit fractional, and +00:00 into lexicographically sortable UTC", () => {
    const whole = toCanonicalIso("2026-11-25T12:00:00Z");
    const micro = toCanonicalIso("2026-11-25T12:00:00.500000Z");
    const offset = toCanonicalIso("2026-11-25T14:00:00+02:00");
    expect(whole).toBe("2026-11-25T12:00:00.000Z");
    expect(micro).toBe("2026-11-25T12:00:00.500Z");
    expect(offset).toBe("2026-11-25T12:00:00.000Z");
    expect(whole < micro).toBe(true);

    const feed = normalizeSyncFeed(
      makeFeed({
        field_updated_at: { status: "2026-11-25T12:00:00+00:00" },
        updated_at: "2026-11-25T12:00:00Z",
      }),
    );
    expect(feed.field_updated_at.status).toBe("2026-11-25T12:00:00.000Z");
    expect(feed.updated_at).toBe("2026-11-25T12:00:00.000Z");

    const item = normalizeSyncItem(
      makeSyncItem({
        published_at: "2026-11-25T12:00:00.123456Z",
        fetched_at: "2026-11-25T12:00:00Z",
      }),
    );
    expect(item.published_at).toBe("2026-11-25T12:00:00.123Z");
    expect(item.fetched_at).toBe("2026-11-25T12:00:00.000Z");
    expect(() => toCanonicalIso("not-a-date")).toThrow();
  });

  it("(f) eviction deletes expired/>14d read unstarred items while protecting starred and outbox-referenced items", async () => {
    const nowMs = Date.parse("2026-11-25T12:00:00.000Z");
    const oldRead = makeLocalBase({
      id: "33333333-3333-4333-8333-333333333321",
      state: "read",
      read_at: "2026-11-01T00:00:00.000Z",
      is_starred: false,
    });
    const recentRead = makeLocalBase({
      id: "33333333-3333-4333-8333-333333333322",
      state: "read",
      read_at: "2026-11-20T00:00:00.000Z",
      is_starred: false,
    });
    const expiredUnstarred = makeLocalBase({
      id: "33333333-3333-4333-8333-333333333323",
      state: "expired",
      expired_at: "2026-11-24T00:00:00.000Z",
      is_starred: false,
    });
    const expiredStarred = makeLocalBase({
      id: "33333333-3333-4333-8333-333333333324",
      state: "expired",
      expired_at: "2026-11-24T00:00:00.000Z",
      is_starred: true,
    });
    const oldReadProtected = makeLocalBase({
      id: "33333333-3333-4333-8333-333333333325",
      state: "read",
      read_at: "2026-11-01T00:00:00.000Z",
      is_starred: false,
    });

    const allBases = [oldRead, recentRead, expiredUnstarred, expiredStarred, oldReadProtected];
    await db.items_base.bulkPut(allBases);
    await db.items.bulkPut(allBases.map((b) => applyOutboxToItem(b, [])));

    await db.outbox.add({
      client_mutation_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
      created_at: "2026-11-25T11:59:00.000Z",
      payload: {
        type: "item_set_starred",
        client_mutation_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        client_timestamp: "2026-11-25T11:59:00.000Z",
        item_id: oldReadProtected.id,
        is_starred: true,
      },
      entity_key: `item:${oldReadProtected.id}`,
      target_absent: false,
      captured_item_ids: null,
      display_label: "Protected",
      attempts: 0,
      last_error: null,
    });

    const evicted = await evictStaleLocalItems(db, nowMs);
    expect(new Set(evicted)).toEqual(new Set([oldRead.id, expiredUnstarred.id]));

    const remainingIds = (await db.items.toArray()).map((i) => i.id);
    expect(new Set(remainingIds)).toEqual(
      new Set([recentRead.id, expiredStarred.id, oldReadProtected.id]),
    );
    expect(await db.items_base.count()).toBe(3);

    expect(isItemOutOfWindow(oldRead, nowMs)).toBe(true);
    expect(isItemOutOfWindow(recentRead, nowMs)).toBe(false);
    expect(isItemOutOfWindow(expiredStarred, nowMs)).toBe(false);
  });

  it("covers feed overlay, snapshot merges, clock drift, clientMeta helpers & rejection messages", async () => {
    const feed = makeFeed();
    const overlaid = applyOutboxToFeed(feed, [
      {
        local_seq: 1,
        client_mutation_id: "10000000-0000-4000-8000-000000000001",
        created_at: "2026-11-25T11:00:00.000Z",
        payload: {
          type: "feed_set_field",
          client_mutation_id: "10000000-0000-4000-8000-000000000001",
          client_timestamp: "2026-11-25T11:00:00.000Z",
          feed_id: feed.id,
          field: "status",
          value: "muted",
        },
        entity_key: `feed:${feed.id}`,
        target_absent: false,
        captured_item_ids: null,
        display_label: "Feed",
        attempts: 0,
        last_error: null,
      },
      {
        local_seq: 2,
        client_mutation_id: "10000000-0000-4000-8000-000000000002",
        created_at: "2026-11-25T11:01:00.000Z",
        payload: {
          type: "feed_set_field",
          client_mutation_id: "10000000-0000-4000-8000-000000000002",
          client_timestamp: "2026-11-25T11:01:00.000Z",
          feed_id: feed.id,
          field: "research_policy.fetch_pdfs",
          value: true,
        },
        entity_key: `feed:${feed.id}`,
        target_absent: false,
        captured_item_ids: null,
        display_label: "Feed",
        attempts: 0,
        last_error: null,
      },
      {
        local_seq: 3,
        client_mutation_id: "10000000-0000-4000-8000-000000000003",
        created_at: "2026-11-25T11:02:00.000Z",
        payload: {
          type: "feed_set_field",
          client_mutation_id: "10000000-0000-4000-8000-000000000003",
          client_timestamp: "2026-11-25T11:02:00.000Z",
          feed_id: feed.id,
          field: "expiration_days_override",
          value: 0,
        },
        entity_key: `feed:${feed.id}`,
        target_absent: false,
        captured_item_ids: null,
        display_label: "Feed",
        attempts: 0,
        last_error: null,
      },
    ]);
    expect(overlaid.status).toBe("muted");
    expect(overlaid.research_policy.fetch_pdfs).toBe(true);
    expect(overlaid.expiration_days_override).toBe(0);

    const mergedFeed = mergeFeedSnapshotIntoBase(feed, {
      id: feed.id,
      status: "paused",
      category_id: null,
      expiration_days_override: 7,
      research_policy: { enabled: true, fetch_pdfs: false, guidance: "", max_hops: 1 },
      config_version: 2,
      field_updated_at: { status: "2026-11-25T12:00:00.000Z" },
      updated_at: "2026-11-25T12:00:00.000Z",
    });
    expect(mergedFeed.status).toBe("paused");
    expect(mergedFeed.title).toBe(feed.title);

    const baseItem = makeLocalBase();
    const mergedItem = mergeItemSnapshotIntoBase(baseItem, {
      id: baseItem.id,
      state: "read",
      is_starred: true,
      read_at: "2026-11-25T12:00:00.000Z",
      starred_at: "2026-11-25T12:00:00.000Z",
      expired_at: null,
      unexpired_at: null,
      read_state_updated_at: "2026-11-25T12:00:00.000Z",
      starred_updated_at: "2026-11-25T12:00:00.000Z",
      updated_at: "2026-11-25T12:00:00.000Z",
    });
    expect(mergedItem.state).toBe("read");
    expect(mergedItem.raw_summary).toBe(baseItem.raw_summary);

    const drift = computeClockDriftMs(
      "2026-11-25T12:00:00.000Z",
      Date.parse("2026-11-25T12:10:00.000Z"),
    );
    expect(isClockDriftExcessive(drift)).toBe(true);
    expect(computeClockDriftMs("invalid")).toBe(0);

    expect(humanizeRejectionReason("category_not_found")).toContain("category");
    expect(humanizeRejectionReason("clock_skew")).toContain("90 days");
    expect(humanizeRejectionReason("epoch_mismatch")).toContain("restored");
    expect(humanizeRejectionReason("entity_not_found")).toContain("no longer exists");
    expect(humanizeRejectionReason("invalid_field_value")).toContain("invalid");
    expect(humanizeRejectionReason("mutation_id_conflict")).toContain("Duplicate");
    expect(humanizeRejectionReason("unsupported_mutation_type")).toContain("does not support");
    expect(humanizeRejectionReason("other_err")).toContain("other_err");
    expect(humanizeRejectionReason(null)).toContain("Server rejected");

    // clientMeta round-trips
    expect(await getAuthMeta(db)).toBeNull();
    await setAuthMeta(db, {
      device_id: "d1",
      device_name: "Laptop",
      platform: "ubuntu",
      bearer_token: "tok",
      api_origin: "http://localhost:8080",
      paired_at: "2026-11-25T10:00:00.000Z",
    });
    expect((await getAuthMeta(db))?.device_name).toBe("Laptop");

    await setSyncMeta(db, {
      instance_epoch: "e1",
      sync_cursor: 42,
      active_gen: 1,
      last_sync_at: "2026-11-25T10:00:00.000Z",
      last_server_now: "2026-11-25T10:00:00.000Z",
      clock_drift_ms: 0,
    });
    expect((await getSyncMeta(db)).sync_cursor).toBe(42);

    await setAppSettingsMeta(db, {
      default_expiration_days: 30,
      backfill_cap: 10,
      default_poll_interval_minutes: 60,
      updated_at: "2026-11-25T10:00:00.000Z",
    });
    expect((await getAppSettingsMeta(db))?.backfill_cap).toBe(10);

    await setInterestProfileMeta(db, { version: 1, profile_markdown: "# AI" });
    expect((await getInterestProfileMeta(db))?.version).toBe(1);

    await setUiPrefs(db, {
      theme: "dark",
      default_sort: "date",
      density: "compact",
      auto_mark_read_on_select: true,
    });
    expect((await getUiPrefs(db)).theme).toBe("dark");

    expect(await requestStoragePersistence()).toBe("unsupported");
    expect(await getStorageEstimate()).toEqual({ usageBytes: null, quotaBytes: null });

    // Test navigator.storage.persist() & estimate() branches
    const origStorage = navigator.storage;
    Object.defineProperty(navigator, "storage", {
      value: {
        persisted: async () => true,
        persist: async () => true,
        estimate: async () => ({ usage: 1024, quota: 4096 }),
      },
      configurable: true,
    });
    expect(await requestStoragePersistence()).toBe("granted");
    expect(await getStorageEstimate()).toEqual({ usageBytes: 1024, quotaBytes: 4096 });

    Object.defineProperty(navigator, "storage", {
      value: {
        persisted: async () => false,
        persist: async () => false,
        estimate: async () => {
          throw new Error("quota error");
        },
      },
      configurable: true,
    });
    expect(await requestStoragePersistence()).toBe("denied");
    expect(await getStorageEstimate()).toEqual({ usageBytes: null, quotaBytes: null });

    Object.defineProperty(navigator, "storage", {
      value: {
        persisted: async () => false,
        persist: async () => true,
      },
      configurable: true,
    });
    expect(await requestStoragePersistence()).toBe("granted");

    Object.defineProperty(navigator, "storage", {
      value: {
        persist: async () => {
          throw new Error("denied");
        },
      },
      configurable: true,
    });
    expect(await requestStoragePersistence()).toBe("denied");
    Object.defineProperty(navigator, "storage", { value: origStorage, configurable: true });

    // Expired -> unread sets unexpired_at; scope='items' excludes expired (RR-2)
    const expBase = makeLocalBase({
      state: "expired",
      expired_at: "2026-11-20T00:00:00.000Z",
    });
    const expBulk = applyOutboxToItem(expBase, [
      {
        local_seq: 1,
        client_mutation_id: "10000000-0000-4000-8000-000000000011",
        created_at: "2026-11-25T11:00:00.000Z",
        payload: {
          type: "item_mark_bulk_read",
          client_mutation_id: "10000000-0000-4000-8000-000000000011",
          client_timestamp: "2026-11-25T11:00:00.000Z",
          scope: "items",
          item_ids: [expBase.id],
        },
        entity_key: "bulk",
        target_absent: false,
        captured_item_ids: null,
        display_label: "Bulk",
        attempts: 0,
        last_error: null,
      },
    ]);
    expect(expBulk.state).toBe("expired");

    const restored = applyOutboxToItem(expBase, [
      {
        local_seq: 2,
        client_mutation_id: "10000000-0000-4000-8000-000000000012",
        created_at: "2026-11-25T11:05:00.000Z",
        payload: {
          type: "item_set_state",
          client_mutation_id: "10000000-0000-4000-8000-000000000012",
          client_timestamp: "2026-11-25T11:05:00.000Z",
          item_id: expBase.id,
          state: "unread",
        },
        entity_key: `item:${expBase.id}`,
        target_absent: false,
        captured_item_ids: null,
        display_label: "Restore",
        attempts: 0,
        last_error: null,
      },
    ]);
    expect(restored.state).toBe("unread");
    expect(restored.unexpired_at).toBe("2026-11-25T11:05:00.000Z");

    // Feed category_id & research_policy.enabled overlay
    const feedOverlaid2 = applyOutboxToFeed(feed, [
      {
        local_seq: 1,
        client_mutation_id: "10000000-0000-4000-8000-000000000013",
        created_at: "2026-11-25T11:00:00.000Z",
        payload: {
          type: "feed_set_field",
          client_mutation_id: "10000000-0000-4000-8000-000000000013",
          client_timestamp: "2026-11-25T11:00:00.000Z",
          feed_id: feed.id,
          field: "category_id",
          value: null,
        },
        entity_key: `feed:${feed.id}`,
        target_absent: false,
        captured_item_ids: null,
        display_label: "Feed",
        attempts: 0,
        last_error: null,
      },
      {
        local_seq: 2,
        client_mutation_id: "10000000-0000-4000-8000-000000000014",
        created_at: "2026-11-25T11:01:00.000Z",
        payload: {
          type: "feed_set_field",
          client_mutation_id: "10000000-0000-4000-8000-000000000014",
          client_timestamp: "2026-11-25T11:01:00.000Z",
          feed_id: feed.id,
          field: "research_policy.enabled",
          value: true,
        },
        entity_key: `feed:${feed.id}`,
        target_absent: false,
        captured_item_ids: null,
        display_label: "Feed",
        attempts: 0,
        last_error: null,
      },
    ]);
    expect(feedOverlaid2.category_id).toBeNull();
    expect(feedOverlaid2.research_policy.enabled).toBe(true);

    let vcFired = false;
    attachVersionChangeHandler(db, () => {
      vcFired = true;
    });
    await clearAllLocalData(db);
    expect(await getAuthMeta(db)).toBeNull();
    expect(vcFired).toBe(false);
  });
});
