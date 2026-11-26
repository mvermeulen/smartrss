/**
 * Cross-tab sync lock coordinator (1c §6.2, U-05, U-05-F1, U-09-F1).
 *
 * - Secure contexts (`navigator.locks` available): uses Web Locks API.
 * - Insecure HTTP LAN fallback (`navigator.locks` absent): uses a fenced atomic
 *   IndexedDB lease lock stored in `client_meta['sync_lock']` with monotonic
 *   `fencing_token`, heartbeat timer, pre-fetch renewal (§10 note 2), and mandatory
 *   in-transaction `assertAndRenew()` fencing checks (`SyncLockLostError`).
 */

import type { SmartRSSDB, SyncLockMeta } from "../db/schema";
import { uuidv4 } from "../security/uuid";

export const SYNC_LOCK_NAME = "smartrss-sync-cycle";
export const LEASE_TTL_MS = 20_000; // > 15s push timeout (review §10 note 2)
export const LEASE_HEARTBEAT_MS = 5_000;

export class SyncLockLostError extends Error {
  constructor(message = "Sync lease lock lost to another tab") {
    super(message);
    this.name = "SyncLockLostError";
  }
}

export interface LockHandle {
  readonly mode: "web_locks" | "idb_lease";
  readonly ownerTabId: string;
  readonly fencingToken: number | null;
  /**
   * Call inside any Dexie `rw` transaction that includes `db.client_meta`.
   * Throws `SyncLockLostError` (aborting the Dexie tx atomically) if lease was stolen;
   * otherwise refreshes `expires_at` inline. No-op under Web Locks.
   */
  assertAndRenewInTx(): Promise<void>;
  /**
   * Call right before any slow network `fetch()` to avoid needless lease expiry (§10 note 2).
   */
  renewBeforeIo(): Promise<void>;
}

export interface SyncLockOptions {
  db: SmartRSSDB;
  tabId?: string;
  now?: () => number;
  leaseTtlMs?: number;
  heartbeatMs?: number;
  forceIdbFallback?: boolean;
}

function hasWebLocks(forceIdbFallback?: boolean): boolean {
  if (forceIdbFallback) {
    return false;
  }
  return (
    typeof globalThis.navigator !== "undefined" &&
    typeof globalThis.navigator.locks?.request === "function"
  );
}

/**
 * Acquire the cross-tab sync lock non-blockingly (`ifAvailable: true`) or blockingly
 * (under Web Locks for the deduplicated `waiterPending` path in U-09-F1).
 * Returns `{ acquired: false }` if `ifAvailable` is true and another tab holds the lock.
 */
export async function withSyncLock<T>(
  options: SyncLockOptions & { ifAvailable?: boolean },
  fn: (handle: LockHandle) => Promise<T>,
): Promise<{ acquired: true; value: T } | { acquired: false }> {
  const {
    db,
    tabId = uuidv4(),
    now = () => Date.now(),
    leaseTtlMs = LEASE_TTL_MS,
    heartbeatMs = LEASE_HEARTBEAT_MS,
    ifAvailable = true,
    forceIdbFallback = false,
  } = options;

  if (hasWebLocks(forceIdbFallback)) {
    let result: { acquired: true; value: T } | { acquired: false } = { acquired: false };
    await globalThis.navigator.locks.request(
      SYNC_LOCK_NAME,
      ifAvailable ? { ifAvailable: true } : {},
      async (lock) => {
        if (!lock) {
          result = { acquired: false };
          return;
        }
        const handle: LockHandle = {
          mode: "web_locks",
          ownerTabId: tabId,
          fencingToken: null,
          async assertAndRenewInTx() {
            // Web Locks guarantees exclusive ownership for the callback lifetime
          },
          async renewBeforeIo() {
            // No lease renewal needed under Web Locks
          },
        };
        const value = await fn(handle);
        result = { acquired: true, value };
      },
    );
    return result;
  }

  // Fenced IndexedDB lease lock fallback (U-05, U-05-F1)
  let acquiredToken: number | null = null;
  await db.transaction("rw", db.client_meta, async () => {
    const existingRow = await db.client_meta.get("sync_lock");
    const existing = existingRow?.value as SyncLockMeta | null | undefined;
    const currentNow = now();
    if (existing && existing.expires_at > currentNow && existing.owner_tab_id !== tabId) {
      acquiredToken = null;
      return;
    }
    const nextToken = (existing?.fencing_token ?? 0) + 1;
    const nextLease: SyncLockMeta = {
      owner_tab_id: tabId,
      fencing_token: nextToken,
      acquired_at: currentNow,
      expires_at: currentNow + leaseTtlMs,
    };
    await db.client_meta.put({ key: "sync_lock", value: nextLease });
    acquiredToken = nextToken;
  });

  if (acquiredToken === null) {
    return { acquired: false };
  }

  const token = acquiredToken;

  const assertAndRenewInTx = async (): Promise<void> => {
    const row = await db.client_meta.get("sync_lock");
    const current = row?.value as SyncLockMeta | null | undefined;
    if (!current || current.owner_tab_id !== tabId || current.fencing_token !== token) {
      throw new SyncLockLostError();
    }
    const t = now();
    await db.client_meta.put({
      key: "sync_lock",
      value: {
        ...current,
        expires_at: t + leaseTtlMs,
      },
    });
  };

  const renewBeforeIo = async (): Promise<void> => {
    await db.transaction("rw", db.client_meta, async () => {
      await assertAndRenewInTx();
    });
  };

  const heartbeatTimer = globalThis.setInterval(() => {
    void renewBeforeIo().catch(() => {
      // If lease was lost, next fenced write or renewBeforeIo will abort cleanly
    });
  }, heartbeatMs);

  const releaseLease = async (): Promise<void> => {
    try {
      await db.transaction("rw", db.client_meta, async () => {
        const row = await db.client_meta.get("sync_lock");
        const current = row?.value as SyncLockMeta | null | undefined;
        if (current && current.owner_tab_id === tabId && current.fencing_token === token) {
          await db.client_meta.put({
            key: "sync_lock",
            value: {
              ...current,
              expires_at: 0,
            },
          });
        }
      });
    } catch {
      // Ignore release errors during tab unload / DB close
    }
  };

  const handle: LockHandle = {
    mode: "idb_lease",
    ownerTabId: tabId,
    fencingToken: token,
    assertAndRenewInTx,
    renewBeforeIo,
  };

  try {
    const value = await fn(handle);
    return { acquired: true, value };
  } finally {
    globalThis.clearInterval(heartbeatTimer);
    await releaseLease();
  }
}
