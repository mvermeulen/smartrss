/** Exact TypeScript mirrors of backend Milestone 1a/1b REST & System API schemas. */

import type {
  ExtractionStatus,
  FeedHealthState,
  FeedStatus,
  ItemEnrichmentDTO,
  ItemState,
  ResearchPolicy,
  ResearchSourceDTO,
  UserFeedbackDTO,
} from "./sync";

export interface ClientRuntimeConfig {
  mode: "localhost" | "lan_fallback";
  api_origin: string;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  db: "ok" | "error";
  version: string;
}

export type DevicePlatform = "ubuntu" | "android" | "web";

export interface PairExchangeRequest {
  code: string;
  device_name?: string | null;
  platform?: DevicePlatform | null;
}

export interface PairExchangeResponse {
  device_id: string;
  device_name: string;
  platform: DevicePlatform;
  bearer_token: string;
}

export interface CreatePairingCodeRequest {
  device_name: string;
  platform: DevicePlatform;
}

export interface CreatePairingCodeResponse {
  code: string;
  expires_at: string;
}

export interface DeviceResponse {
  id: string;
  name: string;
  platform: DevicePlatform | string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
}

export interface CategoryCreateRequest {
  name: string;
  sort_order?: number;
}

export interface CategoryUpdateRequest {
  name?: string;
  sort_order?: number;
}

export interface CategoryResponse {
  id: string;
  name: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface FeedCreateRequest {
  feed_url: string;
  title?: string | null;
  category_id?: string | null;
  poll_interval_minutes?: number | null;
  expiration_days_override?: number | null;
  fetch_full_text?: boolean;
  research_policy?: ResearchPolicy | null;
  curation_instructions?: string;
}

export interface FeedUpdateRequest {
  feed_url?: string;
  title?: string;
  status?: FeedStatus;
  category_id?: string | null;
  poll_interval_minutes?: number;
  expiration_days_override?: number | null;
  fetch_full_text?: boolean;
  research_policy?: ResearchPolicy;
  curation_instructions?: string;
  expected_config_version?: number;
}

export interface FeedResponse {
  id: string;
  category_id: string | null;
  feed_url: string;
  site_url: string | null;
  title: string;
  status: FeedStatus;
  health_state: FeedHealthState;
  poll_interval_minutes: number;
  last_fetched_at: string | null;
  last_success_at: string | null;
  next_fetch_at: string | null;
  consecutive_errors: number;
  last_error: string | null;
  initial_backfill_done: boolean;
  expiration_days_override: number | null;
  fetch_full_text: boolean;
  research_policy: ResearchPolicy;
  curation_instructions: string;
  field_updated_at: Record<string, string>;
  config_version: number;
  created_at: string;
  updated_at: string;
}

export interface FeedPollTriggerResponse {
  feed_id: string;
  job_id: number | null;
  queued: boolean;
}

export interface OpmlImportRequest {
  opml_xml: string;
  default_category_id?: string | null;
}

export interface OpmlImportResponse {
  imported: number;
  reactivated: number;
  skipped_duplicates: number;
  rejected_invalid: number;
}

export interface ItemResponse {
  id: string;
  feed_id: string;
  guid: string;
  raw_url: string;
  canonical_url: string;
  title: string;
  author: string | null;
  published_at: string | null;
  fetched_at: string;
  raw_summary: string | null;
  extracted_text: string | null;
  extraction_status: ExtractionStatus;
  entry_hash: string | null;
  content_hash: string | null;
  state: ItemState;
  is_starred: boolean;
  read_at: string | null;
  starred_at: string | null;
  expired_at: string | null;
  read_state_updated_at: string;
  starred_updated_at: string;
  unexpired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PipelineRunResponse {
  id: string;
  trigger: string;
  stages: string[];
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  items_polled: number;
  items_extracted: number;
  items_clustered: number;
  items_researched: number;
  items_enriched: number;
  jobs_failed: number;
  total_llm_ms: number;
  notes: string | null;
}

export interface DeadLetterJobResponse {
  id: number;
  job_type: string;
  dedupe_key: string;
  payload: Record<string, unknown>;
  feed_id: string | null;
  item_id: string | null;
  run_id: string | null;
  priority: number;
  status: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface DeadLetterListResponse {
  jobs: DeadLetterJobResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface SystemHealthResponse {
  db: "ok" | "error";
  instance_epoch: string;
  current_seq: number;
  queue_counts: Record<string, Record<string, number>>;
  dead_letter_count: number;
  recent_runs: PipelineRunResponse[];
}

export interface StageMetricRow {
  job_type: string;
  total_count: number;
  ok_count: number;
  failed_count: number;
  skipped_count: number;
  failure_rate: number;
  avg_duration_ms: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
  max_duration_ms: number;
  avg_queue_wait_ms: number;
}

export interface FeedCostRow {
  feed_id: string;
  feed_title: string | null;
  total_jobs: number;
  failed_jobs: number;
  total_duration_ms: number;
  avg_duration_ms: number;
}

export type MetricsWindow = "24h" | "7d" | "30d";

export interface SystemMetricsResponse {
  window: MetricsWindow;
  since: string;
  stages: StageMetricRow[];
  per_feed: FeedCostRow[];
}

export interface AppSettingsUpdateRequest {
  default_expiration_days?: number;
  backfill_cap?: number;
  default_poll_interval_minutes?: number;
}

export interface SnippetFragmentDTO {
  text: string;
  highlight: boolean;
}

export interface SearchItemDTO {
  id: string;
  feed_id: string;
  cluster_id: string | null;
  raw_url: string;
  canonical_url: string;
  title: string;
  author: string | null;
  published_at: string | null;
  fetched_at: string;
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

export interface SearchResultDTO {
  item: SearchItemDTO;
  rank: number | null;
  snippet: SnippetFragmentDTO[][];
}

export interface SearchResponse {
  query: string | null;
  sort: "relevance" | "newest";
  total: number;
  limit: number;
  offset: number;
  results: SearchResultDTO[];
}
