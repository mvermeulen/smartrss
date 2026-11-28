/**
 * Client Sync Engine coordinator (1c §6.1–§6.7).
 *
 * Implements:
 * - Connection state machine (`unpaired`, `origin_confirm`, `bootstrapping`, `online`,
 *   `syncing`, `detached`, `misconfigured`, `sync_error`, `auth_revoked`)
 * - Multi-tab serialization via `withSyncLock` + `BroadcastChannel('smartrss-sync')`
 *   with pre-release dirty check + deduplicated blocking waiter (`U-09-F1`)
 * - Two-phase streaming resumable bootstrap (`U-06`)
 * - Push -> Pull -> Evict loop with server-base + overlay (`U-01`)
 * - Origin-change guard + single-step SW activation + CSP violation listener (`U-08-F1`, §10 note 3)
 * - Restore recovery on `epoch_mismatch`, `cursor_ahead`, `cursor_pruned` (`U-10`)
 */

import {
  ApiHttpError,
  ApiNetworkError,
  fetchClientRuntimeConfig,
  SmartRSSApiClient,
} from "../api/client";
import {
  clearFullResyncRequest,
  DEFAULT_SYNC_META,
  getAuthMeta,
  getFullResyncRequest,
  getSyncMeta,
  setAppSettingsMeta,
  setAuthMeta,
  setFullResyncRequest,
  setInterestProfileMeta,
  setSyncMeta,
} from "../db/clientMeta";
import {
  clearAllLocalData,
  type BootstrapMetaRecord,
  type LocalItemBaseRecord,
  type SmartRSSDB,
} from "../db/schema";
import { uuidv4 } from "../security/uuid";
import { activateWaitingServiceWorker } from "../sw/register";
import type { ClientRuntimeConfig } from "../types/api";
import type {
  SyncAppSettingsDTO,
  SyncCategoryDTO,
  SyncClusterDTO,
  SyncFeedDTO,
  SyncInterestProfileDTO,
  SyncItemDTO,
  SyncPullResponse,
} from "../types/sync";
import type { ConnectionState } from "../types/ui";
import { SyncLockLostError, withSyncLock, type LockHandle } from "./lock";
import {
  flushOutbox,
  mintFreshMutationIdsOnRepair,
  prepareOutboxForRestore,
  pruneReplayJournal,
  type PushObserverCallbacks,
} from "./outbox";
import { probeServerReachability } from "./probe";
import {
  applyOutboxToFeed,
  applyOutboxToItem,
  collectProtectedItemIds,
  computeClockDriftMs,
  evictStaleLocalItems,
  isItemOutOfWindow,
  toLocalItemBase,
} from "./reconciliation";

const BROADCAST_CHANNEL_NAME = "smartrss-sync";
const MAX_BOOTSTRAP_RESTARTS = 3;
const MAX_DRAIN_LOOPS = 5;
const BOOTSTRAP_COMMIT_CHUNK_SIZE = 1_000;

export interface SyncEngineSnapshot {
  connectionState: ConnectionState;
  statusDetail: string | null;
  runtimeConfig: ClientRuntimeConfig;
  configLoaded: boolean;
  pendingOriginChange: { oldOrigin: string; newOrigin: string } | null;
  bootstrapProgress: { pagesFetched: number; itemsStaged: number } | null;
  clockDriftMs: number;
  cspReloadRequired: boolean;
}

type BroadcastMsg =
  | { type: "outbox_enqueued"; senderId: string }
  | { type: "full_resync_requested"; senderId: string }
  | { type: "sync_complete"; senderId: string }
  | { type: "auth_changed"; senderId: string };

export interface SyncEngineOptions {
  db: SmartRSSDB;
  fetchImpl?: typeof fetch;
  onItemsMutated?: (changedIds: string[], deletedIds: string[]) => void;
  onFullReindex?: () => Promise<void>;
  onArchiveMutationResolved?: PushObserverCallbacks["onArchiveMutationResolved"];
  onReloadRequested?: () => void;
  forceIdbLockFallback?: boolean;
  now?: () => number;
}

export class SyncEngine {
  private readonly db: SmartRSSDB;
  private readonly fetchImpl: typeof fetch;
  private readonly tabId = uuidv4();
  private readonly listeners = new Set<(snap: SyncEngineSnapshot) => void>();
  private readonly onItemsMutated?: (changedIds: string[], deletedIds: string[]) => void;
  private readonly onFullReindex?: () => Promise<void>;
  private readonly onArchiveMutationResolved?: PushObserverCallbacks["onArchiveMutationResolved"];
  private readonly onReloadRequested: () => void;
  private readonly forceIdbLockFallback: boolean;
  private readonly now: () => number;

  private channel: BroadcastChannel | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private dirty = false;
  private waiterPending = false;
  private recheckOnSyncComplete = false;
  private running = false;
  private lastCycleAt = 0;

  private snapshot: SyncEngineSnapshot = {
    connectionState: "unpaired",
    statusDetail: null,
    runtimeConfig: { mode: "localhost", api_origin: "" },
    configLoaded: false,
    pendingOriginChange: null,
    bootstrapProgress: null,
    clockDriftMs: 0,
    cspReloadRequired: false,
  };

  private readonly boundOnline = () => {
    void this.triggerSync();
  };
  private readonly boundOffline = () => {
    if (this.snapshot.connectionState === "online" || this.snapshot.connectionState === "syncing") {
      this.updateSnapshot({ connectionState: "detached", statusDetail: "Browser offline" });
    }
  };
  private readonly boundVisibility = () => {
    if (
      typeof document !== "undefined" &&
      !document.hidden &&
      this.now() - this.lastCycleAt > 15_000
    ) {
      void this.triggerSync();
    }
  };
  private readonly boundCspViolation = (evt: Event) => {
    const cspEvent = evt as SecurityPolicyViolationEvent;
    const targetOrigin =
      this.snapshot.pendingOriginChange?.newOrigin || this.snapshot.runtimeConfig.api_origin;
    if (
      targetOrigin &&
      cspEvent.effectiveDirective.startsWith("connect-src") &&
      cspEvent.blockedURI.startsWith(targetOrigin)
    ) {
      this.updateSnapshot({
        cspReloadRequired: true,
        statusDetail: "Reload required to apply updated server origin CSP.",
      });
    }
  };

  constructor(options: SyncEngineOptions) {
    this.db = options.db;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.onItemsMutated = options.onItemsMutated;
    this.onFullReindex = options.onFullReindex;
    this.onArchiveMutationResolved = options.onArchiveMutationResolved;
    this.onReloadRequested = options.onReloadRequested ?? (() => globalThis.location?.reload?.());
    this.forceIdbLockFallback = options.forceIdbLockFallback ?? false;
    this.now = options.now ?? (() => Date.now());
  }

  getSnapshot(): SyncEngineSnapshot {
    return this.snapshot;
  }

  subscribe(listener: (snap: SyncEngineSnapshot) => void): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private updateSnapshot(patch: Partial<SyncEngineSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) {
      listener(this.snapshot);
    }
  }

  private postBroadcast(msg: BroadcastMsg): void {
    try {
      this.channel?.postMessage(msg);
    } catch {
      // BroadcastChannel may be closed during teardown
    }
  }

  async start(): Promise<void> {
    if (typeof BroadcastChannel !== "undefined") {
      this.channel = new BroadcastChannel(BROADCAST_CHANNEL_NAME);
      this.channel.onmessage = (event: MessageEvent<BroadcastMsg>) => {
        const msg = event.data;
        if (!msg || msg.senderId === this.tabId) {
          return;
        }
        if (msg.type === "outbox_enqueued") {
          this.dirty = true;
          if (!this.running) {
            this.scheduleDebouncedSync();
          }
        } else if (msg.type === "full_resync_requested") {
          this.dirty = true;
          if (!this.running) {
            void this.triggerSync();
          }
        } else if (msg.type === "sync_complete") {
          if (this.recheckOnSyncComplete) {
            this.recheckOnSyncComplete = false;
            void Promise.all([this.db.outbox.count(), getFullResyncRequest(this.db)]).then(
              ([cnt, resyncReq]) => {
                if (cnt > 0 || resyncReq !== null) {
                  void this.triggerSync();
                }
              },
            );
          }
        } else if (msg.type === "auth_changed") {
          void this.refreshAuthState();
        }
      };
    }

    if (typeof window !== "undefined") {
      window.addEventListener("online", this.boundOnline);
      window.addEventListener("offline", this.boundOffline);
    }
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.boundVisibility);
      document.addEventListener("securitypolicyviolation", this.boundCspViolation);
    }

    // Install the 60 s poll timer BEFORE the first startup sync so an unexpected startup
    // failure can never leave the tab without periodic retry (V-01, checklist B6).
    this.pollTimer = globalThis.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) {
        return;
      }
      if (
        this.snapshot.connectionState === "online" ||
        this.snapshot.connectionState === "detached" ||
        this.snapshot.connectionState === "sync_error"
      ) {
        void this.triggerSync();
      }
    }, 60_000);

    try {
      await this.initializeRuntimeAndAuth();
    } catch (err) {
      this.updateSnapshot({
        connectionState: "sync_error",
        statusDetail: err instanceof Error ? err.message : String(err),
      });
    }
  }

  stop(): void {
    if (this.pollTimer !== null) {
      globalThis.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    if (this.debounceTimer !== null) {
      globalThis.clearTimeout(this.debounceTimer);
      this.debounceTimer = null;
    }
    if (this.channel) {
      this.channel.close();
      this.channel = null;
    }
    if (typeof window !== "undefined") {
      window.removeEventListener("online", this.boundOnline);
      window.removeEventListener("offline", this.boundOffline);
    }
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.boundVisibility);
      document.removeEventListener("securitypolicyviolation", this.boundCspViolation);
    }
  }

  async initializeRuntimeAndAuth(): Promise<void> {
    const fetchedConfig = await fetchClientRuntimeConfig(this.fetchImpl);
    const auth = await getAuthMeta(this.db);
    const syncMeta = await getSyncMeta(this.db);

    const runtimeConfig: ClientRuntimeConfig = fetchedConfig ?? {
      mode:
        typeof window !== "undefined" && window.location.hostname === "localhost"
          ? "localhost"
          : "lan_fallback",
      api_origin: auth?.api_origin ?? "",
    };

    this.updateSnapshot({
      runtimeConfig,
      configLoaded: true,
      clockDriftMs: syncMeta.clock_drift_ms,
    });

    if (!auth) {
      this.updateSnapshot({
        connectionState: "unpaired",
        pendingOriginChange: null,
        statusDetail: null,
      });
      return;
    }

    // Origin-change guard (U-08): if runner config origin differs from paired auth origin, pause!
    if (
      fetchedConfig &&
      fetchedConfig.mode === "localhost" &&
      fetchedConfig.api_origin &&
      auth.api_origin &&
      fetchedConfig.api_origin !== auth.api_origin
    ) {
      this.updateSnapshot({
        connectionState: "origin_confirm",
        pendingOriginChange: {
          oldOrigin: auth.api_origin,
          newOrigin: fetchedConfig.api_origin,
        },
        statusDetail: `Server address changed from ${auth.api_origin} to ${fetchedConfig.api_origin}.`,
      });
      return;
    }

    const activeBootstrap = await this.db.bootstrap_meta.get("active");
    if (syncMeta.sync_cursor === null || activeBootstrap) {
      this.updateSnapshot({ connectionState: "bootstrapping" });
    } else {
      this.updateSnapshot({ connectionState: "detached" });
    }

    await this.triggerSync();
  }

  async refreshAuthState(): Promise<void> {
    const auth = await getAuthMeta(this.db);
    if (!auth) {
      this.updateSnapshot({
        connectionState: "unpaired",
        pendingOriginChange: null,
        statusDetail: null,
      });
      return;
    }
    await this.triggerSync();
  }

  /**
   * Single-step origin confirmation + SW activation (`U-08-F1`).
   */
  async confirmOriginChange(): Promise<void> {
    const pending = this.snapshot.pendingOriginChange;
    const auth = await getAuthMeta(this.db);
    if (!pending || !auth) {
      return;
    }

    await setAuthMeta(this.db, {
      ...auth,
      api_origin: pending.newOrigin,
    });
    this.postBroadcast({ type: "auth_changed", senderId: this.tabId });
    this.updateSnapshot({
      pendingOriginChange: null,
      connectionState: "detached",
      statusDetail: "Applying updated server origin…",
    });

    // Activate updated SW immediately so page reloads under the new connect-src CSP (U-08-F1)
    const switched = await activateWaitingServiceWorker(5_000);
    if (switched) {
      this.onReloadRequested();
      return;
    }

    await this.triggerSync();
  }

  /**
   * Called after initial pairing or re-pairing from `auth_revoked` (`U-11`).
   */
  async completePairing(
    auth: {
      device_id: string;
      device_name: string;
      platform: "ubuntu" | "android" | "web";
      bearer_token: string;
    },
    isRepair: boolean,
  ): Promise<void> {
    const apiOrigin = this.snapshot.runtimeConfig.api_origin;
    await setAuthMeta(this.db, {
      ...auth,
      api_origin: apiOrigin,
      paired_at: new Date(this.now()).toISOString(),
    });
    if (isRepair) {
      await mintFreshMutationIdsOnRepair(this.db);
    }
    this.postBroadcast({ type: "auth_changed", senderId: this.tabId });
    this.updateSnapshot({
      connectionState: isRepair ? "syncing" : "bootstrapping",
      pendingOriginChange: null,
      statusDetail: null,
    });
    await this.triggerSync();
  }

  /**
   * Sign Out & Clear Local Cache (`U-21`): best-effort server-side self-revoke when online.
   */
  async signOutAndClear(): Promise<void> {
    const auth = await getAuthMeta(this.db);
    if (auth) {
      const api = new SmartRSSApiClient(auth.api_origin, auth.bearer_token, this.fetchImpl);
      try {
        await api.revokeDevice(auth.device_id, 3_000);
      } catch {
        // Best-effort: still clear local cache when offline or already revoked
      }
    }
    await clearAllLocalData(this.db);
    await this.onFullReindex?.();
    this.postBroadcast({ type: "auth_changed", senderId: this.tabId });
    this.updateSnapshot({
      connectionState: "unpaired",
      pendingOriginChange: null,
      bootstrapProgress: null,
      statusDetail: null,
      clockDriftMs: 0,
      cspReloadRequired: false,
    });
  }

  notifyOutboxEnqueued(): void {
    this.dirty = true;
    this.postBroadcast({ type: "outbox_enqueued", senderId: this.tabId });
    this.scheduleDebouncedSync();
  }

  scheduleDebouncedSync(delayMs = 250): void {
    if (
      this.snapshot.connectionState === "unpaired" ||
      this.snapshot.connectionState === "origin_confirm" ||
      this.snapshot.connectionState === "auth_revoked"
    ) {
      return;
    }
    if (this.debounceTimer !== null) {
      globalThis.clearTimeout(this.debounceTimer);
    }
    this.debounceTimer = globalThis.setTimeout(() => {
      this.debounceTimer = null;
      void this.triggerSync();
    }, delayMs);
  }

  async forceFullResync(): Promise<void> {
    if (
      this.snapshot.connectionState === "origin_confirm" ||
      this.snapshot.connectionState === "auth_revoked"
    ) {
      return;
    }
    const auth = await getAuthMeta(this.db);
    if (!auth) {
      return;
    }
    // Persist request in its own client_meta row so an in-flight pull's setSyncMeta()
    // can never clobber it, and execute the reset strictly under withSyncLock (1d-ops §6).
    await setFullResyncRequest(this.db, {
      request_id: uuidv4(),
      requested_at: this.now(),
    });
    if (this.running) {
      this.dirty = true;
    }
    this.postBroadcast({ type: "full_resync_requested", senderId: this.tabId });
    await this.triggerSync();
  }

  /**
   * Public sync entrypoint with non-blocking try-lock + deduplicated blocking waiter (`U-09-F1`).
   */
  async triggerSync(): Promise<void> {
    if (
      this.snapshot.connectionState === "origin_confirm" ||
      this.snapshot.connectionState === "auth_revoked"
    ) {
      return;
    }
    const auth = await getAuthMeta(this.db);
    if (!auth) {
      this.updateSnapshot({ connectionState: "unpaired" });
      return;
    }

    // In-tab re-entrancy guard (V-03, checklist B5): mark dirty so the active drain loop
    // picks up new work without starting an overlapping cycle.
    if (this.running) {
      this.dirty = true;
      return;
    }

    const attempt = await this.tryRunLockedCycle(true);
    if (!attempt.acquired) {
      // Gap-closing deduplicated waiter (U-09-F1)
      this.postBroadcast({ type: "outbox_enqueued", senderId: this.tabId });
      if (!this.waiterPending) {
        this.waiterPending = true;
        const usesWebLocks =
          !this.forceIdbLockFallback &&
          typeof globalThis.navigator !== "undefined" &&
          typeof globalThis.navigator.locks?.request === "function";

        if (usesWebLocks) {
          void this.tryRunLockedCycle(false).finally(() => {
            this.waiterPending = false;
          });
        } else {
          this.recheckOnSyncComplete = true;
          this.waiterPending = false;
        }
      }
    }
  }

  private async tryRunLockedCycle(ifAvailable: boolean): Promise<{ acquired: boolean }> {
    let lastCycleProgressed = false;
    const res = await withSyncLock(
      {
        db: this.db,
        tabId: this.tabId,
        now: this.now,
        ifAvailable,
        forceIdbFallback: this.forceIdbLockFallback,
      },
      async (lock) => {
        this.running = true;
        try {
          let loops = 0;
          while (loops < MAX_DRAIN_LOOPS) {
            this.dirty = false;
            const progressed = await this.executeSingleCycle(lock);
            lastCycleProgressed = progressed;
            loops += 1;
            if (!progressed) {
              break;
            }
            const remaining = await this.db.outbox.count();
            const pendingResync = (await getFullResyncRequest(this.db)) !== null;
            if (!this.dirty && remaining === 0 && !pendingResync) {
              break;
            }
          }
        } catch (err) {
          lastCycleProgressed = false;
          if (err instanceof SyncLockLostError) {
            return;
          }
          // Catch every unexpected error (parse error, Dexie quota/state error, etc.) so
          // the engine never wedges in `syncing` (V-01, checklist B6).
          this.updateSnapshot({
            connectionState: "sync_error",
            statusDetail: err instanceof Error ? err.message : String(err),
          });
        } finally {
          this.running = false;
          this.lastCycleAt = this.now();
        }
      },
    );

    if (res.acquired) {
      // Broadcast AFTER releasing the lock so waiting tabs can acquire immediately (U-09-F1)
      this.postBroadcast({ type: "sync_complete", senderId: this.tabId });
      // Post-release holder follow-up gated on lastCycleProgressed so offline/misconfigured
      // tabs never spin-reschedule (O-02, O-02-F1).
      if (lastCycleProgressed && (await getFullResyncRequest(this.db)) !== null) {
        this.scheduleDebouncedSync(0);
      }
    }
    return { acquired: res.acquired };
  }

  private async executeSingleCycle(lock: LockHandle): Promise<boolean> {
    const auth = await getAuthMeta(this.db);
    if (!auth) {
      this.updateSnapshot({ connectionState: "unpaired" });
      return false;
    }

    const api = new SmartRSSApiClient(auth.api_origin, auth.bearer_token, this.fetchImpl);

    // Probe when detached / misconfigured / first startup
    if (
      this.snapshot.connectionState === "detached" ||
      this.snapshot.connectionState === "misconfigured"
    ) {
      const probe = await probeServerReachability(auth.api_origin, this.fetchImpl);
      if (probe.status === "detached") {
        this.updateSnapshot({ connectionState: "detached", statusDetail: null });
        return false;
      }
      if (probe.status === "misconfigured") {
        this.updateSnapshot({
          connectionState: "misconfigured",
          statusDetail: probe.message,
        });
        return false;
      }
    }

    // Consume any pending forceFullResync() request strictly inside a fenced lock tx (1d-ops §6).
    // Dexie serializes rw transactions on client_meta: any request written before this tx starts
    // is satisfied by this reset, and any request written after it commits persists (G-05).
    const resyncReq = await getFullResyncRequest(this.db);
    if (resyncReq !== null) {
      await this.db.transaction(
        "rw",
        [this.db.client_meta, this.db.bootstrap_meta, this.db.bootstrap_items],
        async () => {
          await lock.assertAndRenewInTx();
          await this.db.bootstrap_meta.clear();
          await this.db.bootstrap_items.clear();
          const currentMeta = await getSyncMeta(this.db);
          await setSyncMeta(this.db, { ...currentMeta, sync_cursor: null });
          await clearFullResyncRequest(this.db);
        },
      );
    }

    const syncMeta = await getSyncMeta(this.db);
    const activeBootstrap = await this.db.bootstrap_meta.get("active");

    if (syncMeta.sync_cursor === null || syncMeta.instance_epoch === null || activeBootstrap) {
      this.updateSnapshot({ connectionState: "bootstrapping", statusDetail: null });
      const bootOk = await this.runStreamingBootstrap(api, lock);
      if (!bootOk) {
        return false;
      }
    }

    this.updateSnapshot({ connectionState: "syncing", statusDetail: null });

    // 1. Flush outbox BEFORE pull (clock drift persisted inside applyPushSuccessBatch's fenced tx, V-04)
    const pushOutcome = await flushOutbox(this.db, api, lock, {
      onServerNow: (_serverNow, driftMs) => {
        this.updateSnapshot({ clockDriftMs: driftMs });
      },
      onArchiveMutationResolved: this.onArchiveMutationResolved,
      onItemsChanged: (ids) => this.onItemsMutated?.(ids, []),
      now: this.now,
    });

    if (pushOutcome.status === "auth_revoked") {
      this.updateSnapshot({
        connectionState: "auth_revoked",
        statusDetail: "This device token was revoked. Re-pair to resume syncing.",
      });
      return false;
    }
    if (pushOutcome.status === "detached") {
      await this.classifyNetworkFailure(auth.api_origin);
      return false;
    }
    if (pushOutcome.status === "sync_error") {
      this.updateSnapshot({
        connectionState: "sync_error",
        statusDetail: pushOutcome.message,
      });
      return false;
    }

    // 2. Pull deltas until has_more === false
    const pullRes = await this.runPullLoop(api, lock);
    if (pullRes === "auth_revoked") {
      this.updateSnapshot({
        connectionState: "auth_revoked",
        statusDetail: "This device token was revoked. Re-pair to resume syncing.",
      });
      return false;
    }
    if (pullRes === "detached") {
      await this.classifyNetworkFailure(auth.api_origin);
      return false;
    }
    if (pullRes === "sync_error") {
      return false;
    }

    // 3. Prune 48h replay journal & evict stale local items (also runs after `resynced`, V-10)
    await pruneReplayJournal(this.db, this.now());
    const evictedIds = await evictStaleLocalItems(this.db, this.now(), () =>
      lock.assertAndRenewInTx(),
    );
    if (evictedIds.length > 0) {
      this.onItemsMutated?.([], evictedIds);
    }

    this.updateSnapshot({
      connectionState: "online",
      statusDetail: null,
      bootstrapProgress: null,
    });
    return true;
  }

  private async classifyNetworkFailure(apiOrigin: string): Promise<void> {
    const probe = await probeServerReachability(apiOrigin, this.fetchImpl);
    if (probe.status === "misconfigured") {
      this.updateSnapshot({
        connectionState: "misconfigured",
        statusDetail: probe.message,
      });
    } else {
      this.updateSnapshot({
        connectionState: "detached",
        statusDetail: null,
      });
    }
  }

  /**
   * Two-phase streaming resumable bootstrap (`1c` §6.3, U-06).
   */
  private async runStreamingBootstrap(api: SmartRSSApiClient, lock: LockHandle): Promise<boolean> {
    let restarts = 0;

    while (restarts <= MAX_BOOTSTRAP_RESTARTS) {
      let checkpoint = await this.db.bootstrap_meta.get("active");

      try {
        if (!checkpoint) {
          await lock.renewBeforeIo();
          const page1 = await api.syncBootstrap(null, 500);
          if (page1.watermark === null) {
            this.updateSnapshot({
              connectionState: "sync_error",
              statusDetail: "Bootstrap Page 1 returned null watermark.",
            });
            return false;
          }
          const driftMs = computeClockDriftMs(page1.server_now, this.now());
          this.updateSnapshot({ clockDriftMs: driftMs });

          const prevSync = await getSyncMeta(this.db);
          const nextCheckpoint: BootstrapMetaRecord = {
            key: "active",
            phase: page1.has_more ? "fetching" : "committing",
            target_gen: prevSync.active_gen + 1,
            page1_epoch: page1.instance_epoch,
            page1_watermark: page1.watermark,
            server_now: page1.server_now,
            next_cursor: page1.has_more ? page1.next_cursor : null,
            categories: page1.categories ?? [],
            feeds: page1.feeds ?? [],
            clusters: page1.clusters ?? [],
            interest_profile: page1.interest_profile ?? null,
            app_settings: page1.app_settings ?? null,
            pages_fetched: 1,
            items_staged: page1.items.length,
          };

          await this.db.transaction(
            "rw",
            [this.db.client_meta, this.db.bootstrap_meta, this.db.bootstrap_items],
            async () => {
              await lock.assertAndRenewInTx();
              await this.db.bootstrap_items.clear();
              if (page1.items.length > 0) {
                await this.db.bootstrap_items.bulkPut(page1.items);
              }
              await this.db.bootstrap_meta.put(nextCheckpoint);
            },
          );
          checkpoint = nextCheckpoint;
          this.updateSnapshot({
            bootstrapProgress: {
              pagesFetched: checkpoint.pages_fetched,
              itemsStaged: checkpoint.items_staged,
            },
          });
        }

        // Phase 1: stream remaining pages 2..N into bootstrap_items
        let epochFlipped = false;
        while (checkpoint.phase === "fetching" && checkpoint.next_cursor !== null) {
          await lock.renewBeforeIo();
          const page = await api.syncBootstrap(checkpoint.next_cursor, 500);
          if (page.instance_epoch !== checkpoint.page1_epoch) {
            restarts += 1;
            epochFlipped = true;
            await this.db.transaction(
              "rw",
              [this.db.client_meta, this.db.bootstrap_meta, this.db.bootstrap_items],
              async () => {
                await lock.assertAndRenewInTx();
                await this.db.bootstrap_meta.clear();
                await this.db.bootstrap_items.clear();
              },
            );
            break;
          }

          const updatedCheckpoint: BootstrapMetaRecord = {
            ...checkpoint,
            phase: page.has_more ? "fetching" : "committing",
            next_cursor: page.has_more ? page.next_cursor : null,
            pages_fetched: checkpoint.pages_fetched + 1,
            items_staged: checkpoint.items_staged + page.items.length,
          };

          await this.db.transaction(
            "rw",
            [this.db.client_meta, this.db.bootstrap_meta, this.db.bootstrap_items],
            async () => {
              await lock.assertAndRenewInTx();
              if (page.items.length > 0) {
                await this.db.bootstrap_items.bulkPut(page.items);
              }
              await this.db.bootstrap_meta.put(updatedCheckpoint);
            },
          );
          checkpoint = updatedCheckpoint;
          this.updateSnapshot({
            bootstrapProgress: {
              pagesFetched: checkpoint.pages_fetched,
              itemsStaged: checkpoint.items_staged,
            },
          });
        }

        if (epochFlipped) {
          continue;
        }

        // Phase 2: chunked commit & atomic activation
        const meta = checkpoint;
        await this.db.transaction(
          "rw",
          [
            this.db.client_meta,
            this.db.categories,
            this.db.feeds_base,
            this.db.feeds,
            this.db.clusters,
            this.db.outbox,
          ],
          async () => {
            await lock.assertAndRenewInTx();
            const allOutbox = await this.db.outbox.toArray();
            await this.db.categories.clear();
            if (meta.categories.length > 0) {
              await this.db.categories.bulkPut(meta.categories);
            }
            await this.db.feeds_base.clear();
            await this.db.feeds.clear();
            if (meta.feeds.length > 0) {
              await this.db.feeds_base.bulkPut(meta.feeds);
              await this.db.feeds.bulkPut(meta.feeds.map((f) => applyOutboxToFeed(f, allOutbox)));
            }
            await this.db.clusters.clear();
            if (meta.clusters.length > 0) {
              await this.db.clusters.bulkPut(meta.clusters);
            }
            await setAppSettingsMeta(this.db, meta.app_settings);
            await setInterestProfileMeta(this.db, meta.interest_profile);
          },
        );

        // Stream bootstrap_items -> items_base + items in 1,000-row chunks
        while (true) {
          const chunk = await this.db.bootstrap_items.limit(BOOTSTRAP_COMMIT_CHUNK_SIZE).toArray();
          if (chunk.length === 0) {
            break;
          }
          const chunkIds = chunk.map((i) => i.id);
          await this.db.transaction(
            "rw",
            [
              this.db.client_meta,
              this.db.items_base,
              this.db.items,
              this.db.bootstrap_items,
              this.db.outbox,
            ],
            async () => {
              await lock.assertAndRenewInTx();
              const allOutbox = await this.db.outbox.toArray();
              const baseRows: LocalItemBaseRecord[] = chunk.map((item) =>
                toLocalItemBase(item, meta.target_gen),
              );
              const displayRows = baseRows.map((b) => applyOutboxToItem(b, allOutbox));
              await this.db.items_base.bulkPut(baseRows);
              await this.db.items.bulkPut(displayRows);
              await this.db.bootstrap_items.bulkDelete(chunkIds);
            },
          );
        }

        // Final short atomic activation: delete stale generation & commit sync_cursor = page1_watermark
        const driftMs = computeClockDriftMs(meta.server_now, this.now());
        await this.db.transaction(
          "rw",
          [this.db.client_meta, this.db.items_base, this.db.items, this.db.bootstrap_meta],
          async () => {
            await lock.assertAndRenewInTx();
            await this.db.items_base.where("bootstrap_gen").below(meta.target_gen).delete();
            await this.db.items.where("bootstrap_gen").below(meta.target_gen).delete();
            await setSyncMeta(this.db, {
              ...DEFAULT_SYNC_META,
              instance_epoch: meta.page1_epoch,
              sync_cursor: meta.page1_watermark,
              active_gen: meta.target_gen,
              last_sync_at: new Date(this.now()).toISOString(),
              last_server_now: meta.server_now,
              clock_drift_ms: driftMs,
            });
            await this.db.bootstrap_meta.delete("active");
          },
        );

        await this.onFullReindex?.();
        return true;
      } catch (err) {
        if (err instanceof SyncLockLostError) {
          throw err;
        }
        if (err instanceof ApiHttpError && err.status === 401) {
          this.updateSnapshot({
            connectionState: "auth_revoked",
            statusDetail: "This device token was revoked. Re-pair to resume syncing.",
          });
          return false;
        }
        if (err instanceof ApiNetworkError) {
          await this.classifyNetworkFailure(api.origin);
          return false;
        }
        this.updateSnapshot({
          connectionState: "sync_error",
          statusDetail: err instanceof Error ? err.message : String(err),
        });
        return false;
      }
    }

    this.updateSnapshot({
      connectionState: "sync_error",
      statusDetail: "Bootstrap aborted after repeated server epoch changes.",
    });
    return false;
  }

  private async runPullLoop(
    api: SmartRSSApiClient,
    lock: LockHandle,
  ): Promise<"ok" | "resynced" | "auth_revoked" | "detached" | "sync_error"> {
    while (true) {
      const syncMeta = await getSyncMeta(this.db);
      if (syncMeta.sync_cursor === null || syncMeta.instance_epoch === null) {
        return "sync_error";
      }

      await lock.renewBeforeIo();
      let page: SyncPullResponse;
      try {
        page = await api.syncPull(syncMeta.sync_cursor, syncMeta.instance_epoch, 200);
      } catch (err) {
        if (err instanceof ApiHttpError && err.status === 401) {
          return "auth_revoked";
        }
        if (err instanceof ApiNetworkError) {
          return "detached";
        }
        this.updateSnapshot({
          connectionState: "sync_error",
          statusDetail: err instanceof Error ? err.message : String(err),
        });
        return "sync_error";
      }

      const driftMs = computeClockDriftMs(page.server_now, this.now());
      this.updateSnapshot({ clockDriftMs: driftMs });

      if (page.resync_required) {
        const reason = page.resync_reason ?? "epoch_mismatch";
        if (reason === "epoch_mismatch" || reason === "cursor_ahead") {
          await prepareOutboxForRestore(this.db, lock, this.now());
          await this.db.sync_notifications.add({
            severity: "info",
            title: "Resynced after server restore",
            detail: "Recent local actions were preserved and replayed cleanly.",
            reason,
            mutation_type: null,
            entity_label: null,
            created_at: new Date(this.now()).toISOString(),
            dismissed: 0,
          });
        }
        // Clear any stale checkpoint & run bootstrap -> push -> pull
        await this.db.transaction(
          "rw",
          [this.db.client_meta, this.db.bootstrap_meta, this.db.bootstrap_items],
          async () => {
            await lock.assertAndRenewInTx();
            await this.db.bootstrap_meta.clear();
            await this.db.bootstrap_items.clear();
          },
        );
        const bootOk = await this.runStreamingBootstrap(api, lock);
        if (!bootOk) {
          return "sync_error";
        }
        const pushRes = await flushOutbox(this.db, api, lock, {
          onArchiveMutationResolved: this.onArchiveMutationResolved,
          onItemsChanged: (ids) => this.onItemsMutated?.(ids, []),
        });
        if (pushRes.status !== "ok") {
          if (pushRes.status === "auth_revoked") return "auth_revoked";
          if (pushRes.status === "detached") return "detached";
          this.updateSnapshot({ connectionState: "sync_error", statusDetail: pushRes.message });
          return "sync_error";
        }
        // Trailing pull after restore bootstrap + push
        const trailing = await this.runPullLoop(api, lock);
        return trailing === "ok" ? "resynced" : trailing;
      }

      const changedItemIds: string[] = [];
      const deletedItemIds: string[] = [];

      await this.db.transaction(
        "rw",
        [
          this.db.client_meta,
          this.db.categories,
          this.db.feeds_base,
          this.db.feeds,
          this.db.clusters,
          this.db.items_base,
          this.db.items,
          this.db.outbox,
        ],
        async () => {
          await lock.assertAndRenewInTx();
          const allOutbox = await this.db.outbox.toArray();
          const protectedIds = collectProtectedItemIds(allOutbox);
          const currentSync = await getSyncMeta(this.db);

          for (const ch of page.changes) {
            if (ch.entity_type === "category") {
              if (ch.op === "delete" || !ch.data) {
                await this.db.categories.delete(ch.entity_id);
              } else {
                await this.db.categories.put(ch.data as SyncCategoryDTO);
              }
            } else if (ch.entity_type === "feed") {
              if (ch.op === "delete" || !ch.data) {
                await this.db.feeds_base.delete(ch.entity_id);
                await this.db.feeds.delete(ch.entity_id);
              } else {
                const feedDto = ch.data as SyncFeedDTO;
                await this.db.feeds_base.put(feedDto);
                await this.db.feeds.put(applyOutboxToFeed(feedDto, allOutbox));
              }
            } else if (ch.entity_type === "cluster") {
              if (ch.op === "delete" || !ch.data) {
                await this.db.clusters.delete(ch.entity_id);
              } else {
                await this.db.clusters.put(ch.data as SyncClusterDTO);
              }
            } else if (ch.entity_type === "item") {
              if (ch.op === "delete" || !ch.data) {
                await this.db.items_base.delete(ch.entity_id);
                await this.db.items.delete(ch.entity_id);
                deletedItemIds.push(ch.entity_id);
              } else {
                const itemDto = ch.data as SyncItemDTO;
                const existing = await this.db.items_base.get(itemDto.id);
                if (
                  !existing &&
                  !protectedIds.has(itemDto.id) &&
                  isItemOutOfWindow(itemDto, this.now())
                ) {
                  continue;
                }
                const baseRow = toLocalItemBase(itemDto, currentSync.active_gen);
                await this.db.items_base.put(baseRow);
                await this.db.items.put(applyOutboxToItem(baseRow, allOutbox));
                changedItemIds.push(itemDto.id);
              }
            } else if (ch.entity_type === "app_settings" && ch.data) {
              await setAppSettingsMeta(this.db, ch.data as SyncAppSettingsDTO);
            } else if (ch.entity_type === "interest_profile") {
              await setInterestProfileMeta(
                this.db,
                (ch.data as SyncInterestProfileDTO | null) ?? null,
              );
            }
          }

          await setSyncMeta(this.db, {
            ...currentSync,
            sync_cursor: page.next_seq,
            last_sync_at: new Date(this.now()).toISOString(),
            last_server_now: page.server_now,
            clock_drift_ms: driftMs,
          });
        },
      );

      if (changedItemIds.length > 0 || deletedItemIds.length > 0) {
        this.onItemsMutated?.(changedItemIds, deletedItemIds);
      }

      if (!page.has_more) {
        return "ok";
      }
    }
  }
}
