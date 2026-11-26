/** Typed helpers for Dexie `client_meta` singleton rows and StorageManager persistence. */

import type { SyncAppSettingsDTO, SyncInterestProfileDTO } from "../types/sync";
import type { StoragePersistenceStatus, UiPrefs } from "../types/ui";
import type { AuthMeta, SmartRSSDB, SyncCursorMeta } from "./schema";

export const DEFAULT_SYNC_META: SyncCursorMeta = {
  instance_epoch: null,
  sync_cursor: null,
  active_gen: 0,
  last_sync_at: null,
  last_server_now: null,
  clock_drift_ms: 0,
};

export const DEFAULT_UI_PREFS: UiPrefs = {
  theme: "system",
  default_sort: "relevance",
  density: "comfortable",
  auto_mark_read_on_select: false,
};

export async function getAuthMeta(db: SmartRSSDB): Promise<AuthMeta | null> {
  const row = await db.client_meta.get("auth");
  return (row?.value as AuthMeta | null | undefined) ?? null;
}

export async function setAuthMeta(db: SmartRSSDB, auth: AuthMeta): Promise<void> {
  await db.client_meta.put({ key: "auth", value: auth });
}

export async function getSyncMeta(db: SmartRSSDB): Promise<SyncCursorMeta> {
  const row = await db.client_meta.get("sync");
  return (row?.value as SyncCursorMeta | undefined) ?? { ...DEFAULT_SYNC_META };
}

export async function setSyncMeta(db: SmartRSSDB, meta: SyncCursorMeta): Promise<void> {
  await db.client_meta.put({ key: "sync", value: meta });
}

export async function getAppSettingsMeta(db: SmartRSSDB): Promise<SyncAppSettingsDTO | null> {
  const row = await db.client_meta.get("app_settings");
  return (row?.value as SyncAppSettingsDTO | null | undefined) ?? null;
}

export async function setAppSettingsMeta(
  db: SmartRSSDB,
  settings: SyncAppSettingsDTO | null,
): Promise<void> {
  await db.client_meta.put({ key: "app_settings", value: settings });
}

export async function getInterestProfileMeta(
  db: SmartRSSDB,
): Promise<SyncInterestProfileDTO | null> {
  const row = await db.client_meta.get("interest_profile");
  return (row?.value as SyncInterestProfileDTO | null | undefined) ?? null;
}

export async function setInterestProfileMeta(
  db: SmartRSSDB,
  profile: SyncInterestProfileDTO | null,
): Promise<void> {
  await db.client_meta.put({ key: "interest_profile", value: profile });
}

export async function getUiPrefs(db: SmartRSSDB): Promise<UiPrefs> {
  const row = await db.client_meta.get("ui_prefs");
  return {
    ...DEFAULT_UI_PREFS,
    ...((row?.value as Partial<UiPrefs> | undefined) ?? {}),
  };
}

export async function setUiPrefs(db: SmartRSSDB, prefs: UiPrefs): Promise<void> {
  await db.client_meta.put({ key: "ui_prefs", value: prefs });
}

export async function requestStoragePersistence(): Promise<StoragePersistenceStatus> {
  const storage = globalThis.navigator?.storage;
  if (!storage || typeof storage.persist !== "function") {
    return "unsupported";
  }
  try {
    if (typeof storage.persisted === "function") {
      const already = await storage.persisted();
      if (already) {
        return "granted";
      }
    }
    const granted = await storage.persist();
    return granted ? "granted" : "denied";
  } catch {
    return "denied";
  }
}

export interface StorageEstimateInfo {
  usageBytes: number | null;
  quotaBytes: number | null;
}

export async function getStorageEstimate(): Promise<StorageEstimateInfo> {
  const storage = globalThis.navigator?.storage;
  if (!storage || typeof storage.estimate !== "function") {
    return { usageBytes: null, quotaBytes: null };
  }
  try {
    const est = await storage.estimate();
    return {
      usageBytes: typeof est.usage === "number" ? est.usage : null,
      quotaBytes: typeof est.quota === "number" ? est.quota : null,
    };
  } catch {
    return { usageBytes: null, quotaBytes: null };
  }
}
