/**
 * Tests for 1c-T04:
 * - U-02: mid-push crash recovery without `in_flight` leak
 * - S6 / P-09: FIFO chunking across 500 mutations, 5,000 explicit IDs, 1.5 MiB UTF-8 cap
 * - P-03: sync_cursor is NEVER modified from push responses
 * - U-04 / U-04-F1: systematic 422 halt, 10-row batch with pills at positions 2 & 7,
 *   lone 422 + manual quarantine, Retry, and consolidated repeated-type banner
 * - superseded / rejected reconciliation from *_base
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SmartRSSApiClient } from "../../api/client";
import { getSyncMeta, setSyncMeta } from "../../db/clientMeta";
import { SmartRSSDB } from "../../db/schema";
import type { LockHandle } from "../../sync/lock";
import {
  dismissFailedMutation,
  enqueueBulkMarkRead,
  enqueueFeedField,
  enqueueItemStarred,
  enqueueItemState,
  flushOutbox,
  manualQuarantineOutboxRow,
  packOutboxBatch,
  retryFailedMutation,
} from "../../sync/outbox";
import { applyOutboxToFeed, applyOutboxToItem } from "../../sync/reconciliation";
import type { SyncPushRequest, SyncPushResponse } from "../../types/sync";
import { makeFeed, makeLocalBase } from "../fixtures/syncFactories";

const EPOCH_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const NOOP_LOCK: LockHandle = {
  mode: "web_locks",
  ownerTabId: "tab-test",
  fencingToken: null,
  async assertAndRenewInTx() {},
  async renewBeforeIo() {},
};

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("1c-T04 outbox lifecycle, chunking, skew-safe bisection & rollback", () => {
  let db: SmartRSSDB;

  beforeEach(async () => {
    db = new SmartRSSDB(`test_t04_${Math.random().toString(36).slice(2)}`);
    await db.open();
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 50,
      active_gen: 1,
      last_sync_at: "2026-11-25T10:00:00.000Z",
      last_server_now: "2026-11-25T10:00:00.000Z",
      clock_drift_ms: 0,
    });
  });

  afterEach(async () => {
    await db.delete();
  });

  it("(a) U-02: mid-push crash preserves outbox with same client_mutation_id and increments attempts", async () => {
    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));

    const row = await enqueueItemState(db, base.id, "read", base.title);
    expect((await db.items.get(base.id))?.state).toBe("read");

    // Simulate network drop mid-push (tab crash / offline)
    const failingFetch: typeof fetch = async () => {
      throw new TypeError("Failed to fetch");
    };
    const apiFail = new SmartRSSApiClient("http://localhost:8080", "tok", failingFetch);
    const res1 = await flushOutbox(db, apiFail, NOOP_LOCK);
    expect(res1.status).toBe("detached");

    const stuckRows = await db.outbox.toArray();
    expect(stuckRows).toHaveLength(1);
    expect(stuckRows[0]!.client_mutation_id).toBe(row.client_mutation_id);
    expect(stuckRows[0]!.attempts).toBe(1);
    expect(stuckRows[0]!.last_error).toBeTruthy();

    // Next engine startup re-sends exact same client_mutation_id
    const sentIds: string[] = [];
    const okFetch: typeof fetch = async (_url, init) => {
      const req = JSON.parse(String(init?.body)) as SyncPushRequest;
      for (const m of req.mutations) sentIds.push(m.client_mutation_id);
      const body: SyncPushResponse = {
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
      };
      return jsonResponse(body);
    };
    const apiOk = new SmartRSSApiClient("http://localhost:8080", "tok", okFetch);
    const res2 = await flushOutbox(db, apiOk, NOOP_LOCK);
    expect(res2).toEqual({ status: "ok", flushedCount: 1, quarantinedCount: 0 });
    expect(sentIds).toEqual([row.client_mutation_id]);
    expect(await db.outbox.count()).toBe(0);
    // P-03: sync_cursor MUST NOT move from push
    expect((await getSyncMeta(db)).sync_cursor).toBe(50);
  });

  it("(b) packs 650 mutations and > 5,000 explicit item_ids respecting all three server caps", async () => {
    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));

    // Enqueue 650 mutations -> must flush in 2 batches (500 + 150)
    const rows = Array.from({ length: 650 }, (_, i) => ({
      client_mutation_id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
      created_at: "2026-11-25T11:00:00.000Z",
      payload: {
        type: "item_set_starred" as const,
        client_mutation_id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
        client_timestamp: "2026-11-25T11:00:00.000Z",
        item_id: base.id,
        is_starred: i % 2 === 0,
      },
      entity_key: `item:${base.id}`,
      target_absent: false,
      captured_item_ids: null,
      display_label: "Article",
      attempts: 0,
      last_error: null,
    }));
    await db.outbox.bulkAdd(rows);

    const batchSizes: number[] = [];
    const okFetch: typeof fetch = async (_url, init) => {
      const req = JSON.parse(String(init?.body)) as SyncPushRequest;
      batchSizes.push(req.mutations.length);
      return jsonResponse({
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
    };
    const api = new SmartRSSApiClient("http://localhost:8080", "tok", okFetch);
    const out = await flushOutbox(db, api, NOOP_LOCK);
    expect(out).toEqual({ status: "ok", flushedCount: 650, quarantinedCount: 0 });
    expect(batchSizes).toEqual([500, 150]);

    // Explicit item_ids cap: 6 chunks of 1,000 IDs = 6,000 IDs -> first batch capped at 5 chunks (5,000 IDs)
    const manyIds = Array.from(
      { length: 6_000 },
      (_, i) => `44444444-4444-4444-8444-${String(i).padStart(12, "0")}`,
    );
    const bulkRows = await enqueueBulkMarkRead(db, { scope: "items", itemIds: manyIds });
    expect(bulkRows).toHaveLength(6);
    const packed = packOutboxBatch(bulkRows);
    expect(packed).toHaveLength(5);
  });

  it("(c-i) U-04: systematic 422 across all requests halts in sync_error with zero quarantines/rollbacks", async () => {
    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));

    for (let i = 0; i < 8; i += 1) {
      await enqueueItemStarred(db, base.id, i % 2 === 0);
    }

    const always422: typeof fetch = async () => jsonResponse({ detail: "Schema mismatch" }, 422);
    const api = new SmartRSSApiClient("http://localhost:8080", "tok", always422);
    const res = await flushOutbox(db, api, NOOP_LOCK);
    expect(res.status).toBe("sync_error");
    expect(await db.outbox.count()).toBe(8);
    expect(await db.failed_mutations.count()).toBe(0);
  });

  it("(c-ii) U-04-F1: 10-row batch with poison pills at positions 2 and 7 quarantines ONLY those 2 and flushes the other 8", async () => {
    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));

    const rows = [];
    for (let i = 0; i < 10; i += 1) {
      rows.push(await enqueueItemStarred(db, base.id, i % 2 === 0, `Item #${i}`));
    }
    const badIds = new Set([rows[2]!.client_mutation_id, rows[7]!.client_mutation_id]);
    const appliedIds: string[] = [];

    const selective422: typeof fetch = async (_url, init) => {
      const req = JSON.parse(String(init?.body)) as SyncPushRequest;
      if (req.mutations.some((m) => badIds.has(m.client_mutation_id))) {
        return jsonResponse({ detail: "Invalid mutation payload" }, 422);
      }
      for (const m of req.mutations) appliedIds.push(m.client_mutation_id);
      return jsonResponse({
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
    };

    const api = new SmartRSSApiClient("http://localhost:8080", "tok", selective422);
    const res = await flushOutbox(db, api, NOOP_LOCK);
    expect(res).toEqual({ status: "ok", flushedCount: 8, quarantinedCount: 2 });
    expect(await db.outbox.count()).toBe(0);
    const quarantined = await db.failed_mutations.toArray();
    expect(new Set(quarantined.map((q) => q.client_mutation_id))).toEqual(badIds);
    expect(appliedIds).toHaveLength(8);

    // Consolidated notification for repeated same-type quarantines (review §10 note 1)
    const notifs = await db.sync_notifications.toArray();
    expect(notifs.some((n) => n.reason === "partial_skew:item_set_starred")).toBe(true);
  });

  it("(c-iii & iv) U-04-F1: lone 422 halts safely; manual quarantine unblocks queue; Retry re-enqueues", async () => {
    const base = makeLocalBase({ state: "unread" });
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));

    const lone = await enqueueItemState(db, base.id, "read", "Lone article");
    const always422: typeof fetch = async () => jsonResponse({ detail: "Bad single row" }, 422);
    const api422 = new SmartRSSApiClient("http://localhost:8080", "tok", always422);

    const res = await flushOutbox(db, api422, NOOP_LOCK);
    expect(res.status).toBe("sync_error");
    expect(await db.outbox.count()).toBe(1);
    expect(await db.failed_mutations.count()).toBe(0);
    expect((await db.outbox.get(lone.local_seq!))?.last_error).toContain("Bad single row");

    // User manually quarantines the head row in Outbox Inspector
    await manualQuarantineOutboxRow(db, lone.local_seq!);
    expect(await db.outbox.count()).toBe(0);
    expect(await db.failed_mutations.count()).toBe(1);
    // Display recomputed from items_base -> reverted to unread
    expect((await db.items.get(base.id))?.state).toBe("unread");

    // Retry re-enqueues with the exact same client_mutation_id
    const retried = await retryFailedMutation(db, lone.client_mutation_id);
    expect(retried).toBe(true);
    expect(await db.failed_mutations.count()).toBe(0);
    const reEnqueued = await db.outbox.toArray();
    expect(reEnqueued).toHaveLength(1);
    expect(reEnqueued[0]!.client_mutation_id).toBe(lone.client_mutation_id);
    expect((await db.items.get(base.id))?.state).toBe("read");

    // Dismiss removes quarantined row
    await manualQuarantineOutboxRow(db, reEnqueued[0]!.local_seq!);
    await dismissFailedMutation(db, lone.client_mutation_id);
    expect(await db.failed_mutations.count()).toBe(0);
  });

  it("(d) superseded merges slim current_entity into base; rejected rolls back feed + creates notification", async () => {
    const feed = makeFeed({ category_id: "11111111-1111-4111-8111-111111111111" });
    await db.feeds_base.put(feed);
    await db.feeds.put(applyOutboxToFeed(feed, []));

    const m = await enqueueFeedField(
      db,
      feed.id,
      "category_id",
      "99999999-9999-4999-8999-999999999999",
      feed.title,
    );
    expect((await db.feeds.get(feed.id))?.category_id).toBe("99999999-9999-4999-8999-999999999999");

    const rejectFetch: typeof fetch = async () =>
      jsonResponse({
        instance_epoch: EPOCH_1,
        server_now: "2026-11-25T12:00:00Z",
        results: [
          {
            client_mutation_id: m.client_mutation_id,
            outcome: "rejected",
            reason: "category_not_found",
            affected_count: null,
            current_entity_type: "feed",
            current_entity: {
              id: feed.id,
              status: feed.status,
              category_id: "11111111-1111-4111-8111-111111111111",
              expiration_days_override: null,
              research_policy: feed.research_policy,
              config_version: 1,
              field_updated_at: feed.field_updated_at,
              updated_at: "2026-11-25T12:00:00Z",
            },
          },
        ],
      } satisfies SyncPushResponse);

    const api = new SmartRSSApiClient("http://localhost:8080", "tok", rejectFetch);
    const out = await flushOutbox(db, api, NOOP_LOCK);
    expect(out.status).toBe("ok");
    expect((await db.feeds.get(feed.id))?.category_id).toBe("11111111-1111-4111-8111-111111111111");
    const notifs = await db.sync_notifications.toArray();
    expect(notifs).toHaveLength(1);
    expect(notifs[0]!.reason).toBe("category_not_found");
  });

  it("handles 401 auth_revoked and 503 Retry-After without dropping outbox", async () => {
    const base = makeLocalBase();
    await db.items_base.put(base);
    await db.items.put(applyOutboxToItem(base, []));
    await enqueueItemState(db, base.id, "read");

    const api401 = new SmartRSSApiClient("http://localhost:8080", "tok", async () =>
      jsonResponse({ detail: "Revoked" }, 401),
    );
    expect(await flushOutbox(db, api401, NOOP_LOCK)).toEqual({ status: "auth_revoked" });
    expect(await db.outbox.count()).toBe(1);

    const api503 = new SmartRSSApiClient("http://localhost:8080", "tok", async () =>
      jsonResponse({ detail: "Busy" }, 503, { "Retry-After": "2" }),
    );
    const res503 = await flushOutbox(db, api503, NOOP_LOCK);
    expect(res503.status).toBe("sync_error");
    if (res503.status === "sync_error") {
      expect(res503.retryAfterSeconds).toBe(2);
    }
    expect(await db.outbox.count()).toBe(1);
  });

  it("covers scoped category/all bulk read, 413 single quarantine, >3 quarantine cap, left-fail/right-ok, and restore drop >5000", async () => {
    const feed = makeFeed({ category_id: "11111111-1111-4111-8111-111111111111" });
    await db.feeds_base.put(feed);
    await db.feeds.put(feed);
    const item = makeLocalBase({
      feed_id: feed.id,
      ingest_sync_seq: 10,
      published_at: "2026-11-20T00:00:00.000Z",
    });
    await db.items_base.put(item);
    await db.items.put(applyOutboxToItem(item, []));

    // Scoped category bulk read with olderThanTs
    const catBulk = await enqueueBulkMarkRead(db, {
      scope: "category",
      categoryId: feed.category_id,
      olderThanTs: "2026-11-24T00:00:00.000Z",
    });
    expect(catBulk[0]!.captured_item_ids).toEqual([item.id]);

    // Empty items bulk read returns []
    expect(await enqueueBulkMarkRead(db, { scope: "items", itemIds: [] })).toEqual([]);

    // 413 on a single mutation quarantines immediately without needing skewDisproven
    await db.outbox.clear();
    await enqueueItemState(db, item.id, "unread");
    const api413 = new SmartRSSApiClient("http://localhost:8080", "tok", async () =>
      jsonResponse({ detail: "Payload too large" }, 413),
    );
    const res413 = await flushOutbox(db, api413, NOOP_LOCK);
    expect(res413).toEqual({ status: "ok", flushedCount: 0, quarantinedCount: 1 });
    await db.failed_mutations.clear();

    // Left half fails while right half succeeds -> isolates left bad row cleanly
    const r0 = await enqueueItemStarred(db, item.id, true);
    const r1 = await enqueueItemStarred(db, item.id, false);
    const leftBadFetch: typeof fetch = async (_url, init) => {
      const req = JSON.parse(String(init?.body)) as SyncPushRequest;
      if (req.mutations.some((m) => m.client_mutation_id === r0.client_mutation_id)) {
        return jsonResponse({ detail: "Bad r0" }, 422);
      }
      return jsonResponse({
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
    };
    const resLeftBad = await flushOutbox(
      db,
      new SmartRSSApiClient("http://localhost:8080", "tok", leftBadFetch),
      NOOP_LOCK,
    );
    expect(resLeftBad).toEqual({ status: "ok", flushedCount: 1, quarantinedCount: 1 });
    expect(await db.failed_mutations.get(r0.client_mutation_id)).toBeDefined();
    expect(await db.replay_journal.get(r1.client_mutation_id)).toBeDefined();

    // Manual quarantine & retry on missing IDs are safe no-ops
    expect(await manualQuarantineOutboxRow(db, 999999)).toEqual([]);
    expect(await retryFailedMutation(db, "missing-id")).toBe(false);

    // > 5,000 captured_item_ids scoped bulk read is dropped with notification on restore
    const { prepareOutboxForRestore } = await import("../../sync/outbox");
    const hugeIds = Array.from(
      { length: 5_001 },
      (_, i) => `55555555-5555-4555-8555-${String(i).padStart(12, "0")}`,
    );
    await db.outbox.add({
      client_mutation_id: "66666666-6666-4666-8666-666666666666",
      created_at: "2026-11-25T11:00:00.000Z",
      payload: {
        type: "item_mark_bulk_read",
        client_mutation_id: "66666666-6666-4666-8666-666666666666",
        client_timestamp: "2026-11-25T11:00:00.000Z",
        scope: "all",
        up_to_sync_seq: 50,
        epoch: EPOCH_1,
      },
      entity_key: "bulk",
      target_absent: false,
      captured_item_ids: hugeIds,
      display_label: "Mark all read",
      attempts: 0,
      last_error: null,
    });
    const prep = await prepareOutboxForRestore(db, NOOP_LOCK);
    expect(prep.droppedScopedCount).toBe(1);
    expect(
      (await db.sync_notifications.toArray()).some((n) => n.reason === "restore_bulk_dropped"),
    ).toBe(true);

    // Scoped bulk read before bootstrap throws cleanly
    await setSyncMeta(db, {
      instance_epoch: null,
      sync_cursor: null,
      active_gen: 0,
      last_sync_at: null,
      last_server_now: null,
      clock_drift_ms: 0,
    });
    await expect(enqueueBulkMarkRead(db, { scope: "all" })).rejects.toThrow("bootstrap");

    // Restore cursor + test > MAX_QUARANTINES_PER_CYCLE (3) halts in sync_error
    await setSyncMeta(db, {
      instance_epoch: EPOCH_1,
      sync_cursor: 50,
      active_gen: 1,
      last_sync_at: "2026-11-25T10:00:00.000Z",
      last_server_now: "2026-11-25T10:00:00.000Z",
      clock_drift_ms: 0,
    });
    await db.outbox.clear();
    await db.failed_mutations.clear();

    const goodRow = await enqueueItemStarred(db, item.id, true);
    const bad4 = [];
    for (let i = 0; i < 4; i += 1) {
      bad4.push(await enqueueFeedField(db, feed.id, "status", "paused"));
    }
    const bad4Ids = new Set(bad4.map((b) => b.client_mutation_id));
    const capFetch: typeof fetch = async (_url, init) => {
      const req = JSON.parse(String(init?.body)) as SyncPushRequest;
      if (req.mutations.some((m) => bad4Ids.has(m.client_mutation_id))) {
        return jsonResponse({ detail: "Bad feed field" }, 422);
      }
      return jsonResponse({
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
    };
    const capRes = await flushOutbox(
      db,
      new SmartRSSApiClient("http://localhost:8080", "tok", capFetch),
      NOOP_LOCK,
    );
    // Good row flushed (disproving skew), 3 bad rows quarantined, 4th hits cap -> sync_error
    expect(capRes.status).toBe("sync_error");
    expect(await db.failed_mutations.count()).toBe(3);
    expect(await db.outbox.count()).toBe(1);
    expect(await db.replay_journal.get(goodRow.client_mutation_id)).toBeDefined();

    // Retry & manual quarantine on feed mutation recomputes feeds display cleanly
    const lastRow = (await db.outbox.toArray())[0]!;
    await manualQuarantineOutboxRow(db, lastRow.local_seq!);
    expect(await retryFailedMutation(db, lastRow.client_mutation_id)).toBe(true);

    // Superseded item mutation merges slim snapshot into items_base
    await db.outbox.clear();
    const mSup = await enqueueItemState(db, item.id, "read");
    const supFetch: typeof fetch = async () =>
      jsonResponse({
        instance_epoch: EPOCH_1,
        server_now: "2026-11-25T12:00:00Z",
        results: [
          {
            client_mutation_id: mSup.client_mutation_id,
            outcome: "superseded",
            reason: null,
            affected_count: null,
            current_entity_type: "item",
            current_entity: {
              id: item.id,
              state: "unread",
              is_starred: true,
              read_at: null,
              starred_at: "2026-11-25T12:00:00Z",
              expired_at: null,
              unexpired_at: null,
              read_state_updated_at: "2026-11-25T12:00:00Z",
              starred_updated_at: "2026-11-25T12:00:00Z",
              updated_at: "2026-11-25T12:00:00Z",
            },
          },
        ],
      } satisfies SyncPushResponse);
    await flushOutbox(
      db,
      new SmartRSSApiClient("http://localhost:8080", "tok", supFetch),
      NOOP_LOCK,
    );
    expect((await db.items.get(item.id))?.is_starred).toBe(true);
    expect((await db.items.get(item.id))?.state).toBe("unread");

    // prepareOutboxForRestore rewrites a valid <=5000 scoped bulk read into chunked scope='items'
    // and mintFreshMutationIdsOnRepair refreshes both outbox and replay_journal IDs (U-11)
    const { mintFreshMutationIdsOnRepair } = await import("../../sync/outbox");
    await db.outbox.clear();
    await enqueueBulkMarkRead(db, { scope: "feed", feedId: feed.id });
    const prepRewrite = await prepareOutboxForRestore(db, NOOP_LOCK);
    expect(prepRewrite.rewrittenScopedCount).toBe(1);
    const outboxAfterRewrite = await db.outbox.toArray();
    expect(
      outboxAfterRewrite.some(
        (r) => r.payload.type === "item_mark_bulk_read" && r.payload.scope === "items",
      ),
    ).toBe(true);

    const oldJournalIds = new Set(
      (await db.replay_journal.toArray()).map((j) => j.client_mutation_id),
    );
    await mintFreshMutationIdsOnRepair(db);
    const newJournalIds = (await db.replay_journal.toArray()).map((j) => j.client_mutation_id);
    expect(newJournalIds.length).toBeGreaterThan(0);
    for (const id of newJournalIds) {
      expect(oldJournalIds.has(id)).toBe(false);
    }

    // 1.5 MiB UTF-8 byte cap in packOutboxBatch (S6)
    const hugeLabelRows = Array.from({ length: 3 }, (_, idx) => {
      const ids = Array.from(
        { length: 1_000 },
        (_, j) => `${String(idx).padStart(8, "0")}-0000-4000-8000-${String(j).padStart(12, "0")}`,
      );
      return {
        local_seq: idx + 1,
        client_mutation_id: `77777777-7777-4777-8777-00000000000${idx}`,
        created_at: "2026-11-25T11:00:00.000Z",
        payload: {
          type: "item_mark_bulk_read" as const,
          client_mutation_id: `77777777-7777-4777-8777-00000000000${idx}`,
          client_timestamp: "2026-11-25T11:00:00.000Z",
          scope: "items" as const,
          item_ids: ids,
        },
        entity_key: "bulk",
        target_absent: false,
        captured_item_ids: null,
        display_label: "Big",
        attempts: 0,
        last_error: null,
      };
    });
    expect(packOutboxBatch(hugeLabelRows).length).toBe(3);
    // Custom tight byte cap forces single-item chunking
    expect(packOutboxBatch(hugeLabelRows, 50_000).length).toBe(1);

    // scope='items' updates existing items_base rows optimistically & archive target_absent applied fires callback
    const updatedBulk = await enqueueBulkMarkRead(db, { scope: "items", itemIds: [item.id] });
    expect(updatedBulk).toHaveLength(1);
    expect((await db.items.get(item.id))?.state).toBe("read");

    await db.outbox.clear();
    const absentRow = await enqueueItemState(
      db,
      "99999999-9999-4999-8999-999999999911",
      "unread",
      "Archived item",
    );
    expect(absentRow.target_absent).toBe(true);
    let appliedArchiveId: string | null = null;
    await flushOutbox(
      db,
      new SmartRSSApiClient("http://localhost:8080", "tok", async () =>
        jsonResponse({
          instance_epoch: EPOCH_1,
          server_now: "2026-11-25T12:00:00Z",
          results: [
            {
              client_mutation_id: absentRow.client_mutation_id,
              outcome: "applied",
              reason: null,
              affected_count: null,
              current_entity_type: null,
              current_entity: null,
            },
          ],
        } satisfies SyncPushResponse),
      ),
      NOOP_LOCK,
      {
        onArchiveMutationResolved: (id, outcome) => {
          if (outcome === "applied") appliedArchiveId = id;
        },
      },
    );
    expect(appliedArchiveId).toBe("99999999-9999-4999-8999-999999999911");
  });
});
