/**
 * Pure deterministic client sync reconciliation, overlay, eviction & restore rules (1c §5.3, §6.4–§6.6).
 *
 * Implements Checklist B1 (`U-01`): display rows are ALWAYS derived as `base ⊕ fold(outbox)`
 * from server-confirmed `items_base` / `feeds_base` rows, eliminating stale-snapshot rollback bugs.
 */

import type { LocalItemBaseRecord, LocalItemRecord, OutboxRow, SmartRSSDB } from "../db/schema";
import { uuidv4 } from "../security/uuid";
import type { SearchItemDTO } from "../types/api";
import type {
  FeedStatus,
  ItemMarkBulkReadMutation,
  SyncFeedDTO,
  SyncItemDTO,
  SyncPushFeedSnapshot,
  SyncPushItemSnapshot,
} from "../types/sync";

export const EVICTION_WINDOW_MS = 14 * 86_400_000;
export const CLOCK_DRIFT_WARN_MS = 5 * 60 * 1_000;
export const MAX_RESTORE_REWRITE_IDS = 5_000;
export const BULK_ITEMS_CHUNK_SIZE = 1_000;

export function toLocalItemBase(dto: SyncItemDTO, bootstrapGen: number): LocalItemBaseRecord {
  return {
    ...dto,
    bootstrap_gen: bootstrapGen,
  };
}

export function toLocalItemDisplay(base: LocalItemBaseRecord): LocalItemRecord {
  return {
    ...base,
    effective_date: base.published_at ?? base.fetched_at,
    starred_num: base.is_starred ? 1 : 0,
  };
}

/**
 * Pure 2-argument overlay function (`1c` §6.4, U-01):
 * Folds all outbox mutations targeting `base.id` in `local_seq ASC` order on top of `base`.
 * Scoped bulk reads match ONLY via `captured_item_ids` (never re-running scope predicates, U-15).
 */
export function applyOutboxToItem(
  base: LocalItemBaseRecord,
  outboxRows: readonly OutboxRow[],
): LocalItemRecord {
  const current: LocalItemRecord = toLocalItemDisplay(base);

  const sorted = [...outboxRows].sort((a, b) => (a.local_seq ?? 0) - (b.local_seq ?? 0));

  for (const row of sorted) {
    const m = row.payload;
    if (m.type === "item_set_state" && m.item_id === current.id) {
      if (m.state === "read") {
        current.state = "read";
        current.read_at = m.client_timestamp;
        current.read_state_updated_at = m.client_timestamp;
        current.unexpired_at = null;
      } else {
        if (current.state === "expired" || current.expired_at !== null) {
          current.unexpired_at = m.client_timestamp;
        }
        current.state = "unread";
        current.read_at = null;
        current.read_state_updated_at = m.client_timestamp;
      }
    } else if (m.type === "item_set_starred" && m.item_id === current.id) {
      current.is_starred = m.is_starred;
      current.starred_num = m.is_starred ? 1 : 0;
      current.starred_at = m.is_starred ? m.client_timestamp : null;
      current.starred_updated_at = m.client_timestamp;
    } else if (m.type === "item_mark_bulk_read") {
      if (m.scope === "items") {
        if (m.item_ids?.includes(current.id) && current.state !== "expired") {
          current.state = "read";
          current.read_at = m.client_timestamp;
          current.read_state_updated_at = m.client_timestamp;
          current.unexpired_at = null;
        }
      } else if (row.captured_item_ids?.includes(current.id) && current.state === "unread") {
        current.state = "read";
        current.read_at = m.client_timestamp;
        current.read_state_updated_at = m.client_timestamp;
        current.unexpired_at = null;
      }
    }
  }

  return current;
}

/**
 * Pure overlay for archive-only `SearchItemDTO` rows (`U-12`, `P-17`).
 */
export function applyOutboxToSearchItem(
  searchItem: SearchItemDTO,
  outboxRows: readonly OutboxRow[],
): SearchItemDTO {
  const syntheticBase: LocalItemBaseRecord = {
    ...searchItem,
    raw_summary: null,
    summary_truncated: false,
    bootstrap_gen: 0,
  };
  const overlaid = applyOutboxToItem(syntheticBase, outboxRows);
  return {
    ...searchItem,
    state: overlaid.state,
    is_starred: overlaid.is_starred,
    read_at: overlaid.read_at,
    starred_at: overlaid.starred_at,
    expired_at: overlaid.expired_at,
    unexpired_at: overlaid.unexpired_at,
    read_state_updated_at: overlaid.read_state_updated_at,
    starred_updated_at: overlaid.starred_updated_at,
  };
}

/**
 * Pure feed overlay function (`1c` §6.4, U-01).
 */
export function applyOutboxToFeed(
  base: SyncFeedDTO,
  outboxRows: readonly OutboxRow[],
): SyncFeedDTO {
  const current: SyncFeedDTO = {
    ...base,
    research_policy: { ...base.research_policy },
    field_updated_at: { ...base.field_updated_at },
  };

  const sorted = [...outboxRows].sort((a, b) => (a.local_seq ?? 0) - (b.local_seq ?? 0));

  for (const row of sorted) {
    const m = row.payload;
    if (m.type !== "feed_set_field" || m.feed_id !== current.id) {
      continue;
    }
    switch (m.field) {
      case "status":
        current.status = m.value as FeedStatus;
        break;
      case "category_id":
        current.category_id = (m.value as string | null) ?? null;
        break;
      case "expiration_days_override":
        current.expiration_days_override = (m.value as number | null) ?? null;
        break;
      case "research_policy.enabled":
        current.research_policy.enabled = Boolean(m.value);
        break;
      case "research_policy.fetch_pdfs":
        current.research_policy.fetch_pdfs = Boolean(m.value);
        break;
    }
    current.field_updated_at[m.field] = m.client_timestamp;
  }

  return current;
}

export function mergeItemSnapshotIntoBase(
  base: LocalItemBaseRecord,
  snap: SyncPushItemSnapshot,
): LocalItemBaseRecord {
  return {
    ...base,
    state: snap.state,
    is_starred: snap.is_starred,
    read_at: snap.read_at,
    starred_at: snap.starred_at,
    expired_at: snap.expired_at,
    unexpired_at: snap.unexpired_at,
    read_state_updated_at: snap.read_state_updated_at,
    starred_updated_at: snap.starred_updated_at,
    updated_at: snap.updated_at,
  };
}

export function mergeFeedSnapshotIntoBase(
  base: SyncFeedDTO,
  snap: SyncPushFeedSnapshot,
): SyncFeedDTO {
  return {
    ...base,
    status: snap.status,
    category_id: snap.category_id,
    expiration_days_override: snap.expiration_days_override,
    research_policy: { ...snap.research_policy },
    config_version: snap.config_version,
    field_updated_at: { ...snap.field_updated_at },
    updated_at: snap.updated_at,
  };
}

export function collectProtectedItemIds(outboxRows: readonly OutboxRow[]): Set<string> {
  const ids = new Set<string>();
  for (const row of outboxRows) {
    const m = row.payload;
    if (m.type === "item_set_state" || m.type === "item_set_starred") {
      ids.add(m.item_id);
    } else if (m.type === "item_mark_bulk_read") {
      if (m.item_ids) {
        for (const id of m.item_ids) ids.add(id);
      }
      if (row.captured_item_ids) {
        for (const id of row.captured_item_ids) ids.add(id);
      }
    }
  }
  return ids;
}

export function isItemOutOfWindow(
  item: Pick<SyncItemDTO, "is_starred" | "state" | "read_at" | "read_state_updated_at">,
  nowMs: number,
): boolean {
  if (item.is_starred) {
    return false;
  }
  if (item.state === "expired") {
    return true;
  }
  if (item.state === "read") {
    const cutoffIso = new Date(nowMs - EVICTION_WINDOW_MS).toISOString();
    const anchor = item.read_at ?? item.read_state_updated_at;
    return anchor < cutoffIso;
  }
  return false;
}

export function computeClockDriftMs(serverNowIso: string, clientNowMs = Date.now()): number {
  const serverMs = Date.parse(serverNowIso);
  if (Number.isNaN(serverMs)) {
    return 0;
  }
  return clientNowMs - serverMs;
}

export function isClockDriftExcessive(driftMs: number): boolean {
  return Math.abs(driftMs) > CLOCK_DRIFT_WARN_MS;
}

/**
 * Chunk captured item IDs into `scope: 'items'` mutations (max 1,000 IDs/chunk).
 * Returns `null` if empty or exceeding `MAX_RESTORE_REWRITE_IDS` (5,000).
 */
export function buildChunkedItemsMutations(
  capturedIds: readonly string[],
  clientTimestamp: string,
): ItemMarkBulkReadMutation[] | null {
  const unique = Array.from(new Set(capturedIds));
  if (unique.length === 0 || unique.length > MAX_RESTORE_REWRITE_IDS) {
    return null;
  }
  const chunks: ItemMarkBulkReadMutation[] = [];
  for (let i = 0; i < unique.length; i += BULK_ITEMS_CHUNK_SIZE) {
    chunks.push({
      type: "item_mark_bulk_read",
      client_mutation_id: uuidv4(),
      client_timestamp: clientTimestamp,
      scope: "items",
      item_ids: unique.slice(i, i + BULK_ITEMS_CHUNK_SIZE),
    });
  }
  return chunks;
}

/**
 * Evict stale expired / >14d read unstarred items from BOTH `items_base` and `items`,
 * protecting any item referenced by `outbox` (1c §5.3, U-01, U-02).
 * Returns evicted item IDs (for incremental MiniSearch discard).
 */
export async function evictStaleLocalItems(
  db: SmartRSSDB,
  nowMs = Date.now(),
  assertLockInTx?: () => Promise<void>,
): Promise<string[]> {
  const evictedIds: string[] = [];
  await db.transaction("rw", [db.client_meta, db.items_base, db.items, db.outbox], async () => {
    if (assertLockInTx) {
      await assertLockInTx();
    }
    const allOutbox = await db.outbox.toArray();
    const protectedIds = collectProtectedItemIds(allOutbox);
    const displayItems = await db.items.toArray();
    for (const item of displayItems) {
      if (protectedIds.has(item.id)) {
        continue;
      }
      if (isItemOutOfWindow(item, nowMs)) {
        evictedIds.push(item.id);
      }
    }
    if (evictedIds.length > 0) {
      await db.items.bulkDelete(evictedIds);
      await db.items_base.bulkDelete(evictedIds);
    }
  });
  return evictedIds;
}

export function humanizeRejectionReason(reason: string | null): string {
  switch (reason) {
    case "category_not_found":
      return "Target category was deleted on another device.";
    case "entity_not_found":
      return "Item or feed no longer exists on the server.";
    case "clock_skew":
      return "Device clock is more than 90 days behind the server.";
    case "epoch_mismatch":
      return "Server database was restored; pre-restore bulk mark-read was rolled back.";
    case "invalid_field_value":
      return "Server rejected the field value as invalid.";
    case "mutation_id_conflict":
      return "Duplicate mutation ID conflicted with an earlier device request.";
    case "unsupported_mutation_type":
      return "Server version does not support this mutation type yet.";
    default:
      return reason ? `Server rejected change (${reason}).` : "Server rejected change.";
  }
}
