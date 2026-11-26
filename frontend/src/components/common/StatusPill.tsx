import type { ConnectionState } from "../../types/ui";

const LABELS: Record<ConnectionState, string> = {
  unpaired: "Unpaired",
  origin_confirm: "Confirm Server",
  bootstrapping: "Bootstrapping…",
  online: "Online",
  syncing: "Syncing…",
  detached: "Detached",
  misconfigured: "Misconfigured",
  sync_error: "Sync Error",
  auth_revoked: "Auth Revoked",
};

const STYLES: Record<ConnectionState, string> = {
  unpaired: "bg-slate-200 text-slate-800 dark:bg-slate-800 dark:text-slate-200",
  origin_confirm: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  bootstrapping: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
  online: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  syncing: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
  detached: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  misconfigured: "bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
  sync_error: "bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
  auth_revoked: "bg-rose-200 text-rose-950 dark:bg-rose-900/60 dark:text-rose-100",
};

export function StatusPill({ state, detail }: { state: ConnectionState; detail?: string | null }) {
  return (
    <span
      data-testid="connection-status-pill"
      data-connection-state={state}
      title={detail ?? LABELS[state]}
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${STYLES[state]}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
      {LABELS[state]}
    </span>
  );
}
