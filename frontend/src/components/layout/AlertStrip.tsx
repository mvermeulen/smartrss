import type { SyncNotificationRow } from "../../db/schema";
import { isClockDriftExcessive } from "../../sync/reconciliation";
import type { ClientRuntimeConfig } from "../../types/api";
import type { ConnectionState, StoragePersistenceStatus } from "../../types/ui";

export function AlertStrip({
  connectionState,
  statusDetail,
  runtimeConfig,
  pendingOriginChange,
  onConfirmOriginChange,
  onSignOutAndClear,
  clockDriftMs,
  storageStatus,
  swUpdateAvailable,
  onApplySwUpdate,
  schemaUpgradeRequired,
  cspReloadRequired,
  notifications,
  onDismissNotification,
  onOpenRepair,
}: {
  connectionState: ConnectionState;
  statusDetail: string | null;
  runtimeConfig: ClientRuntimeConfig;
  pendingOriginChange: { oldOrigin: string; newOrigin: string } | null;
  onConfirmOriginChange: () => void;
  onSignOutAndClear: () => void;
  clockDriftMs: number;
  storageStatus: StoragePersistenceStatus;
  swUpdateAvailable: boolean;
  onApplySwUpdate: () => void;
  schemaUpgradeRequired: boolean;
  cspReloadRequired: boolean;
  notifications: readonly SyncNotificationRow[];
  onDismissNotification: (id: number) => void;
  onOpenRepair: () => void;
}) {
  const showClockDrift = isClockDriftExcessive(clockDriftMs);
  const driftMinutes = Math.round(Math.abs(clockDriftMs) / 60_000);
  const showPersistWarning = runtimeConfig.mode === "localhost" && storageStatus === "denied";
  const showLanFallbackInfo = runtimeConfig.mode === "lan_fallback";

  return (
    <div className="space-y-1 px-3 pt-1">
      {pendingOriginChange && (
        <div
          role="alert"
          data-testid="origin-change-banner"
          className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-100"
        >
          <span>
            Server address changed from{" "}
            <code className="font-mono">{pendingOriginChange.oldOrigin}</code> to{" "}
            <code className="font-mono">{pendingOriginChange.newOrigin}</code>. Is this the same
            SmartRSS server? (Sync paused; no token sent.)
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onConfirmOriginChange}
              className="rounded bg-amber-700 px-2.5 py-1 font-medium text-white hover:bg-amber-800"
            >
              Confirm — Same Server
            </button>
            <button
              type="button"
              onClick={onSignOutAndClear}
              className="rounded border border-amber-400 px-2.5 py-1 font-medium hover:bg-amber-100 dark:hover:bg-amber-900"
            >
              Sign Out & Pair New
            </button>
          </div>
        </div>
      )}

      {cspReloadRequired && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-100"
        >
          <span>Reload required to apply the updated server origin CSP policy.</span>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded bg-amber-700 px-2.5 py-1 font-medium text-white"
          >
            Reload Now
          </button>
        </div>
      )}

      {connectionState === "auth_revoked" && (
        <div
          role="alert"
          data-testid="auth-revoked-banner"
          className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-xs text-rose-950 dark:border-rose-800 dark:bg-rose-950/60 dark:text-rose-100"
        >
          <span>
            Device token revoked. Sync paused — local cache and pending offline edits are preserved.
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onOpenRepair}
              className="rounded bg-rose-700 px-2.5 py-1 font-medium text-white hover:bg-rose-800"
            >
              Re-pair Device
            </button>
            <button
              type="button"
              onClick={onSignOutAndClear}
              className="rounded border border-rose-400 px-2.5 py-1 font-medium"
            >
              Sign Out & Clear Cache
            </button>
          </div>
        </div>
      )}

      {connectionState === "misconfigured" && statusDetail && (
        <div
          role="alert"
          data-testid="misconfigured-banner"
          className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-xs text-rose-950 dark:border-rose-800 dark:bg-rose-950/60 dark:text-rose-100"
        >
          <strong>Connection Misconfigured:</strong> {statusDetail}
        </div>
      )}

      {connectionState === "sync_error" && statusDetail && (
        <div
          role="alert"
          data-testid="sync-error-banner"
          className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-xs text-rose-950 dark:border-rose-800 dark:bg-rose-950/60 dark:text-rose-100"
        >
          <strong>Sync Error:</strong> {statusDetail}
        </div>
      )}

      {showClockDrift && (
        <div
          role="alert"
          data-testid="clock-drift-banner"
          className="rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs text-amber-950 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-100"
        >
          Device clock is {clockDriftMs > 0 ? "ahead of" : "behind"} server by ~{driftMinutes} min.
          Sync your system clock (NTP) so offline conflict timestamps are not clamped or rejected.
        </div>
      )}

      {showPersistWarning && (
        <div
          role="status"
          data-testid="storage-persist-warning"
          className="rounded-md border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200"
        >
          Persistent storage was not granted by the browser. Bookmark or install SmartRSS to protect
          your offline cache and outbox from storage eviction.
        </div>
      )}

      {showLanFallbackInfo && (
        <div
          data-testid="lan-fallback-banner"
          className="rounded-md border border-slate-200 bg-slate-100 px-3 py-1 text-xs text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300"
        >
          Connected LAN view — full reading &amp; actions enabled; use the localhost runner (
          <code className="font-mono">http://localhost:4180</code>) for offline detached mode.
        </div>
      )}

      {(swUpdateAvailable || schemaUpgradeRequired) && (
        <div
          role="status"
          data-testid="sw-update-banner"
          className="flex items-center justify-between gap-2 rounded-md border border-sky-300 bg-sky-50 px-3 py-1.5 text-xs text-sky-950 dark:border-sky-800 dark:bg-sky-950/60 dark:text-sky-100"
        >
          <span>
            {schemaUpgradeRequired
              ? "A newer SmartRSS database schema was opened in another tab — reload to continue."
              : "A new version of SmartRSS is ready."}
          </span>
          <button
            type="button"
            data-testid="sw-update-reload-btn"
            onClick={schemaUpgradeRequired ? () => window.location.reload() : onApplySwUpdate}
            className="rounded bg-sky-600 px-2.5 py-1 font-medium text-white hover:bg-sky-700"
          >
            Reload
          </button>
        </div>
      )}

      {notifications.map((n) => (
        <div
          key={n.id}
          role="status"
          data-testid="sync-notification-toast"
          className={`flex items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-xs ${
            n.severity === "error"
              ? "border-rose-300 bg-rose-50 text-rose-950 dark:border-rose-800 dark:bg-rose-950/60 dark:text-rose-100"
              : n.severity === "warning"
                ? "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-100"
                : "border-sky-200 bg-sky-50 text-sky-950 dark:border-sky-800 dark:bg-sky-950/50 dark:text-sky-100"
          }`}
        >
          <span>
            <strong>{n.title}:</strong> {n.detail}
          </span>
          {n.id !== undefined && (
            <button
              type="button"
              onClick={() => onDismissNotification(n.id as number)}
              className="shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium hover:bg-black/10"
            >
              Dismiss
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
