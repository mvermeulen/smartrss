import type { RefObject } from "react";
import type { AppView, ConnectionState, ThemeMode } from "../../types/ui";
import { StatusPill } from "../common/StatusPill";

export function TopBar({
  activeView,
  onSelectView,
  connectionState,
  statusDetail,
  pendingOutboxCount,
  searchQuery,
  onSearchQueryChange,
  searchInputRef,
  localSearchForced,
  onToggleLocalSearchForced,
  theme,
  onCycleTheme,
  onManualSync,
  onOpenShortcuts,
  onToggleSidebar,
}: {
  activeView: AppView;
  onSelectView: (view: AppView) => void;
  connectionState: ConnectionState;
  statusDetail: string | null;
  pendingOutboxCount: number;
  searchQuery: string;
  onSearchQueryChange: (q: string) => void;
  searchInputRef: RefObject<HTMLInputElement | null>;
  localSearchForced: boolean;
  onToggleLocalSearchForced: () => void;
  theme: ThemeMode;
  onCycleTheme: () => void;
  onManualSync: () => void;
  onOpenShortcuts: () => void;
  onToggleSidebar: () => void;
}) {
  const isOnline = connectionState === "online" || connectionState === "syncing";
  const isDetachedSearch = !isOnline || localSearchForced;

  const navItems: Array<{ id: AppView; label: string }> = [
    { id: "reader", label: "Reader" },
    { id: "feeds", label: "Feeds & OPML" },
    { id: "health", label: "Health & Metrics" },
    { id: "settings", label: "Settings & Devices" },
  ];

  return (
    <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-white px-3 py-2 dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onToggleSidebar}
          aria-label="Toggle sidebar"
          className="rounded border border-slate-200 px-2 py-1 text-xs md:hidden dark:border-slate-700"
        >
          ☰
        </button>
        <span className="text-sm font-bold tracking-tight text-sky-700 dark:text-sky-400">
          SmartRSS
        </span>
        <nav aria-label="Primary navigation" className="flex items-center gap-1">
          {navItems.map((tab) => (
            <button
              key={tab.id}
              type="button"
              data-testid={`nav-${tab.id}`}
              onClick={() => onSelectView(tab.id)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                activeView === tab.id
                  ? "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      <div className="flex flex-1 items-center justify-center gap-1.5 min-w-48 max-w-md">
        <div className="relative flex-1">
          <input
            ref={searchInputRef}
            data-testid="global-search-input"
            type="search"
            value={searchQuery}
            onChange={(e) => {
              if (activeView !== "reader") {
                onSelectView("reader");
              }
              onSearchQueryChange(e.target.value);
            }}
            placeholder={isDetachedSearch ? "Search local cache… (/)" : "Search full archive… (/)"}
            className="w-full rounded-md border border-slate-300 bg-slate-50 px-2.5 py-1 text-xs focus:border-sky-500 focus:bg-white focus:outline-none dark:border-slate-700 dark:bg-slate-950 dark:focus:bg-slate-900"
          />
        </div>
        <button
          type="button"
          data-testid="search-mode-badge"
          disabled={!isOnline}
          onClick={onToggleLocalSearchForced}
          title={
            !isOnline
              ? "Detached — searching local Dexie working-set cache"
              : "Click to toggle Full Archive vs Local Cache Only"
          }
          className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${
            isDetachedSearch
              ? "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200"
              : "bg-slate-100 text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300"
          }`}
        >
          {isDetachedSearch
            ? !isOnline
              ? "Detached — Local Cache Only"
              : "Local Cache Only"
            : "Full Archive"}
        </button>
      </div>

      <div className="flex items-center gap-2">
        <StatusPill state={connectionState} detail={statusDetail} />
        {pendingOutboxCount > 0 && (
          <button
            type="button"
            data-testid="pending-outbox-badge"
            onClick={() => onSelectView("settings")}
            className="rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-semibold text-indigo-900 hover:bg-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-200"
            title="Pending offline mutations — click to inspect"
          >
            {pendingOutboxCount} queued
          </button>
        )}
        <button
          type="button"
          data-testid="manual-sync-btn"
          onClick={onManualSync}
          title="Sync now (r)"
          className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          Sync
        </button>
        <button
          type="button"
          onClick={onOpenShortcuts}
          title="Keyboard shortcuts (?)"
          className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          ?
        </button>
        <button
          type="button"
          onClick={onCycleTheme}
          title={`Theme: ${theme}`}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium capitalize hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          {theme}
        </button>
      </div>
    </header>
  );
}
