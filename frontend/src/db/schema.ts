/**
 * Dexie.js IndexedDB v1 schema for SmartRSS (1c §5.1, U-01, U-02, U-03, U-06, U-14).
 *
 * Separates authoritative server-confirmed base tables (`items_base`, `feeds_base`) from
 * derived display tables (`items`, `feeds`), indexes numeric `starred_num: 0 | 1` (never
 * boolean keys, Checklist B2), stages multi-page bootstrap in `bootstrap_meta` /
 * `bootstrap_items`, and registers `versionchange` handling from v1.
 */

import Dexie, { type Table } from "dexie";
import type { DevicePlatform } from "../types/api";
import type {
  SyncAppSettingsDTO,
  SyncCategoryDTO,
  SyncClusterDTO,
  SyncFeedDTO,
  SyncInterestProfileDTO,
  SyncItemDTO,
  SyncMutationInput,
} from "../types/sync";
import type { UiPrefs } from "../types/ui";

export interface AuthMeta {
  device_id: string;
  device_name: string;
  platform: DevicePlatform;
  bearer_token: string;
  api_origin: string;
  paired_at: string;
}

export interface SyncCursorMeta {
  instance_epoch: string | null;
  sync_cursor: number | null;
  active_gen: number;
  last_sync_at: string | null;
  last_server_now: string | null;
  clock_drift_ms: number;
}

export interface SyncLockMeta {
  owner_tab_id: string;
  fencing_token: number;
  acquired_at: number;
  expires_at: number;
}

export interface FullResyncRequestMeta {
  request_id: string;
  requested_at: number;
}

export type ClientMetaValue =
  | AuthMeta
  | SyncCursorMeta
  | SyncLockMeta
  | FullResyncRequestMeta
  | SyncAppSettingsDTO
  | SyncInterestProfileDTO
  | UiPrefs
  | null;

export interface ClientMetaRow {
  key: string;
  value: ClientMetaValue;
}

export interface LocalItemBaseRecord extends SyncItemDTO {
  bootstrap_gen: number;
}

export interface LocalItemRecord extends LocalItemBaseRecord {
  effective_date: string;
  starred_num: 0 | 1;
}

export interface BootstrapMetaRecord {
  key: "active";
  phase: "fetching" | "committing";
  target_gen: number;
  page1_epoch: string;
  page1_watermark: number;
  server_now: string;
  next_cursor: string | null;
  categories: SyncCategoryDTO[];
  feeds: SyncFeedDTO[];
  clusters: SyncClusterDTO[];
  interest_profile: SyncInterestProfileDTO | null;
  app_settings: SyncAppSettingsDTO | null;
  pages_fetched: number;
  items_staged: number;
}

export interface OutboxRow {
  local_seq?: number;
  client_mutation_id: string;
  created_at: string;
  payload: SyncMutationInput;
  entity_key: string;
  target_absent: boolean;
  captured_item_ids: string[] | null;
  display_label: string;
  attempts: number;
  last_error: string | null;
}

export interface ReplayJournalRow {
  client_mutation_id: string;
  acknowledged_at: string;
  payload: SyncMutationInput;
}

export interface FailedMutationRow {
  client_mutation_id: string;
  failed_at: string;
  payload: SyncMutationInput;
  entity_key: string;
  target_absent: boolean;
  captured_item_ids: string[] | null;
  display_label: string;
  reason: string;
}

export interface SyncNotificationRow {
  id?: number;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  reason: string | null;
  mutation_type: string | null;
  entity_label: string | null;
  created_at: string;
  dismissed: 0 | 1;
}

export class SmartRSSDB extends Dexie {
  client_meta!: Table<ClientMetaRow, string>;
  categories!: Table<SyncCategoryDTO, string>;
  feeds_base!: Table<SyncFeedDTO, string>;
  feeds!: Table<SyncFeedDTO, string>;
  clusters!: Table<SyncClusterDTO, string>;
  items_base!: Table<LocalItemBaseRecord, string>;
  items!: Table<LocalItemRecord, string>;
  bootstrap_meta!: Table<BootstrapMetaRecord, string>;
  bootstrap_items!: Table<SyncItemDTO, string>;
  outbox!: Table<OutboxRow, number>;
  replay_journal!: Table<ReplayJournalRow, string>;
  failed_mutations!: Table<FailedMutationRow, string>;
  sync_notifications!: Table<SyncNotificationRow, number>;

  constructor(name = "smartrss_v1") {
    super(name);
    this.version(1).stores({
      client_meta: "key",
      categories: "id, sort_order, name",
      feeds_base: "id",
      feeds: "id, category_id, status, health_state, title",
      clusters: "id, max_relevance_tier, last_seen_at",
      items_base: "id, bootstrap_gen",
      items:
        "id, feed_id, cluster_id, state, starred_num, fetched_at, published_at, effective_date, read_at, ingest_sync_seq, bootstrap_gen, [state+effective_date], [feed_id+state], [feed_id+effective_date], [starred_num+effective_date]",
      bootstrap_meta: "key",
      bootstrap_items: "id",
      outbox: "++local_seq, &client_mutation_id, entity_key",
      replay_journal: "client_mutation_id, acknowledged_at",
      failed_mutations: "client_mutation_id, failed_at",
      sync_notifications: "++id, created_at, dismissed",
    });
  }
}

export function attachVersionChangeHandler(db: SmartRSSDB, onVersionChange: () => void): void {
  db.on("versionchange", () => {
    db.close();
    onVersionChange();
  });
}

let defaultDb: SmartRSSDB | null = null;

export function getDefaultDb(): SmartRSSDB {
  if (!defaultDb) {
    defaultDb = new SmartRSSDB("smartrss_v1");
  }
  return defaultDb;
}

export async function clearAllLocalData(db: SmartRSSDB): Promise<void> {
  await db.transaction(
    "rw",
    [
      db.client_meta,
      db.categories,
      db.feeds_base,
      db.feeds,
      db.clusters,
      db.items_base,
      db.items,
      db.bootstrap_meta,
      db.bootstrap_items,
      db.outbox,
      db.replay_journal,
      db.failed_mutations,
      db.sync_notifications,
    ],
    async () => {
      await Promise.all([
        db.client_meta.clear(),
        db.categories.clear(),
        db.feeds_base.clear(),
        db.feeds.clear(),
        db.clusters.clear(),
        db.items_base.clear(),
        db.items.clear(),
        db.bootstrap_meta.clear(),
        db.bootstrap_items.clear(),
        db.outbox.clear(),
        db.replay_journal.clear(),
        db.failed_mutations.clear(),
        db.sync_notifications.clear(),
      ]);
    },
  );
}
