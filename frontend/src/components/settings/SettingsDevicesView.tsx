import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { SmartRSSApiClient } from "../../api/client";
import type { AuthMeta, FailedMutationRow, OutboxRow } from "../../db/schema";
import type { CreatePairingCodeResponse, DevicePlatform, DeviceResponse } from "../../types/api";
import type { SyncAppSettingsDTO } from "../../types/sync";

export function SettingsDevicesView({
  isOnline,
  apiClient,
  authMeta,
  appSettings,
  outboxRows,
  failedRows,
  onSyncNow,
  onForceFullResync,
  onSignOutAndClear,
  onManualQuarantine,
  onRetryFailed,
  onDismissFailed,
  onSettingsUpdated,
}: {
  isOnline: boolean;
  apiClient: SmartRSSApiClient | null;
  authMeta: AuthMeta | null;
  appSettings: SyncAppSettingsDTO | null;
  outboxRows: readonly OutboxRow[];
  failedRows: readonly FailedMutationRow[];
  onSyncNow: () => void;
  onForceFullResync: () => Promise<void>;
  onSignOutAndClear: () => Promise<void>;
  onManualQuarantine: (localSeq: number) => Promise<void>;
  onRetryFailed: (mutationId: string) => Promise<void>;
  onDismissFailed: (mutationId: string) => Promise<void>;
  onSettingsUpdated: () => Promise<void>;
}) {
  // App settings form
  const [expDays, setExpDays] = useState(appSettings?.default_expiration_days ?? 30);
  const [backfillCap, setBackfillCap] = useState(appSettings?.backfill_cap ?? 10);
  const [pollInterval, setPollInterval] = useState(
    appSettings?.default_poll_interval_minutes ?? 60,
  );
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null);

  // Devices & pairing code
  const [devices, setDevices] = useState<DeviceResponse[]>([]);
  const [newDevName, setNewDevName] = useState("Second Device");
  const [newDevPlatform, setNewDevPlatform] = useState<DevicePlatform>("ubuntu");
  const [issuedCode, setIssuedCode] = useState<CreatePairingCodeResponse | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [deviceMsg, setDeviceMsg] = useState<string | null>(null);

  useEffect(() => {
    if (appSettings) {
      setExpDays(appSettings.default_expiration_days);
      setBackfillCap(appSettings.backfill_cap);
      setPollInterval(appSettings.default_poll_interval_minutes);
    }
  }, [appSettings]);

  const loadDevices = useCallback(async () => {
    if (!apiClient || !isOnline) return;
    try {
      setDevices(await apiClient.listDevices());
    } catch (err) {
      setDeviceMsg(err instanceof Error ? err.message : String(err));
    }
  }, [apiClient, isOnline]);

  useEffect(() => {
    void loadDevices();
  }, [loadDevices]);

  useEffect(() => {
    if (!issuedCode) return;
    const tick = () => {
      const rem = Math.max(0, Math.floor((Date.parse(issuedCode.expires_at) - Date.now()) / 1000));
      setSecondsLeft(rem);
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [issuedCode]);

  const handleSaveSettings = async (e: FormEvent) => {
    e.preventDefault();
    if (!apiClient || !isOnline) return;
    setSettingsMsg(null);
    try {
      await apiClient.updateSettings({
        default_expiration_days: expDays,
        backfill_cap: backfillCap,
        default_poll_interval_minutes: pollInterval,
      });
      setSettingsMsg("Settings saved.");
      await onSettingsUpdated();
    } catch (err) {
      setSettingsMsg(err instanceof Error ? err.message : String(err));
    }
  };

  const handleCreatePairingCode = async (e: FormEvent) => {
    e.preventDefault();
    if (!apiClient || !isOnline) return;
    setDeviceMsg(null);
    try {
      const code = await apiClient.createPairingCode({
        device_name: newDevName.trim() || "New Device",
        platform: newDevPlatform,
      });
      setIssuedCode(code);
    } catch (err) {
      setDeviceMsg(err instanceof Error ? err.message : String(err));
    }
  };

  const handleRevoke = async (dev: DeviceResponse) => {
    if (!apiClient || !isOnline) return;
    const isSelf = authMeta?.device_id === dev.id;
    const ok = window.confirm(
      isSelf
        ? `Revoke THIS device (${dev.name})? You will need to re-pair it.`
        : `Revoke device "${dev.name}"?`,
    );
    if (!ok) return;
    try {
      await apiClient.revokeDevice(dev.id);
      setDeviceMsg(`Revoked ${dev.name}.`);
      await loadDevices();
    } catch (err) {
      setDeviceMsg(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div data-testid="settings-devices-view" className="flex-1 overflow-y-auto p-5">
      <div className="mx-auto max-w-5xl space-y-6">
        <h1 className="text-lg font-bold">Settings, Devices &amp; Outbox Inspector</h1>

        <div className="grid gap-4 md:grid-cols-2">
          {/* Global App Settings */}
          <form
            onSubmit={handleSaveSettings}
            className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900"
          >
            <h2 className="text-sm font-semibold">Global App Settings (Online-Only)</h2>
            <div className="mt-3 space-y-2.5">
              <div>
                <label className="block font-medium">
                  Default Unread Expiration (days; 0 = never)
                </label>
                <input
                  type="number"
                  min={0}
                  max={3650}
                  disabled={!isOnline}
                  value={expDays}
                  onChange={(e) => setExpDays(Number(e.target.value))}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
                />
              </div>
              <div>
                <label className="block font-medium">Initial Backfill Cap (items/feed)</label>
                <input
                  type="number"
                  min={0}
                  max={500}
                  disabled={!isOnline}
                  value={backfillCap}
                  onChange={(e) => setBackfillCap(Number(e.target.value))}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
                />
              </div>
              <div>
                <label className="block font-medium">Default Poll Interval (minutes)</label>
                <input
                  type="number"
                  min={5}
                  max={10080}
                  disabled={!isOnline}
                  value={pollInterval}
                  onChange={(e) => setPollInterval(Number(e.target.value))}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
                />
              </div>
              <button
                type="submit"
                disabled={!isOnline}
                className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700 disabled:opacity-40"
              >
                Save Global Settings
              </button>
              {settingsMsg && (
                <p className="text-emerald-700 dark:text-emerald-400">{settingsMsg}</p>
              )}
            </div>
          </form>

          {/* Generate Pairing Code */}
          <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold">Pair Another Device (Online-Only)</h2>
            <form onSubmit={handleCreatePairingCode} className="mt-3 space-y-2.5">
              <input
                type="text"
                disabled={!isOnline}
                value={newDevName}
                onChange={(e) => setNewDevName(e.target.value)}
                placeholder="Device name"
                className="w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              />
              <select
                disabled={!isOnline}
                value={newDevPlatform}
                onChange={(e) => setNewDevPlatform(e.target.value as DevicePlatform)}
                className="w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              >
                <option value="ubuntu">Ubuntu</option>
                <option value="android">Android</option>
                <option value="web">Web</option>
              </select>
              <button
                type="submit"
                data-testid="generate-pairing-code-btn"
                disabled={!isOnline}
                className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700 disabled:opacity-40"
              >
                Generate One-Time Pairing Code
              </button>
            </form>

            {issuedCode && (
              <div
                data-testid="generated-pairing-code"
                className="mt-3 rounded-md border border-emerald-300 bg-emerald-50 p-3 dark:border-emerald-900 dark:bg-emerald-950/50"
              >
                <div className="font-mono text-base font-bold tracking-wider">
                  {issuedCode.code}
                </div>
                <div className="mt-1 text-[11px] text-slate-600 dark:text-slate-300">
                  Expires in {secondsLeft}s
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Paired Devices Table */}
        <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-sm font-semibold">Paired Devices ({devices.length})</h2>
          {deviceMsg && <p className="mt-1 text-sky-700 dark:text-sky-300">{deviceMsg}</p>}
          <div className="mt-2 overflow-x-auto">
            <table data-testid="devices-table" className="w-full text-left">
              <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="py-1.5 pr-3">Name</th>
                  <th className="py-1.5 pr-3">Platform</th>
                  <th className="py-1.5 pr-3">Last Seen</th>
                  <th className="py-1.5 pr-3">Status</th>
                  <th className="py-1.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {devices.map((dev) => {
                  const isSelf = authMeta?.device_id === dev.id;
                  return (
                    <tr key={dev.id}>
                      <td className="py-1.5 pr-3 font-medium">
                        {dev.name}{" "}
                        {isSelf && (
                          <span className="rounded bg-sky-100 px-1 text-[10px] text-sky-800 dark:bg-sky-900/50 dark:text-sky-200">
                            This device
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-3 capitalize">{dev.platform}</td>
                      <td className="py-1.5 pr-3">
                        {dev.last_seen_at ? new Date(dev.last_seen_at).toLocaleString() : "—"}
                      </td>
                      <td className="py-1.5 pr-3">{dev.revoked_at ? "Revoked" : "Active"}</td>
                      <td className="py-1.5 text-right">
                        {!dev.revoked_at && (
                          <button
                            type="button"
                            data-testid={`revoke-device-${dev.id}`}
                            disabled={!isOnline}
                            onClick={() => void handleRevoke(dev)}
                            className="rounded border border-rose-300 px-2 py-0.5 font-medium text-rose-700 hover:bg-rose-50 disabled:opacity-40 dark:border-rose-800 dark:text-rose-300"
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Outbox Inspector & Diagnostics (U-02, U-04, U-04-F1, U-21) */}
        <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold">
              Outbox Inspector ({outboxRows.length} pending, {failedRows.length} quarantined)
            </h2>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={onSyncNow}
                className="rounded border border-slate-300 px-2.5 py-1 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              >
                Sync Now
              </button>
              <button
                type="button"
                disabled={!isOnline}
                onClick={() => void onForceFullResync()}
                className="rounded border border-amber-300 px-2.5 py-1 font-medium text-amber-800 hover:bg-amber-50 disabled:opacity-40 dark:border-amber-800 dark:text-amber-300"
              >
                Force Full Resync
              </button>
              <button
                type="button"
                data-testid="sign-out-clear-btn"
                onClick={() => void onSignOutAndClear()}
                className="rounded border border-rose-300 px-2.5 py-1 font-medium text-rose-700 hover:bg-rose-50 dark:border-rose-800 dark:text-rose-300"
              >
                Sign Out &amp; Clear Local Cache
              </button>
            </div>
          </div>

          {outboxRows.length > 0 && (
            <div>
              <h3 className="mb-1 font-semibold">Pending Outbox</h3>
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {outboxRows.map((row) => (
                  <li
                    key={row.client_mutation_id}
                    className="flex items-center justify-between gap-2 py-1.5"
                  >
                    <div>
                      <span className="font-mono font-medium">{row.payload.type}</span> —{" "}
                      <span>{row.display_label}</span>{" "}
                      <span className="text-slate-400">(attempts: {row.attempts})</span>
                      {row.last_error && (
                        <div className="text-rose-600 dark:text-rose-400">{row.last_error}</div>
                      )}
                    </div>
                    {row.local_seq !== undefined && (
                      <button
                        type="button"
                        data-testid={`manual-quarantine-${row.client_mutation_id}`}
                        onClick={() => void onManualQuarantine(row.local_seq as number)}
                        className="shrink-0 rounded border border-amber-300 px-2 py-0.5 font-medium text-amber-800 hover:bg-amber-50 dark:border-amber-800 dark:text-amber-300"
                      >
                        Move to quarantine
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {failedRows.length > 0 && (
            <div>
              <h3 className="mb-1 font-semibold text-rose-700 dark:text-rose-400">
                Quarantined Mutations
              </h3>
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {failedRows.map((row) => (
                  <li
                    key={row.client_mutation_id}
                    className="flex items-center justify-between gap-2 py-1.5"
                  >
                    <div>
                      <span className="font-mono font-medium">{row.payload.type}</span> —{" "}
                      <span>{row.display_label}</span>
                      <div className="text-rose-600 dark:text-rose-400">{row.reason}</div>
                    </div>
                    <div className="flex shrink-0 gap-1.5">
                      <button
                        type="button"
                        data-testid={`retry-failed-${row.client_mutation_id}`}
                        onClick={() => void onRetryFailed(row.client_mutation_id)}
                        className="rounded bg-sky-600 px-2 py-0.5 font-medium text-white hover:bg-sky-700"
                      >
                        Retry
                      </button>
                      <button
                        type="button"
                        onClick={() => void onDismissFailed(row.client_mutation_id)}
                        className="rounded border border-slate-300 px-2 py-0.5 font-medium dark:border-slate-700"
                      >
                        Dismiss
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
