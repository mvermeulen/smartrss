/**
 * Persistent offline mutation outbox, FIFO chunking, skew-safe poison-pill bisection,
 * 48-hour replay journal, and re-pair ID minting (1c §6.4–§6.7, U-01, U-02, U-04-F1, U-10, U-11, U-12).
 */

import { ApiHttpError, ApiNetworkError, type SmartRSSApiClient } from "../api/client";
import { getSyncMeta, setSyncMeta } from "../db/clientMeta";
import type { FailedMutationRow, OutboxRow, ReplayJournalRow, SmartRSSDB } from "../db/schema";
import { uuidv4 } from "../security/uuid";
import type {
  BulkReadScope,
  FeedMutableField,
  ItemMarkBulkReadMutation,
  SyncMutationInput,
  SyncPushFeedSnapshot,
  SyncPushItemSnapshot,
  SyncPushResponse,
} from "../types/sync";
import type { LockHandle } from "./lock";
import {
  applyOutboxToFeed,
  applyOutboxToItem,
  buildChunkedItemsMutations,
  BULK_ITEMS_CHUNK_SIZE,
  computeClockDriftMs,
  humanizeRejectionReason,
  mergeFeedSnapshotIntoBase,
  mergeItemSnapshotIntoBase,
} from "./reconciliation";

export const MAX_BATCH_MUTATIONS = 500;
export const MAX_BATCH_EXPLICIT_ITEM_IDS = 5_000;
export const MAX_BATCH_UTF8_BYTES = 1_572_864; // 1.5 MiB (< server 2 MiB 413 cap)
export const MAX_QUARANTINES_PER_CYCLE = 3;
export const REPLAY_JOURNAL_TTL_MS = 48 * 3_600_000;

export interface PushObserverCallbacks {
  onServerNow?: (serverNowIso: string, driftMs: number) => void;
  onArchiveMutationResolved?: (
    itemId: string,
    outcome: "applied" | "superseded" | "rejected",
    snapshot: SyncPushItemSnapshot | null,
  ) => void;
  onItemsChanged?: (itemIds: string[]) => void;
  now?: () => number;
}

export type FlushOutcome =
  | { status: "ok"; flushedCount: number; quarantinedCount: number }
  | { status: "auth_revoked" }
  | { status: "detached" }
  | { status: "sync_error"; message: string; retryAfterSeconds?: number | null };

const textEncoder = new TextEncoder();
const PUSH_WRAPPER_UTF8_BYTES = textEncoder.encode('{"mutations":[]}').byteLength;

export function measurePushUtf8Bytes(mutations: readonly SyncMutationInput[]): number {
  return textEncoder.encode(JSON.stringify({ mutations })).byteLength;
}

/**
 * Pack pending outbox rows into a single FIFO batch respecting all 3 server caps:
 * <= 500 mutations, <= 5,000 explicit item_ids, <= 1.5 MiB UTF-8 serialized JSON (S6, P-09).
 * Runs in O(n) by tracking running UTF-8 byte length incrementally (V-10).
 */
export function packOutboxBatch(
  rows: readonly OutboxRow[],
  maxBytes = MAX_BATCH_UTF8_BYTES,
): OutboxRow[] {
  const batch: OutboxRow[] = [];
  let runningBytes = PUSH_WRAPPER_UTF8_BYTES;
  let explicitIds = 0;

  for (const row of rows) {
    if (batch.length >= MAX_BATCH_MUTATIONS) {
      break;
    }
    const addedIds =
      row.payload.type === "item_mark_bulk_read" &&
      row.payload.scope === "items" &&
      row.payload.item_ids
        ? row.payload.item_ids.length
        : 0;

    if (batch.length > 0 && explicitIds + addedIds > MAX_BATCH_EXPLICIT_ITEM_IDS) {
      break;
    }

    const rowBytes = textEncoder.encode(JSON.stringify(row.payload)).byteLength;
    const nextBytes = runningBytes + (batch.length > 0 ? 1 : 0) + rowBytes;
    if (batch.length > 0 && nextBytes > maxBytes) {
      break;
    }

    batch.push(row);
    runningBytes = nextBytes;
    explicitIds += addedIds;
  }

  return batch;
}

async function recomputeItemInTx(db: SmartRSSDB, itemId: string): Promise<boolean> {
  const base = await db.items_base.get(itemId);
  if (!base) {
    return false;
  }
  const allOutbox = await db.outbox.toArray();
  const display = applyOutboxToItem(base, allOutbox);
  await db.items.put(display);
  return true;
}

async function recomputeFeedInTx(db: SmartRSSDB, feedId: string): Promise<boolean> {
  const base = await db.feeds_base.get(feedId);
  if (!base) {
    return false;
  }
  const allOutbox = await db.outbox.toArray();
  const display = applyOutboxToFeed(base, allOutbox);
  await db.feeds.put(display);
  return true;
}

export async function enqueueItemState(
  db: SmartRSSDB,
  itemId: string,
  state: "read" | "unread",
  displayLabel = "Article",
  nowIso = new Date().toISOString(),
): Promise<OutboxRow> {
  const id = itemId.toLowerCase();
  const mutationId = uuidv4();
  const payload: SyncMutationInput = {
    type: "item_set_state",
    client_mutation_id: mutationId,
    client_timestamp: nowIso,
    item_id: id,
    state,
  };

  let inserted!: OutboxRow;
  await db.transaction("rw", [db.items_base, db.items, db.outbox], async () => {
    const base = await db.items_base.get(id);
    const row: OutboxRow = {
      client_mutation_id: mutationId,
      created_at: nowIso,
      payload,
      entity_key: `item:${id}`,
      target_absent: base === undefined,
      captured_item_ids: null,
      display_label: displayLabel,
      attempts: 0,
      last_error: null,
    };
    const localSeq = await db.outbox.add(row);
    inserted = { ...row, local_seq: localSeq };
    if (base) {
      await recomputeItemInTx(db, id);
    }
  });
  return inserted;
}

export async function enqueueItemStarred(
  db: SmartRSSDB,
  itemId: string,
  isStarred: boolean,
  displayLabel = "Article",
  nowIso = new Date().toISOString(),
): Promise<OutboxRow> {
  const id = itemId.toLowerCase();
  const mutationId = uuidv4();
  const payload: SyncMutationInput = {
    type: "item_set_starred",
    client_mutation_id: mutationId,
    client_timestamp: nowIso,
    item_id: id,
    is_starred: isStarred,
  };

  let inserted!: OutboxRow;
  await db.transaction("rw", [db.items_base, db.items, db.outbox], async () => {
    const base = await db.items_base.get(id);
    const row: OutboxRow = {
      client_mutation_id: mutationId,
      created_at: nowIso,
      payload,
      entity_key: `item:${id}`,
      target_absent: base === undefined,
      captured_item_ids: null,
      display_label: displayLabel,
      attempts: 0,
      last_error: null,
    };
    const localSeq = await db.outbox.add(row);
    inserted = { ...row, local_seq: localSeq };
    if (base) {
      await recomputeItemInTx(db, id);
    }
  });
  return inserted;
}

export async function enqueueFeedField(
  db: SmartRSSDB,
  feedId: string,
  field: FeedMutableField,
  value: string | number | boolean | null,
  displayLabel = "Feed",
  nowIso = new Date().toISOString(),
): Promise<OutboxRow> {
  const id = feedId.toLowerCase();
  const normalizedValue =
    field === "category_id" && typeof value === "string" ? value.toLowerCase() : value;
  const mutationId = uuidv4();
  const payload: SyncMutationInput = {
    type: "feed_set_field",
    client_mutation_id: mutationId,
    client_timestamp: nowIso,
    feed_id: id,
    field,
    value: normalizedValue,
  };

  let inserted!: OutboxRow;
  await db.transaction("rw", [db.feeds_base, db.feeds, db.outbox], async () => {
    const base = await db.feeds_base.get(id);
    const row: OutboxRow = {
      client_mutation_id: mutationId,
      created_at: nowIso,
      payload,
      entity_key: `feed:${id}`,
      target_absent: base === undefined,
      captured_item_ids: null,
      display_label: displayLabel,
      attempts: 0,
      last_error: null,
    };
    const localSeq = await db.outbox.add(row);
    inserted = { ...row, local_seq: localSeq };
    if (base) {
      await recomputeFeedInTx(db, id);
    }
  });
  return inserted;
}

export interface BulkReadEnqueueOptions {
  scope: BulkReadScope;
  feedId?: string | null;
  categoryId?: string | null;
  olderThanTs?: string | null;
  itemIds?: string[] | null;
  displayLabel?: string;
  nowIso?: string;
}

export async function enqueueBulkMarkRead(
  db: SmartRSSDB,
  options: BulkReadEnqueueOptions,
): Promise<OutboxRow[]> {
  const nowIso = options.nowIso ?? new Date().toISOString();
  const label = options.displayLabel ?? `Mark ${options.scope} read`;

  if (options.scope === "items") {
    const unique = Array.from(new Set((options.itemIds ?? []).map((x) => x.toLowerCase())));
    if (unique.length === 0) {
      return [];
    }
    const created: OutboxRow[] = [];
    await db.transaction("rw", [db.items_base, db.items, db.outbox], async () => {
      for (let i = 0; i < unique.length; i += BULK_ITEMS_CHUNK_SIZE) {
        const chunk = unique.slice(i, i + BULK_ITEMS_CHUNK_SIZE);
        const mutationId = uuidv4();
        const payload: ItemMarkBulkReadMutation = {
          type: "item_mark_bulk_read",
          client_mutation_id: mutationId,
          client_timestamp: nowIso,
          scope: "items",
          item_ids: chunk,
        };
        const row: OutboxRow = {
          client_mutation_id: mutationId,
          created_at: nowIso,
          payload,
          entity_key: "bulk",
          target_absent: false,
          captured_item_ids: null,
          display_label: label,
          attempts: 0,
          last_error: null,
        };
        const localSeq = await db.outbox.add(row);
        created.push({ ...row, local_seq: localSeq });
      }
      const allOutbox = await db.outbox.toArray();
      const bases = await db.items_base.bulkGet(unique);
      const updated = bases
        .filter((b): b is NonNullable<typeof b> => b !== undefined)
        .map((b) => applyOutboxToItem(b, allOutbox));
      if (updated.length > 0) {
        await db.items.bulkPut(updated);
      }
    });
    return created;
  }

  // Scoped bulk mark-read: requires initialized sync cursor & epoch
  let createdRow!: OutboxRow;
  await db.transaction(
    "rw",
    [db.client_meta, db.feeds, db.items_base, db.items, db.outbox],
    async () => {
      const syncMeta = await getSyncMeta(db);
      if (syncMeta.instance_epoch === null || syncMeta.sync_cursor === null) {
        throw new Error("Cannot enqueue scoped bulk mark-read before initial bootstrap completes");
      }
      const upToSeq = syncMeta.sync_cursor;
      const epoch = syncMeta.instance_epoch;
      const feedId = options.feedId ? options.feedId.toLowerCase() : null;
      const categoryId = options.categoryId ? options.categoryId.toLowerCase() : null;

      let allowedFeedIds: Set<string> | null = null;
      if (options.scope === "category" && categoryId) {
        const catFeeds = await db.feeds.where("category_id").equals(categoryId).toArray();
        allowedFeedIds = new Set(catFeeds.map((f) => f.id));
      } else if (options.scope === "feed" && feedId) {
        allowedFeedIds = new Set([feedId]);
      }

      const unreadItems = await db.items.where("state").equals("unread").toArray();
      const capturedIds: string[] = [];
      for (const item of unreadItems) {
        if (item.ingest_sync_seq > upToSeq) {
          continue;
        }
        if (allowedFeedIds && !allowedFeedIds.has(item.feed_id)) {
          continue;
        }
        if (options.olderThanTs && !(item.effective_date < options.olderThanTs)) {
          continue;
        }
        capturedIds.push(item.id);
      }

      const mutationId = uuidv4();
      const payload: ItemMarkBulkReadMutation = {
        type: "item_mark_bulk_read",
        client_mutation_id: mutationId,
        client_timestamp: nowIso,
        scope: options.scope,
        up_to_sync_seq: upToSeq,
        epoch,
        ...(feedId ? { feed_id: feedId } : {}),
        ...(categoryId ? { category_id: categoryId } : {}),
        ...(options.olderThanTs ? { older_than_ts: options.olderThanTs } : {}),
      };

      const row: OutboxRow = {
        client_mutation_id: mutationId,
        created_at: nowIso,
        payload,
        entity_key: "bulk",
        target_absent: false,
        captured_item_ids: capturedIds,
        display_label: label,
        attempts: 0,
        last_error: null,
      };
      const localSeq = await db.outbox.add(row);
      createdRow = { ...row, local_seq: localSeq };

      if (capturedIds.length > 0) {
        const allOutbox = await db.outbox.toArray();
        const bases = await db.items_base.bulkGet(capturedIds);
        const updated = bases
          .filter((b): b is NonNullable<typeof b> => b !== undefined)
          .map((b) => applyOutboxToItem(b, allOutbox));
        if (updated.length > 0) {
          await db.items.bulkPut(updated);
        }
      }
    },
  );
  return [createdRow];
}

function collectEntityTargets(row: OutboxRow): { itemIds: string[]; feedIds: string[] } {
  const m = row.payload;
  if (m.type === "item_set_state" || m.type === "item_set_starred") {
    return { itemIds: [m.item_id], feedIds: [] };
  }
  if (m.type === "feed_set_field") {
    return { itemIds: [], feedIds: [m.feed_id] };
  }
  const itemIds = new Set<string>();
  if (m.item_ids) {
    for (const id of m.item_ids) itemIds.add(id);
  }
  if (row.captured_item_ids) {
    for (const id of row.captured_item_ids) itemIds.add(id);
  }
  return { itemIds: Array.from(itemIds), feedIds: [] };
}

async function applyPushSuccessBatch(
  db: SmartRSSDB,
  batch: readonly OutboxRow[],
  response: SyncPushResponse,
  lock: LockHandle,
  callbacks: PushObserverCallbacks,
): Promise<{ processedCount: number; missingRows: OutboxRow[] }> {
  const nowMs = callbacks.now ? callbacks.now() : Date.now();
  const driftMs = computeClockDriftMs(response.server_now, nowMs);

  const byId = new Map<string, OutboxRow>();
  for (const row of batch) {
    byId.set(row.client_mutation_id, row);
  }
  const seenIds = new Set<string>();

  const affectedItemIds = new Set<string>();
  const affectedFeedIds = new Set<string>();
  const archiveUpdates: Array<{
    itemId: string;
    outcome: "applied" | "superseded" | "rejected";
    snapshot: SyncPushItemSnapshot | null;
  }> = [];

  await db.transaction(
    "rw",
    [
      db.client_meta,
      db.items_base,
      db.items,
      db.feeds_base,
      db.feeds,
      db.outbox,
      db.replay_journal,
      db.sync_notifications,
    ],
    async () => {
      await lock.assertAndRenewInTx();

      // Persist clock drift atomically inside this fenced transaction (V-04)
      const syncMeta = await getSyncMeta(db);
      await setSyncMeta(db, {
        ...syncMeta,
        last_server_now: response.server_now,
        clock_drift_ms: driftMs,
      });

      for (const res of response.results) {
        const row = byId.get(res.client_mutation_id);
        if (!row) {
          continue;
        }
        seenIds.add(res.client_mutation_id);
        const targets = collectEntityTargets(row);
        for (const id of targets.itemIds) affectedItemIds.add(id);
        for (const id of targets.feedIds) affectedFeedIds.add(id);

        if (row.local_seq !== undefined) {
          await db.outbox.delete(row.local_seq);
        }

        if (res.outcome === "applied") {
          // 48h Replay Journal (U-10 / Q-3)
          const m = row.payload;
          if (m.type === "item_mark_bulk_read" && m.scope !== "items") {
            if (row.captured_item_ids && row.captured_item_ids.length > 0) {
              const chunks = buildChunkedItemsMutations(row.captured_item_ids, m.client_timestamp);
              if (chunks) {
                const journalRows: ReplayJournalRow[] = chunks.map((chunkPayload) => ({
                  client_mutation_id: chunkPayload.client_mutation_id,
                  acknowledged_at: response.server_now,
                  payload: chunkPayload,
                }));
                await db.replay_journal.bulkPut(journalRows);
              }
            }
          } else {
            await db.replay_journal.put({
              client_mutation_id: row.client_mutation_id,
              acknowledged_at: response.server_now,
              payload: row.payload,
            });
          }

          if (
            row.target_absent &&
            (row.payload.type === "item_set_state" || row.payload.type === "item_set_starred")
          ) {
            archiveUpdates.push({
              itemId: row.payload.item_id,
              outcome: "applied",
              snapshot: null,
            });
          }
        } else {
          // superseded or rejected: merge slim current_entity into *_base if present (U-01)
          if (res.current_entity_type === "item" && res.current_entity) {
            const snap = res.current_entity as SyncPushItemSnapshot;
            const base = await db.items_base.get(snap.id);
            if (base) {
              await db.items_base.put(mergeItemSnapshotIntoBase(base, snap));
              affectedItemIds.add(snap.id);
            }
            if (row.target_absent) {
              archiveUpdates.push({
                itemId: snap.id,
                outcome: res.outcome,
                snapshot: snap,
              });
            }
          } else if (res.current_entity_type === "feed" && res.current_entity) {
            const snap = res.current_entity as SyncPushFeedSnapshot;
            const base = await db.feeds_base.get(snap.id);
            if (base) {
              await db.feeds_base.put(mergeFeedSnapshotIntoBase(base, snap));
              affectedFeedIds.add(snap.id);
            }
          } else if (
            row.target_absent &&
            (row.payload.type === "item_set_state" || row.payload.type === "item_set_starred")
          ) {
            archiveUpdates.push({
              itemId: row.payload.item_id,
              outcome: res.outcome,
              snapshot: null,
            });
          }

          if (res.outcome === "rejected") {
            await db.sync_notifications.add({
              severity: "warning",
              title: `Change rolled back: ${row.display_label}`,
              detail: humanizeRejectionReason(res.reason),
              reason: res.reason,
              mutation_type: row.payload.type,
              entity_label: row.display_label,
              created_at: new Date().toISOString(),
              dismissed: 0,
            });
          }
        }
      }

      // Unified recompute from *_base + remaining outbox (U-01)
      const remainingOutbox = await db.outbox.toArray();
      if (affectedItemIds.size > 0) {
        const ids = Array.from(affectedItemIds);
        const bases = await db.items_base.bulkGet(ids);
        const updated = bases
          .filter((b): b is NonNullable<typeof b> => b !== undefined)
          .map((b) => applyOutboxToItem(b, remainingOutbox));
        if (updated.length > 0) {
          await db.items.bulkPut(updated);
        }
      }
      if (affectedFeedIds.size > 0) {
        const ids = Array.from(affectedFeedIds);
        const bases = await db.feeds_base.bulkGet(ids);
        const updated = bases
          .filter((b): b is NonNullable<typeof b> => b !== undefined)
          .map((b) => applyOutboxToFeed(b, remainingOutbox));
        if (updated.length > 0) {
          await db.feeds.bulkPut(updated);
        }
      }
    },
  );

  callbacks.onServerNow?.(response.server_now, driftMs);
  for (const upd of archiveUpdates) {
    callbacks.onArchiveMutationResolved?.(upd.itemId, upd.outcome, upd.snapshot);
  }
  if (affectedItemIds.size > 0) {
    callbacks.onItemsChanged?.(Array.from(affectedItemIds));
  }

  const missingRows = batch.filter((row) => !seenIds.has(row.client_mutation_id));
  return { processedCount: seenIds.size, missingRows };
}

async function quarantineRowInTx(
  db: SmartRSSDB,
  row: OutboxRow,
  reason: string,
  lock: LockHandle | null,
  notifyConsolidatedType: boolean,
): Promise<string[]> {
  const targets = collectEntityTargets(row);
  await db.transaction(
    "rw",
    [
      db.client_meta,
      db.items_base,
      db.items,
      db.feeds_base,
      db.feeds,
      db.outbox,
      db.failed_mutations,
      db.sync_notifications,
    ],
    async () => {
      if (lock) {
        await lock.assertAndRenewInTx();
      }
      if (row.local_seq !== undefined) {
        await db.outbox.delete(row.local_seq);
      }
      const failedRow: FailedMutationRow = {
        client_mutation_id: row.client_mutation_id,
        failed_at: new Date().toISOString(),
        payload: row.payload,
        entity_key: row.entity_key,
        target_absent: row.target_absent,
        captured_item_ids: row.captured_item_ids,
        display_label: row.display_label,
        reason,
      };
      await db.failed_mutations.put(failedRow);

      // Recompute display rows cleanly from *_base (U-01)
      const remainingOutbox = await db.outbox.toArray();
      if (targets.itemIds.length > 0) {
        const bases = await db.items_base.bulkGet(targets.itemIds);
        const updated = bases
          .filter((b): b is NonNullable<typeof b> => b !== undefined)
          .map((b) => applyOutboxToItem(b, remainingOutbox));
        if (updated.length > 0) {
          await db.items.bulkPut(updated);
        }
      }
      if (targets.feedIds.length > 0) {
        const bases = await db.feeds_base.bulkGet(targets.feedIds);
        const updated = bases
          .filter((b): b is NonNullable<typeof b> => b !== undefined)
          .map((b) => applyOutboxToFeed(b, remainingOutbox));
        if (updated.length > 0) {
          await db.feeds.bulkPut(updated);
        }
      }

      if (notifyConsolidatedType) {
        // Consolidated banner for repeated same-type quarantines (review §10 note 1)
        const existing = await db.sync_notifications
          .filter((n) => n.dismissed === 0 && n.reason === `partial_skew:${row.payload.type}`)
          .first();
        if (!existing) {
          await db.sync_notifications.add({
            severity: "error",
            title: `Server rejected ${row.payload.type} changes — update the app`,
            detail:
              "Multiple offline changes of this type were rejected and quarantined. Update the client and retry them in Settings → Outbox Inspector.",
            reason: `partial_skew:${row.payload.type}`,
            mutation_type: row.payload.type,
            entity_label: row.display_label,
            created_at: new Date().toISOString(),
            dismissed: 0,
          });
        }
      } else {
        await db.sync_notifications.add({
          severity: "error",
          title: `Quarantined change: ${row.display_label}`,
          detail: `${reason}. Rolled back locally; you can retry or dismiss it in Settings → Outbox Inspector.`,
          reason: "quarantined",
          mutation_type: row.payload.type,
          entity_label: row.display_label,
          created_at: new Date().toISOString(),
          dismissed: 0,
        });
      }
    },
  );
  return targets.itemIds;
}

async function recordBatchAttemptFailure(
  db: SmartRSSDB,
  rows: readonly OutboxRow[],
  errorMessage: string,
  lock: LockHandle,
): Promise<void> {
  await db.transaction("rw", [db.client_meta, db.outbox], async () => {
    await lock.assertAndRenewInTx();
    for (const row of rows) {
      if (row.local_seq !== undefined) {
        await db.outbox.update(row.local_seq, {
          attempts: row.attempts + 1,
          last_error: errorMessage,
        });
      }
    }
  });
}

function isBisectableStatus(status: number): boolean {
  return status === 422 || status === 400 || status === 413;
}

/**
 * Flush the entire pending outbox with FIFO batching and skew-safe poison-pill bisection
 * (`U-04`, `U-04-F1`, review §10 note 1 & note 2).
 */
export async function flushOutbox(
  db: SmartRSSDB,
  api: SmartRSSApiClient,
  lock: LockHandle,
  callbacks: PushObserverCallbacks = {},
): Promise<FlushOutcome> {
  let flushedCount = 0;
  let quarantinedCount = 0;
  let skewDisproven = false;
  const quarantinedByType = new Map<string, number>();

  const sendRows = async (
    rows: readonly OutboxRow[],
  ): Promise<
    | { ok: true; response: SyncPushResponse }
    | { ok: false; kind: "bisectable"; status: number; message: string }
    | { ok: false; kind: "terminal"; outcome: FlushOutcome }
  > => {
    await lock.renewBeforeIo();
    try {
      const response = await api.syncPush({
        mutations: rows.map((r) => r.payload),
      });
      return { ok: true, response };
    } catch (err) {
      if (err instanceof ApiNetworkError) {
        await recordBatchAttemptFailure(db, rows, err.message, lock);
        return { ok: false, kind: "terminal", outcome: { status: "detached" } };
      }
      if (err instanceof ApiHttpError) {
        if (err.status === 401) {
          return { ok: false, kind: "terminal", outcome: { status: "auth_revoked" } };
        }
        if (isBisectableStatus(err.status)) {
          return { ok: false, kind: "bisectable", status: err.status, message: err.message };
        }
        await recordBatchAttemptFailure(db, rows, err.message, lock);
        return {
          ok: false,
          kind: "terminal",
          outcome: {
            status: "sync_error",
            message: `Server error during sync push (HTTP ${err.status}): ${err.message}`,
            retryAfterSeconds: err.retryAfterSeconds,
          },
        };
      }
      throw err;
    }
  };

  const handleQuarantineSingle = async (
    row: OutboxRow,
    status: number,
    message: string,
  ): Promise<FlushOutcome | null> => {
    const canQuarantine =
      (skewDisproven || status === 413) && quarantinedCount < MAX_QUARANTINES_PER_CYCLE;
    if (!canQuarantine) {
      await recordBatchAttemptFailure(db, [row], message, lock);
      return {
        status: "sync_error",
        message:
          "Server rejected sync payload — update the app, or inspect and quarantine the failing item in Settings → Outbox Inspector.",
      };
    }

    const prevTypeCount =
      (quarantinedByType.get(row.payload.type) ?? 0) +
      (await db.failed_mutations.filter((f) => f.payload.type === row.payload.type).count());
    const itemIds = await quarantineRowInTx(
      db,
      row,
      `HTTP ${status}: ${message}`,
      lock,
      prevTypeCount >= 1,
    );
    quarantinedByType.set(row.payload.type, prevTypeCount + 1);
    quarantinedCount += 1;
    if (
      row.target_absent &&
      (row.payload.type === "item_set_state" || row.payload.type === "item_set_starred")
    ) {
      callbacks.onArchiveMutationResolved?.(row.payload.item_id, "rejected", null);
    }
    if (itemIds.length > 0) {
      callbacks.onItemsChanged?.(itemIds);
    }
    return null;
  };

  const applyAndCheckResults = async (
    rows: readonly OutboxRow[],
    response: SyncPushResponse,
  ): Promise<FlushOutcome | null> => {
    const applied = await applyPushSuccessBatch(db, rows, response, lock, callbacks);
    flushedCount += applied.processedCount;
    if (applied.missingRows.length > 0) {
      const msg = `Server push response omitted ${applied.missingRows.length} mutation result(s).`;
      await recordBatchAttemptFailure(db, applied.missingRows, msg, lock);
      return { status: "sync_error", message: msg };
    }
    return null;
  };

  const flushSubBatch = async (subBatch: readonly OutboxRow[]): Promise<FlushOutcome | null> => {
    const attempt = await sendRows(subBatch);
    if (attempt.ok) {
      skewDisproven = true;
      return applyAndCheckResults(subBatch, attempt.response);
    }
    if (attempt.kind === "terminal") {
      return attempt.outcome;
    }

    if (subBatch.length === 1) {
      const onlyRow = subBatch[0];
      if (!onlyRow) return null;
      return handleQuarantineSingle(onlyRow, attempt.status, attempt.message);
    }

    const mid = Math.floor(subBatch.length / 2);
    const left = subBatch.slice(0, mid);
    const right = subBatch.slice(mid);

    const leftAttempt = await sendRows(left);
    if (!leftAttempt.ok && leftAttempt.kind === "terminal") {
      return leftAttempt.outcome;
    }
    if (leftAttempt.ok) {
      skewDisproven = true;
      const err = await applyAndCheckResults(left, leftAttempt.response);
      if (err) return err;
      return flushSubBatch(right);
    }

    const rightAttempt = await sendRows(right);
    if (!rightAttempt.ok && rightAttempt.kind === "terminal") {
      return rightAttempt.outcome;
    }
    if (rightAttempt.ok) {
      skewDisproven = true;
      const err = await applyAndCheckResults(right, rightAttempt.response);
      if (err) return err;
      return flushSubBatch(left);
    }

    // Both halves failed with 422/400/413 (U-04-F1)
    if (!skewDisproven) {
      // Probe up to MAX_QUARANTINES_PER_CYCLE + 1 (= 4) single rows to distinguish
      // systematic version skew from poison pills in both halves (e.g. indices 2 & 7).
      const probeLimit = Math.min(subBatch.length, MAX_QUARANTINES_PER_CYCLE + 1);
      let remaining = [...subBatch];
      for (let i = 0; i < probeLimit; i += 1) {
        const probeRow = subBatch[i];
        if (!probeRow) continue;
        const probeRes = await sendRows([probeRow]);
        if (!probeRes.ok && probeRes.kind === "terminal") {
          return probeRes.outcome;
        }
        if (probeRes.ok) {
          skewDisproven = true;
          const err = await applyAndCheckResults([probeRow], probeRes.response);
          if (err) return err;
          remaining = remaining.filter((r) => r.client_mutation_id !== probeRow.client_mutation_id);
          break;
        }
      }

      if (!skewDisproven) {
        const head = subBatch[0];
        if (head) {
          await recordBatchAttemptFailure(db, [head], attempt.message, lock);
        }
        return {
          status: "sync_error",
          message:
            "Server rejected sync payload across multiple entries — update or reload the app.",
        };
      }

      // Skew disproven: recurse on remaining rows cleanly
      if (remaining.length > 0) {
        return flushSubBatch(remaining);
      }
      return null;
    }

    // Skew already disproven earlier in this cycle -> recurse on both halves
    const leftErr = await flushSubBatch(left);
    if (leftErr) {
      return leftErr;
    }
    return flushSubBatch(right);
  };

  // Bounded outer loop with progress guard (V-02)
  const initialCount = await db.outbox.count();
  const maxIterations = Math.max(
    4,
    Math.ceil(initialCount / MAX_BATCH_MUTATIONS) + MAX_QUARANTINES_PER_CYCLE + 4,
  );
  let iterations = 0;

  while (iterations < maxIterations) {
    const allPending = await db.outbox.orderBy("local_seq").toArray();
    if (allPending.length === 0) {
      break;
    }
    const batch = packOutboxBatch(allPending);
    const beforeTotal = flushedCount + quarantinedCount;
    const haltOutcome = await flushSubBatch(batch);
    if (haltOutcome) {
      return haltOutcome;
    }
    if (flushedCount + quarantinedCount <= beforeTotal) {
      return {
        status: "sync_error",
        message: "Sync push made no progress while flushing pending outbox.",
      };
    }
    iterations += 1;
  }

  return { status: "ok", flushedCount, quarantinedCount };
}

/**
 * Manual quarantine action from Outbox Inspector for lone poison pills (`U-04-F1`).
 */
export async function manualQuarantineOutboxRow(
  db: SmartRSSDB,
  localSeq: number,
  reason = "Manually quarantined by user",
): Promise<string[]> {
  const row = await db.outbox.get(localSeq);
  if (!row) {
    return [];
  }
  return quarantineRowInTx(db, row, row.last_error ?? reason, null, false);
}

/**
 * Re-enqueue a quarantined mutation with its original `client_mutation_id` (`U-04`).
 */
export async function retryFailedMutation(
  db: SmartRSSDB,
  clientMutationId: string,
): Promise<boolean> {
  let retried = false;
  await db.transaction(
    "rw",
    [db.items_base, db.items, db.feeds_base, db.feeds, db.outbox, db.failed_mutations],
    async () => {
      const failed = await db.failed_mutations.get(clientMutationId);
      if (!failed) {
        return;
      }
      await db.failed_mutations.delete(clientMutationId);
      const row: OutboxRow = {
        client_mutation_id: failed.client_mutation_id,
        created_at: new Date().toISOString(),
        payload: failed.payload,
        entity_key: failed.entity_key,
        target_absent: failed.target_absent,
        captured_item_ids: failed.captured_item_ids,
        display_label: failed.display_label,
        attempts: 0,
        last_error: null,
      };
      await db.outbox.add(row);
      const targets = collectEntityTargets(row);
      for (const itemId of targets.itemIds) {
        await recomputeItemInTx(db, itemId);
      }
      for (const feedId of targets.feedIds) {
        await recomputeFeedInTx(db, feedId);
      }
      retried = true;
    },
  );
  return retried;
}

export async function dismissFailedMutation(
  db: SmartRSSDB,
  clientMutationId: string,
): Promise<void> {
  await db.failed_mutations.delete(clientMutationId);
}

/**
 * Mint fresh `client_mutation_id`s across `outbox` and `replay_journal` after re-pairing
 * (`U-11`: prevents `mutation_id_conflict` because server scopes idempotency per `device_id`).
 */
export async function mintFreshMutationIdsOnRepair(db: SmartRSSDB): Promise<void> {
  await db.transaction("rw", [db.outbox, db.replay_journal], async () => {
    const outboxRows = await db.outbox.orderBy("local_seq").toArray();
    for (const row of outboxRows) {
      const freshId = uuidv4();
      if (row.local_seq !== undefined) {
        await db.outbox.update(row.local_seq, {
          client_mutation_id: freshId,
          payload: { ...row.payload, client_mutation_id: freshId },
          attempts: 0,
          last_error: null,
        });
      }
    }

    const journalRows = await db.replay_journal.toArray();
    if (journalRows.length > 0) {
      await db.replay_journal.clear();
      const refreshed: ReplayJournalRow[] = journalRows.map((jr) => {
        const freshId = uuidv4();
        return {
          client_mutation_id: freshId,
          acknowledged_at: jr.acknowledged_at,
          payload: { ...jr.payload, client_mutation_id: freshId },
        };
      });
      await db.replay_journal.bulkPut(refreshed);
    }
  });
}

export async function pruneReplayJournal(db: SmartRSSDB, nowMs = Date.now()): Promise<number> {
  const cutoffIso = new Date(nowMs - REPLAY_JOURNAL_TTL_MS).toISOString();
  return db.replay_journal.where("acknowledged_at").below(cutoffIso).delete();
}

/**
 * Prepare outbox & replay journal for restore recovery (`epoch_mismatch` or `cursor_ahead`):
 * 1. Rewrite pending scoped `item_mark_bulk_read` into chunked `scope='items'` (`<= 5,000` IDs;
 *    drop + notify if `> 5,000` or empty, `P-11-F1`).
 * 2. Promote 48h `replay_journal` entries into `outbox` preserving original `client_mutation_id`
 *    and `client_timestamp` (`U-10` / `Q-3`, `U-21`).
 */
export async function prepareOutboxForRestore(
  db: SmartRSSDB,
  lock: LockHandle,
  nowMs = Date.now(),
): Promise<{
  rewrittenScopedCount: number;
  droppedScopedCount: number;
  promotedJournalCount: number;
}> {
  let rewrittenScopedCount = 0;
  let droppedScopedCount = 0;
  let promotedJournalCount = 0;
  const cutoffIso = new Date(nowMs - REPLAY_JOURNAL_TTL_MS).toISOString();

  await db.transaction(
    "rw",
    [db.client_meta, db.outbox, db.replay_journal, db.sync_notifications],
    async () => {
      await lock.assertAndRenewInTx();
      const outboxRows = await db.outbox.orderBy("local_seq").toArray();
      const existingMutationIds = new Set(outboxRows.map((r) => r.client_mutation_id));

      for (const row of outboxRows) {
        if (row.payload.type === "item_mark_bulk_read" && row.payload.scope !== "items") {
          if (row.local_seq !== undefined) {
            await db.outbox.delete(row.local_seq);
          }
          const chunks = row.captured_item_ids
            ? buildChunkedItemsMutations(row.captured_item_ids, row.payload.client_timestamp)
            : null;
          if (chunks) {
            for (const chunkPayload of chunks) {
              await db.outbox.add({
                client_mutation_id: chunkPayload.client_mutation_id,
                created_at: row.created_at,
                payload: chunkPayload,
                entity_key: "bulk",
                target_absent: false,
                captured_item_ids: null,
                display_label: row.display_label,
                attempts: 0,
                last_error: null,
              });
              existingMutationIds.add(chunkPayload.client_mutation_id);
            }
            rewrittenScopedCount += 1;
          } else {
            droppedScopedCount += 1;
            await db.sync_notifications.add({
              severity: "info",
              title: "Offline bulk mark-read cleared after restore",
              detail:
                "Server database was restored; a large offline bulk mark-read (> 5,000 items or empty) could not be replayed safely and was cleared.",
              reason: "restore_bulk_dropped",
              mutation_type: "item_mark_bulk_read",
              entity_label: row.display_label,
              created_at: new Date().toISOString(),
              dismissed: 0,
            });
          }
        }
      }

      const journalRows = await db.replay_journal
        .where("acknowledged_at")
        .aboveOrEqual(cutoffIso)
        .toArray();
      for (const jr of journalRows) {
        if (existingMutationIds.has(jr.client_mutation_id)) {
          continue;
        }
        const entityKey =
          jr.payload.type === "item_set_state" || jr.payload.type === "item_set_starred"
            ? `item:${jr.payload.item_id}`
            : jr.payload.type === "feed_set_field"
              ? `feed:${jr.payload.feed_id}`
              : "bulk";
        await db.outbox.add({
          client_mutation_id: jr.client_mutation_id,
          created_at: jr.acknowledged_at,
          payload: jr.payload,
          entity_key: entityKey,
          target_absent: false,
          captured_item_ids: null,
          display_label: "Restored recent action",
          attempts: 0,
          last_error: null,
        });
        existingMutationIds.add(jr.client_mutation_id);
        promotedJournalCount += 1;
      }
    },
  );

  return { rewrittenScopedCount, droppedScopedCount, promotedJournalCount };
}
