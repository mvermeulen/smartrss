import type { LocalItemBaseRecord, LocalItemRecord } from "../../db/schema";
import { toLocalItemBase, toLocalItemDisplay } from "../../sync/reconciliation";
import type { SearchItemDTO } from "../../types/api";
import type { SyncCategoryDTO, SyncFeedDTO, SyncItemDTO } from "../../types/sync";

export function makeCategory(overrides: Partial<SyncCategoryDTO> = {}): SyncCategoryDTO {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    name: "Tech",
    sort_order: 0,
    updated_at: "2026-11-25T10:00:00.000Z",
    ...overrides,
  };
}

export function makeFeed(overrides: Partial<SyncFeedDTO> = {}): SyncFeedDTO {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    category_id: "11111111-1111-4111-8111-111111111111",
    feed_url: "https://example.com/rss.xml",
    site_url: "https://example.com",
    title: "Example Engineering",
    status: "active",
    health_state: "healthy",
    poll_interval_minutes: 60,
    consecutive_errors: 0,
    last_error: null,
    expiration_days_override: null,
    fetch_full_text: true,
    research_policy: {
      enabled: false,
      fetch_pdfs: false,
      guidance: "",
      max_hops: 1,
    },
    curation_instructions: "",
    active_feed_instructions: [],
    config_version: 1,
    field_updated_at: {
      status: "2026-11-25T10:00:00.000Z",
    },
    updated_at: "2026-11-25T10:00:00.000Z",
    ...overrides,
  };
}

export function makeSyncItem(overrides: Partial<SyncItemDTO> = {}): SyncItemDTO {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    feed_id: "22222222-2222-4222-8222-222222222222",
    cluster_id: null,
    raw_url: "https://example.com/posts/1",
    canonical_url: "https://example.com/posts/1",
    title: "PostgreSQL pgvector HNSW deep dive",
    author: "Ada Lovelace",
    published_at: "2026-11-25T09:00:00.000Z",
    fetched_at: "2026-11-25T10:00:00.000Z",
    raw_summary: "<p>Summary about pgvector indexes and sync watermarks.</p>",
    summary_truncated: false,
    lead_excerpt: "Summary about pgvector indexes and sync watermarks.",
    extraction_status: "ok",
    state: "unread",
    is_starred: false,
    read_at: null,
    starred_at: null,
    expired_at: null,
    unexpired_at: null,
    read_state_updated_at: "2026-11-25T10:00:00.000Z",
    starred_updated_at: "2026-11-25T10:00:00.000Z",
    ingest_sync_seq: 10,
    enrichment: null,
    tags: [],
    research_sources: [],
    user_feedback: null,
    updated_at: "2026-11-25T10:00:00.000Z",
    ...overrides,
  };
}

export function makeLocalBase(
  overrides: Partial<SyncItemDTO> = {},
  bootstrapGen = 1,
): LocalItemBaseRecord {
  return toLocalItemBase(makeSyncItem(overrides), bootstrapGen);
}

export function makeLocalItem(
  overrides: Partial<SyncItemDTO> = {},
  bootstrapGen = 1,
): LocalItemRecord {
  return toLocalItemDisplay(makeLocalBase(overrides, bootstrapGen));
}

export function makeSearchItem(overrides: Partial<SearchItemDTO> = {}): SearchItemDTO {
  const item = makeSyncItem(overrides);
  return {
    id: item.id,
    feed_id: item.feed_id,
    cluster_id: item.cluster_id,
    raw_url: item.raw_url,
    canonical_url: item.canonical_url,
    title: item.title,
    author: item.author,
    published_at: item.published_at,
    fetched_at: item.fetched_at,
    lead_excerpt: item.lead_excerpt,
    extraction_status: item.extraction_status,
    state: item.state,
    is_starred: item.is_starred,
    read_at: item.read_at,
    starred_at: item.starred_at,
    expired_at: item.expired_at,
    unexpired_at: item.unexpired_at,
    read_state_updated_at: item.read_state_updated_at,
    starred_updated_at: item.starred_updated_at,
    ingest_sync_seq: item.ingest_sync_seq,
    enrichment: item.enrichment,
    tags: item.tags,
    research_sources: item.research_sources,
    user_feedback: item.user_feedback,
    updated_at: item.updated_at,
  };
}
