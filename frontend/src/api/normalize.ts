/**
 * Canonical 24-char UTC ISO-8601 timestamp normalizer at the API DTO boundary (U-07, D6).
 *
 * Normalizes Pydantic/Python formats (`...Z`, `....123456Z`, `...+00:00`, offsets) into
 * fixed-width `YYYY-MM-DDTHH:mm:ss.sssZ` so IndexedDB string indexes sort identically to
 * numeric epoch time.
 */

import type {
  CategoryResponse,
  DeadLetterJobResponse,
  DeadLetterListResponse,
  DeviceResponse,
  FeedResponse,
  ItemResponse,
  PipelineRunResponse,
  SearchItemDTO,
  SearchResponse,
  SystemHealthResponse,
  SystemMetricsResponse,
} from "../types/api";
import type {
  SyncAppSettingsDTO,
  SyncBootstrapResponse,
  SyncCategoryDTO,
  SyncClusterDTO,
  SyncFeedDTO,
  SyncItemDTO,
  SyncPullChange,
  SyncPullResponse,
  SyncPushFeedSnapshot,
  SyncPushItemSnapshot,
  SyncPushResponse,
  SyncPushResult,
} from "../types/sync";

export function toCanonicalIso(raw: string): string {
  const ms = Date.parse(raw);
  if (Number.isNaN(ms)) {
    throw new Error(`Invalid ISO timestamp: ${raw}`);
  }
  return new Date(ms).toISOString();
}

export function toCanonicalIsoOrNull(raw: string | null | undefined): string | null {
  if (raw == null) {
    return null;
  }
  return toCanonicalIso(raw);
}

function normalizeTimestampMap(map: Record<string, string> | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!map) {
    return out;
  }
  for (const [k, v] of Object.entries(map)) {
    out[k] = toCanonicalIso(v);
  }
  return out;
}

export function normalizeSyncCategory(dto: SyncCategoryDTO): SyncCategoryDTO {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    updated_at: toCanonicalIso(dto.updated_at),
  };
}

export function normalizeSyncFeed(dto: SyncFeedDTO): SyncFeedDTO {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    category_id: dto.category_id ? dto.category_id.toLowerCase() : null,
    active_feed_instructions: (dto.active_feed_instructions ?? []).map((note) => ({
      ...note,
      id: note.id.toLowerCase(),
      created_at: toCanonicalIso(note.created_at),
    })),
    field_updated_at: normalizeTimestampMap(dto.field_updated_at),
    updated_at: toCanonicalIso(dto.updated_at),
  };
}

export function normalizeSyncItem(dto: SyncItemDTO): SyncItemDTO {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    feed_id: dto.feed_id.toLowerCase(),
    cluster_id: dto.cluster_id ? dto.cluster_id.toLowerCase() : null,
    published_at: toCanonicalIsoOrNull(dto.published_at),
    fetched_at: toCanonicalIso(dto.fetched_at),
    read_at: toCanonicalIsoOrNull(dto.read_at),
    starred_at: toCanonicalIsoOrNull(dto.starred_at),
    expired_at: toCanonicalIsoOrNull(dto.expired_at),
    unexpired_at: toCanonicalIsoOrNull(dto.unexpired_at),
    read_state_updated_at: toCanonicalIso(dto.read_state_updated_at),
    starred_updated_at: toCanonicalIso(dto.starred_updated_at),
    updated_at: toCanonicalIso(dto.updated_at),
    tags: dto.tags ?? [],
    research_sources: dto.research_sources ?? [],
    user_feedback: dto.user_feedback
      ? {
          ...dto.user_feedback,
          created_at: toCanonicalIso(dto.user_feedback.created_at),
        }
      : null,
  };
}

export function normalizeSyncCluster(dto: SyncClusterDTO): SyncClusterDTO {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    representative_item_id: dto.representative_item_id
      ? dto.representative_item_id.toLowerCase()
      : null,
    last_seen_at: toCanonicalIso(dto.last_seen_at),
  };
}

export function normalizeSyncAppSettings(dto: SyncAppSettingsDTO): SyncAppSettingsDTO {
  return {
    ...dto,
    updated_at: toCanonicalIso(dto.updated_at),
  };
}

export function normalizeBootstrapResponse(res: SyncBootstrapResponse): SyncBootstrapResponse {
  return {
    ...res,
    instance_epoch: res.instance_epoch.toLowerCase(),
    server_now: toCanonicalIso(res.server_now),
    categories: res.categories ? res.categories.map(normalizeSyncCategory) : null,
    feeds: res.feeds ? res.feeds.map(normalizeSyncFeed) : null,
    clusters: res.clusters ? res.clusters.map(normalizeSyncCluster) : null,
    app_settings: res.app_settings ? normalizeSyncAppSettings(res.app_settings) : null,
    items: res.items.map(normalizeSyncItem),
    next_cursor: res.next_cursor ? res.next_cursor.toLowerCase() : null,
  };
}

function normalizePullChange(change: SyncPullChange): SyncPullChange {
  const base = {
    ...change,
    entity_id: change.entity_id.toLowerCase(),
    changed_at: toCanonicalIso(change.changed_at),
  };
  if (change.op === "delete" || change.data == null) {
    return { ...base, data: null };
  }
  switch (change.entity_type) {
    case "category":
      return { ...base, data: normalizeSyncCategory(change.data as SyncCategoryDTO) };
    case "feed":
      return { ...base, data: normalizeSyncFeed(change.data as SyncFeedDTO) };
    case "item":
      return { ...base, data: normalizeSyncItem(change.data as SyncItemDTO) };
    case "cluster":
      return { ...base, data: normalizeSyncCluster(change.data as SyncClusterDTO) };
    case "app_settings":
      return { ...base, data: normalizeSyncAppSettings(change.data as SyncAppSettingsDTO) };
    default:
      return base;
  }
}

export function normalizePullResponse(res: SyncPullResponse): SyncPullResponse {
  return {
    ...res,
    instance_epoch: res.instance_epoch.toLowerCase(),
    server_now: toCanonicalIso(res.server_now),
    changes: res.changes.map(normalizePullChange),
  };
}

function normalizePushItemSnapshot(snap: SyncPushItemSnapshot): SyncPushItemSnapshot {
  return {
    ...snap,
    id: snap.id.toLowerCase(),
    read_at: toCanonicalIsoOrNull(snap.read_at),
    starred_at: toCanonicalIsoOrNull(snap.starred_at),
    expired_at: toCanonicalIsoOrNull(snap.expired_at),
    unexpired_at: toCanonicalIsoOrNull(snap.unexpired_at),
    read_state_updated_at: toCanonicalIso(snap.read_state_updated_at),
    starred_updated_at: toCanonicalIso(snap.starred_updated_at),
    updated_at: toCanonicalIso(snap.updated_at),
  };
}

function normalizePushFeedSnapshot(snap: SyncPushFeedSnapshot): SyncPushFeedSnapshot {
  return {
    ...snap,
    id: snap.id.toLowerCase(),
    category_id: snap.category_id ? snap.category_id.toLowerCase() : null,
    field_updated_at: normalizeTimestampMap(snap.field_updated_at),
    updated_at: toCanonicalIso(snap.updated_at),
  };
}

function normalizePushResult(result: SyncPushResult): SyncPushResult {
  const base = {
    ...result,
    client_mutation_id: result.client_mutation_id.toLowerCase(),
  };
  if (result.current_entity == null) {
    return base;
  }
  if (result.current_entity_type === "item") {
    return {
      ...base,
      current_entity: normalizePushItemSnapshot(result.current_entity as SyncPushItemSnapshot),
    };
  }
  if (result.current_entity_type === "feed") {
    return {
      ...base,
      current_entity: normalizePushFeedSnapshot(result.current_entity as SyncPushFeedSnapshot),
    };
  }
  return base;
}

export function normalizePushResponse(res: SyncPushResponse): SyncPushResponse {
  return {
    instance_epoch: res.instance_epoch.toLowerCase(),
    server_now: toCanonicalIso(res.server_now),
    results: res.results.map(normalizePushResult),
  };
}

export function normalizeSearchItem(dto: SearchItemDTO): SearchItemDTO {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    feed_id: dto.feed_id.toLowerCase(),
    cluster_id: dto.cluster_id ? dto.cluster_id.toLowerCase() : null,
    published_at: toCanonicalIsoOrNull(dto.published_at),
    fetched_at: toCanonicalIso(dto.fetched_at),
    read_at: toCanonicalIsoOrNull(dto.read_at),
    starred_at: toCanonicalIsoOrNull(dto.starred_at),
    expired_at: toCanonicalIsoOrNull(dto.expired_at),
    unexpired_at: toCanonicalIsoOrNull(dto.unexpired_at),
    read_state_updated_at: toCanonicalIso(dto.read_state_updated_at),
    starred_updated_at: toCanonicalIso(dto.starred_updated_at),
    updated_at: toCanonicalIso(dto.updated_at),
    tags: dto.tags ?? [],
    research_sources: dto.research_sources ?? [],
  };
}

export function normalizeSearchResponse(res: SearchResponse): SearchResponse {
  return {
    ...res,
    results: res.results.map((r) => ({
      ...r,
      item: normalizeSearchItem(r.item),
    })),
  };
}

export function normalizeFeedResponse(dto: FeedResponse): FeedResponse {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    category_id: dto.category_id ? dto.category_id.toLowerCase() : null,
    last_fetched_at: toCanonicalIsoOrNull(dto.last_fetched_at),
    last_success_at: toCanonicalIsoOrNull(dto.last_success_at),
    next_fetch_at: toCanonicalIsoOrNull(dto.next_fetch_at),
    field_updated_at: normalizeTimestampMap(dto.field_updated_at),
    created_at: toCanonicalIso(dto.created_at),
    updated_at: toCanonicalIso(dto.updated_at),
  };
}

export function normalizeCategoryResponse(dto: CategoryResponse): CategoryResponse {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    created_at: toCanonicalIso(dto.created_at),
    updated_at: toCanonicalIso(dto.updated_at),
  };
}

export function normalizeItemResponse(dto: ItemResponse): ItemResponse {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    feed_id: dto.feed_id.toLowerCase(),
    published_at: toCanonicalIsoOrNull(dto.published_at),
    fetched_at: toCanonicalIso(dto.fetched_at),
    read_at: toCanonicalIsoOrNull(dto.read_at),
    starred_at: toCanonicalIsoOrNull(dto.starred_at),
    expired_at: toCanonicalIsoOrNull(dto.expired_at),
    read_state_updated_at: toCanonicalIso(dto.read_state_updated_at),
    starred_updated_at: toCanonicalIso(dto.starred_updated_at),
    unexpired_at: toCanonicalIsoOrNull(dto.unexpired_at),
    created_at: toCanonicalIso(dto.created_at),
    updated_at: toCanonicalIso(dto.updated_at),
  };
}

export function normalizeDeviceResponse(dto: DeviceResponse): DeviceResponse {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    created_at: toCanonicalIso(dto.created_at),
    last_seen_at: toCanonicalIsoOrNull(dto.last_seen_at),
    revoked_at: toCanonicalIsoOrNull(dto.revoked_at),
  };
}

export function normalizePipelineRun(dto: PipelineRunResponse): PipelineRunResponse {
  return {
    ...dto,
    id: dto.id.toLowerCase(),
    started_at: toCanonicalIso(dto.started_at),
    finished_at: toCanonicalIsoOrNull(dto.finished_at),
  };
}

export function normalizeSystemHealth(dto: SystemHealthResponse): SystemHealthResponse {
  return {
    ...dto,
    instance_epoch: dto.instance_epoch.toLowerCase(),
    recent_runs: dto.recent_runs.map(normalizePipelineRun),
  };
}

export function normalizeSystemMetrics(dto: SystemMetricsResponse): SystemMetricsResponse {
  return {
    ...dto,
    since: toCanonicalIso(dto.since),
    per_feed: dto.per_feed.map((f) => ({
      ...f,
      feed_id: f.feed_id.toLowerCase(),
    })),
  };
}

export function normalizeDeadLetterJob(dto: DeadLetterJobResponse): DeadLetterJobResponse {
  return {
    ...dto,
    feed_id: dto.feed_id ? dto.feed_id.toLowerCase() : null,
    item_id: dto.item_id ? dto.item_id.toLowerCase() : null,
    run_id: dto.run_id ? dto.run_id.toLowerCase() : null,
    finished_at: toCanonicalIsoOrNull(dto.finished_at),
    created_at: toCanonicalIso(dto.created_at),
  };
}

export function normalizeDeadLetterList(dto: DeadLetterListResponse): DeadLetterListResponse {
  return {
    ...dto,
    jobs: dto.jobs.map(normalizeDeadLetterJob),
  };
}
