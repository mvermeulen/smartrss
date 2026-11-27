/**
 * Typed SmartRSS REST & Sync API client (1c §3.2, §6, §10).
 *
 * Strictly CORS-safe: sends ONLY `Authorization`, `Accept`, and `Content-Type: application/json`
 * (matching backend `CORSMiddleware`), with `credentials: "omit"`. Response-side
 * `Cache-Control: no-store` from the backend replaces request-side `cache: "no-store"` on API
 * calls so Chromium uses the `Access-Control-Max-Age` preflight cache (E-07).
 * Normalizes all incoming timestamps via `normalize.ts` (U-07).
 */

import type {
  AppSettingsUpdateRequest,
  CategoryCreateRequest,
  CategoryResponse,
  CategoryUpdateRequest,
  ClientRuntimeConfig,
  CreatePairingCodeRequest,
  CreatePairingCodeResponse,
  DeadLetterJobResponse,
  DeadLetterListResponse,
  DeviceResponse,
  FeedCreateRequest,
  FeedPollTriggerResponse,
  FeedResponse,
  FeedUpdateRequest,
  HealthResponse,
  ItemResponse,
  MetricsWindow,
  OpmlImportRequest,
  OpmlImportResponse,
  PairExchangeRequest,
  PairExchangeResponse,
  SearchResponse,
  SystemHealthResponse,
  SystemMetricsResponse,
} from "../types/api";
import type {
  SyncAppSettingsDTO,
  SyncBootstrapResponse,
  SyncPullResponse,
  SyncPushRequest,
  SyncPushResponse,
} from "../types/sync";
import {
  normalizeBootstrapResponse,
  normalizeCategoryResponse,
  normalizeDeadLetterJob,
  normalizeDeadLetterList,
  normalizeDeviceResponse,
  normalizeFeedResponse,
  normalizeItemResponse,
  normalizePullResponse,
  normalizePushResponse,
  normalizeSearchResponse,
  normalizeSyncAppSettings,
  normalizeSystemHealth,
  normalizeSystemMetrics,
  toCanonicalIso,
} from "./normalize";

export class ApiHttpError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    message: string,
    detail: unknown = null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiHttpError";
    this.status = status;
    this.detail = detail;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export class ApiNetworkError extends Error {
  readonly isTimeout: boolean;

  constructor(message: string, isTimeout = false) {
    super(message);
    this.name = "ApiNetworkError";
    this.isTimeout = isTimeout;
  }
}

export interface SearchQueryParams {
  q?: string | null;
  feed_id?: string | null;
  category_id?: string | null;
  state?: "unread" | "read" | "expired" | null;
  is_starred?: boolean | null;
  date_from?: string | null;
  date_to?: string | null;
  sort?: "relevance" | "newest";
  limit?: number;
  offset?: number;
}

export async function fetchClientRuntimeConfig(
  fetchImpl?: typeof fetch,
): Promise<ClientRuntimeConfig | null> {
  const doFetch: typeof fetch = fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  const controller = new AbortController();
  const timer = globalThis.setTimeout(() => controller.abort(), 3_000);
  try {
    const res = await doFetch("/client-config.json", {
      method: "GET",
      cache: "no-store",
      credentials: "omit",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!res.ok) {
      return null;
    }
    const parsed = (await res.json()) as Partial<ClientRuntimeConfig>;
    if (
      (parsed.mode === "localhost" || parsed.mode === "lan_fallback") &&
      typeof parsed.api_origin === "string"
    ) {
      return {
        mode: parsed.mode,
        api_origin: parsed.api_origin.replace(/\/+$/, ""),
      };
    }
    return null;
  } catch {
    return null;
  } finally {
    globalThis.clearTimeout(timer);
  }
}

function parseRetryAfter(header: string | null): number | null {
  if (!header) {
    return null;
  }
  const num = Number(header.trim());
  if (Number.isFinite(num) && num >= 0) {
    return Math.ceil(num);
  }
  return null;
}

export class SmartRSSApiClient {
  private readonly apiOrigin: string;
  private readonly token: string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(apiOrigin: string, token: string | null = null, fetchImpl?: typeof fetch) {
    this.apiOrigin = apiOrigin.replace(/\/+$/, "");
    this.token = token;
    this.fetchImpl = fetchImpl
      ? (input, init) => fetchImpl(input, init)
      : (input, init) => globalThis.fetch(input, init);
  }

  get origin(): string {
    return this.apiOrigin;
  }

  private url(path: string): string {
    return `${this.apiOrigin}${path}`;
  }

  private async requestJson<T>(
    method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
    path: string,
    options: {
      body?: unknown;
      auth?: boolean;
      timeoutMs?: number;
      accept?: string;
    } = {},
  ): Promise<T> {
    const { body, auth = true, timeoutMs = 15_000, accept = "application/json" } = options;
    const headers: Record<string, string> = { Accept: accept };
    if (auth) {
      if (!this.token) {
        throw new ApiHttpError(401, "No bearer token configured");
      }
      headers.Authorization = `Bearer ${this.token}`;
    }
    const isMutation = method !== "GET";
    if (isMutation) {
      headers["Content-Type"] = "application/json";
    }

    const controller = new AbortController();
    const timer = globalThis.setTimeout(() => controller.abort(), timeoutMs);
    try {
      let response: Response;
      try {
        response = await this.fetchImpl(this.url(path), {
          method,
          headers,
          body: body !== undefined ? JSON.stringify(body) : undefined,
          credentials: "omit",
          signal: controller.signal,
        });
      } catch (err) {
        const isAbort = err instanceof DOMException && err.name === "AbortError";
        throw new ApiNetworkError(
          isAbort ? `Request timed out after ${timeoutMs}ms` : String(err),
          isAbort,
        );
      }

      if (response.status === 204) {
        return undefined as T;
      }

      const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
      if (!response.ok) {
        let detail: unknown = null;
        let msg = `HTTP ${response.status}`;
        try {
          const errJson = (await response.json()) as { detail?: unknown };
          detail = errJson.detail ?? errJson;
          if (typeof detail === "string") {
            msg = detail;
          } else if (
            detail &&
            typeof detail === "object" &&
            "message" in detail &&
            typeof (detail as { message: unknown }).message === "string"
          ) {
            msg = (detail as { message: string }).message;
          }
        } catch (err) {
          if (err instanceof DOMException && err.name === "AbortError") {
            throw new ApiNetworkError(`Request timed out after ${timeoutMs}ms`, true);
          }
          // Non-JSON error response
        }
        throw new ApiHttpError(response.status, msg, detail, retryAfter);
      }

      try {
        if (accept === "application/xml") {
          return (await response.text()) as T;
        }
        return (await response.json()) as T;
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          throw new ApiNetworkError(`Request timed out after ${timeoutMs}ms`, true);
        }
        throw new ApiHttpError(
          502,
          `Invalid ${accept === "application/xml" ? "XML" : "JSON"} response from server: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    } finally {
      globalThis.clearTimeout(timer);
    }
  }

  // --- Health & Auth ---
  async health(timeoutMs = 3_000): Promise<HealthResponse> {
    return this.requestJson<HealthResponse>("GET", "/api/v1/health", {
      auth: false,
      timeoutMs,
    });
  }

  async pair(body: PairExchangeRequest): Promise<PairExchangeResponse> {
    const res = await this.requestJson<PairExchangeResponse>("POST", "/api/v1/auth/pair", {
      body,
      auth: false,
    });
    return { ...res, device_id: res.device_id.toLowerCase() };
  }

  async createPairingCode(body: CreatePairingCodeRequest): Promise<CreatePairingCodeResponse> {
    const res = await this.requestJson<CreatePairingCodeResponse>(
      "POST",
      "/api/v1/auth/pairing-codes",
      { body },
    );
    return { ...res, expires_at: toCanonicalIso(res.expires_at) };
  }

  async listDevices(): Promise<DeviceResponse[]> {
    const rows = await this.requestJson<DeviceResponse[]>("GET", "/api/v1/devices");
    return rows.map(normalizeDeviceResponse);
  }

  async revokeDevice(deviceId: string, timeoutMs = 10_000): Promise<DeviceResponse> {
    const res = await this.requestJson<DeviceResponse>(
      "POST",
      `/api/v1/devices/${encodeURIComponent(deviceId)}/revoke`,
      { timeoutMs },
    );
    return normalizeDeviceResponse(res);
  }

  // --- Sync Protocol ---
  async syncBootstrap(cursor?: string | null, limit = 500): Promise<SyncBootstrapResponse> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) {
      params.set("cursor", cursor);
    }
    const res = await this.requestJson<SyncBootstrapResponse>(
      "GET",
      `/api/v1/sync/bootstrap?${params.toString()}`,
      { timeoutMs: 20_000 },
    );
    return normalizeBootstrapResponse(res);
  }

  async syncPull(sinceSeq: number, epoch: string, limit = 200): Promise<SyncPullResponse> {
    const params = new URLSearchParams({
      since_seq: String(sinceSeq),
      epoch,
      limit: String(limit),
    });
    const res = await this.requestJson<SyncPullResponse>(
      "GET",
      `/api/v1/sync/pull?${params.toString()}`,
      { timeoutMs: 15_000 },
    );
    return normalizePullResponse(res);
  }

  async syncPush(body: SyncPushRequest): Promise<SyncPushResponse> {
    const res = await this.requestJson<SyncPushResponse>("POST", "/api/v1/sync/push", {
      body,
      timeoutMs: 15_000,
    });
    return normalizePushResponse(res);
  }

  // --- Search & Item Detail ---
  async searchArchive(params: SearchQueryParams): Promise<SearchResponse> {
    const qs = new URLSearchParams();
    if (params.q && params.q.trim()) qs.set("q", params.q.trim());
    if (params.feed_id) qs.set("feed_id", params.feed_id);
    if (params.category_id) qs.set("category_id", params.category_id);
    if (params.state) qs.set("state", params.state);
    if (params.is_starred !== undefined && params.is_starred !== null) {
      qs.set("is_starred", String(params.is_starred));
    }
    if (params.date_from) qs.set("date_from", params.date_from);
    if (params.date_to) qs.set("date_to", params.date_to);
    if (params.sort) qs.set("sort", params.sort);
    if (params.limit !== undefined) qs.set("limit", String(params.limit));
    if (params.offset !== undefined) qs.set("offset", String(params.offset));
    const res = await this.requestJson<SearchResponse>("GET", `/api/v1/search?${qs.toString()}`);
    return normalizeSearchResponse(res);
  }

  async getItem(itemId: string): Promise<ItemResponse> {
    const res = await this.requestJson<ItemResponse>(
      "GET",
      `/api/v1/items/${encodeURIComponent(itemId)}`,
    );
    return normalizeItemResponse(res);
  }

  // --- Categories ---
  async listCategories(): Promise<CategoryResponse[]> {
    const rows = await this.requestJson<CategoryResponse[]>("GET", "/api/v1/categories");
    return rows.map(normalizeCategoryResponse);
  }

  async createCategory(body: CategoryCreateRequest): Promise<CategoryResponse> {
    const res = await this.requestJson<CategoryResponse>("POST", "/api/v1/categories", { body });
    return normalizeCategoryResponse(res);
  }

  async updateCategory(categoryId: string, body: CategoryUpdateRequest): Promise<CategoryResponse> {
    const res = await this.requestJson<CategoryResponse>(
      "PATCH",
      `/api/v1/categories/${encodeURIComponent(categoryId)}`,
      { body },
    );
    return normalizeCategoryResponse(res);
  }

  async deleteCategory(categoryId: string): Promise<void> {
    await this.requestJson<unknown>(
      "DELETE",
      `/api/v1/categories/${encodeURIComponent(categoryId)}`,
    );
  }

  // --- Feeds & OPML ---
  async listFeeds(): Promise<FeedResponse[]> {
    const rows = await this.requestJson<FeedResponse[]>("GET", "/api/v1/feeds");
    return rows.map(normalizeFeedResponse);
  }

  async getFeed(feedId: string): Promise<FeedResponse> {
    const res = await this.requestJson<FeedResponse>(
      "GET",
      `/api/v1/feeds/${encodeURIComponent(feedId)}`,
    );
    return normalizeFeedResponse(res);
  }

  async getFeedsHealth(): Promise<FeedResponse[]> {
    const rows = await this.requestJson<FeedResponse[]>("GET", "/api/v1/feeds/health");
    return rows.map(normalizeFeedResponse);
  }

  async createFeed(body: FeedCreateRequest): Promise<FeedResponse> {
    const res = await this.requestJson<FeedResponse>("POST", "/api/v1/feeds", { body });
    return normalizeFeedResponse(res);
  }

  async updateFeed(feedId: string, body: FeedUpdateRequest): Promise<FeedResponse> {
    const res = await this.requestJson<FeedResponse>(
      "PATCH",
      `/api/v1/feeds/${encodeURIComponent(feedId)}`,
      { body },
    );
    return normalizeFeedResponse(res);
  }

  async triggerFeedPoll(feedId: string): Promise<FeedPollTriggerResponse> {
    const res = await this.requestJson<FeedPollTriggerResponse>(
      "POST",
      `/api/v1/feeds/${encodeURIComponent(feedId)}/poll`,
    );
    return { ...res, feed_id: res.feed_id.toLowerCase() };
  }

  async importOpml(body: OpmlImportRequest): Promise<OpmlImportResponse> {
    return this.requestJson<OpmlImportResponse>("POST", "/api/v1/feeds/import-opml", { body });
  }

  async exportOpml(): Promise<string> {
    return this.requestJson<string>("GET", "/api/v1/feeds/export-opml", {
      accept: "application/xml",
    });
  }

  // --- Settings ---
  async getSettings(): Promise<SyncAppSettingsDTO> {
    const res = await this.requestJson<SyncAppSettingsDTO>("GET", "/api/v1/settings");
    return normalizeSyncAppSettings(res);
  }

  async updateSettings(body: AppSettingsUpdateRequest): Promise<SyncAppSettingsDTO> {
    const res = await this.requestJson<SyncAppSettingsDTO>("PATCH", "/api/v1/settings", { body });
    return normalizeSyncAppSettings(res);
  }

  // --- System Health & Metrics ---
  async getSystemHealth(): Promise<SystemHealthResponse> {
    const res = await this.requestJson<SystemHealthResponse>("GET", "/api/v1/system/health");
    return normalizeSystemHealth(res);
  }

  async getSystemMetrics(window: MetricsWindow): Promise<SystemMetricsResponse> {
    const res = await this.requestJson<SystemMetricsResponse>(
      "GET",
      `/api/v1/system/metrics?window=${encodeURIComponent(window)}`,
    );
    return normalizeSystemMetrics(res);
  }

  async listDeadLetterJobs(limit = 25, offset = 0): Promise<DeadLetterListResponse> {
    const res = await this.requestJson<DeadLetterListResponse>(
      "GET",
      `/api/v1/system/jobs/dead-letter?limit=${limit}&offset=${offset}`,
    );
    return normalizeDeadLetterList(res);
  }

  async retryDeadLetterJob(jobId: number): Promise<DeadLetterJobResponse> {
    const res = await this.requestJson<DeadLetterJobResponse>(
      "POST",
      `/api/v1/system/jobs/${jobId}/retry`,
    );
    return normalizeDeadLetterJob(res);
  }

  async dismissDeadLetterJob(jobId: number): Promise<void> {
    await this.requestJson<unknown>("POST", `/api/v1/system/jobs/${jobId}/dismiss`);
  }
}
