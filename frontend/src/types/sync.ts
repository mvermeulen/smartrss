/** Exact TypeScript mirrors of backend Milestone 1b sync wire schemas (1b §6). */

export type FeedStatus = "active" | "paused" | "muted" | "unsubscribed";
export type FeedHealthState = "healthy" | "degraded" | "broken" | "gone";
export type ItemState = "unread" | "read" | "expired";
export type ExtractionStatus = "pending" | "ok" | "fallback_rss" | "failed";
export type DepthType =
  "news-brief" | "deep-dive" | "paper-report" | "opinion" | "release-notes" | "discussion";

export interface ResearchPolicy {
  enabled: boolean;
  fetch_pdfs: boolean;
  guidance: string;
  max_hops: number;
}

export interface ActiveFeedInstructionDTO {
  id: string;
  reason_text: string;
  created_at: string;
}

export interface SyncCategoryDTO {
  id: string;
  name: string;
  sort_order: number;
  updated_at: string;
}

export interface SyncFeedDTO {
  id: string;
  category_id: string | null;
  feed_url: string;
  site_url: string | null;
  title: string;
  status: FeedStatus;
  health_state: FeedHealthState;
  poll_interval_minutes: number;
  consecutive_errors: number;
  last_error: string | null;
  expiration_days_override: number | null;
  fetch_full_text: boolean;
  research_policy: ResearchPolicy;
  curation_instructions: string;
  active_feed_instructions: ActiveFeedInstructionDTO[];
  config_version: number;
  field_updated_at: Record<string, string>;
  updated_at: string;
}

export interface ItemEnrichmentDTO {
  status: "pending" | "completed" | "inherited" | "failed";
  tldr: string | null;
  takeaways: string[];
  depth_type: DepthType | null;
  relevance_tier: number | null;
  relevance_rationale: string | null;
  research_context: string | null;
  model_name?: string | null;
  created_at?: string | null;
}

export interface ResearchSourceDTO {
  id?: string;
  source_url: string;
  source_type: string;
  title: string | null;
  fetch_status: string;
}

export interface UserFeedbackDTO {
  feedback_type: string;
  reason_text: string | null;
  created_at: string;
}

export interface SyncItemDTO {
  id: string;
  feed_id: string;
  cluster_id: string | null;
  raw_url: string;
  canonical_url: string;
  title: string;
  author: string | null;
  published_at: string | null;
  fetched_at: string;
  raw_summary: string | null;
  summary_truncated: boolean;
  lead_excerpt: string | null;
  extraction_status: ExtractionStatus;
  state: ItemState;
  is_starred: boolean;
  read_at: string | null;
  starred_at: string | null;
  expired_at: string | null;
  unexpired_at: string | null;
  read_state_updated_at: string;
  starred_updated_at: string;
  ingest_sync_seq: number;
  enrichment: ItemEnrichmentDTO | null;
  tags: string[];
  research_sources: ResearchSourceDTO[];
  user_feedback: UserFeedbackDTO | null;
  updated_at: string;
}

export interface SyncClusterDTO {
  id: string;
  representative_item_id: string | null;
  title: string;
  max_relevance_tier: number | null;
  item_count: number;
  last_seen_at: string;
}

export interface SyncInterestProfileDTO {
  version: number;
  profile_markdown: string;
}

export interface SyncAppSettingsDTO {
  default_expiration_days: number;
  backfill_cap: number;
  default_poll_interval_minutes: number;
  updated_at: string;
}

export interface SyncBootstrapResponse {
  instance_epoch: string;
  server_now: string;
  watermark: number | null;
  categories: SyncCategoryDTO[] | null;
  feeds: SyncFeedDTO[] | null;
  clusters: SyncClusterDTO[] | null;
  interest_profile: SyncInterestProfileDTO | null;
  app_settings: SyncAppSettingsDTO | null;
  items: SyncItemDTO[];
  next_cursor: string | null;
  has_more: boolean;
}

export type SyncEntityType =
  "category" | "feed" | "item" | "cluster" | "interest_profile" | "app_settings";

export interface SyncPullChange {
  seq: number;
  entity_type: SyncEntityType | string;
  entity_id: string;
  op: "upsert" | "delete";
  changed_at: string;
  data:
    | SyncCategoryDTO
    | SyncFeedDTO
    | SyncItemDTO
    | SyncClusterDTO
    | SyncInterestProfileDTO
    | SyncAppSettingsDTO
    | null;
}

export type ResyncReason = "epoch_mismatch" | "cursor_ahead" | "cursor_pruned";

export interface SyncPullResponse {
  resync_required: boolean;
  resync_reason: ResyncReason | null;
  instance_epoch: string;
  server_now: string;
  watermark: number;
  next_seq: number;
  has_more: boolean;
  changes: SyncPullChange[];
}

export interface BaseMutation {
  client_mutation_id: string;
  client_timestamp: string;
}

export interface ItemSetStateMutation extends BaseMutation {
  type: "item_set_state";
  item_id: string;
  state: "read" | "unread";
}

export interface ItemSetStarredMutation extends BaseMutation {
  type: "item_set_starred";
  item_id: string;
  is_starred: boolean;
}

export type BulkReadScope = "all" | "feed" | "category" | "items";

export interface ItemMarkBulkReadMutation extends BaseMutation {
  type: "item_mark_bulk_read";
  scope: BulkReadScope;
  up_to_sync_seq?: number | null;
  epoch?: string | null;
  feed_id?: string | null;
  category_id?: string | null;
  older_than_ts?: string | null;
  item_ids?: string[] | null;
}

export type FeedMutableField =
  | "status"
  | "category_id"
  | "expiration_days_override"
  | "research_policy.enabled"
  | "research_policy.fetch_pdfs";

export interface FeedSetFieldMutation extends BaseMutation {
  type: "feed_set_field";
  feed_id: string;
  field: FeedMutableField;
  value: string | number | boolean | null;
}

export type SyncMutationInput =
  ItemSetStateMutation | ItemSetStarredMutation | ItemMarkBulkReadMutation | FeedSetFieldMutation;

export interface SyncPushRequest {
  mutations: SyncMutationInput[];
}

export interface SyncPushItemSnapshot {
  id: string;
  state: ItemState;
  is_starred: boolean;
  read_at: string | null;
  starred_at: string | null;
  expired_at: string | null;
  unexpired_at: string | null;
  read_state_updated_at: string;
  starred_updated_at: string;
  updated_at: string;
}

export interface SyncPushFeedSnapshot {
  id: string;
  status: FeedStatus;
  category_id: string | null;
  expiration_days_override: number | null;
  research_policy: ResearchPolicy;
  config_version: number;
  field_updated_at: Record<string, string>;
  updated_at: string;
}

export interface SyncPushResult {
  client_mutation_id: string;
  outcome: "applied" | "superseded" | "rejected";
  reason: string | null;
  affected_count: number | null;
  current_entity_type: "item" | "feed" | null;
  current_entity: SyncPushItemSnapshot | SyncPushFeedSnapshot | null;
}

export interface SyncPushResponse {
  instance_epoch: string;
  server_now: string;
  results: SyncPushResult[];
}
