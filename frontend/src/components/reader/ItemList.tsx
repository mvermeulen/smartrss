import { useState } from "react";
import type { LocalItemRecord } from "../../db/schema";
import type { SearchItemDTO, SnippetFragmentDTO } from "../../types/api";
import type { SyncFeedDTO } from "../../types/sync";
import type { SortDir, SortKey } from "../../types/ui";
import { SnippetText } from "./SnippetText";

export interface DisplayListEntry {
  id: string;
  item: LocalItemRecord | SearchItemDTO;
  isArchiveOnly: boolean;
  snippet?: SnippetFragmentDTO[][];
}

function formatRelativeTime(iso: string): string {
  const diffMs = Date.now() - Date.parse(iso);
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return iso.slice(0, 10);
}

const TIER_BADGES: Record<number, { label: string; cls: string }> = {
  5: {
    label: "T5 Must",
    cls: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-200",
  },
  4: { label: "T4 High", cls: "bg-sky-100 text-sky-900 dark:bg-sky-900/50 dark:text-sky-200" },
  3: {
    label: "T3 Look",
    cls: "bg-indigo-100 text-indigo-900 dark:bg-indigo-900/50 dark:text-indigo-200",
  },
  2: { label: "T2 Low", cls: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300" },
  1: {
    label: "T1 Skip",
    cls: "bg-slate-100 text-slate-500 dark:bg-slate-800/60 dark:text-slate-400",
  },
};

export function ItemList({
  scopeLabel,
  entries,
  feedsById,
  selectedId,
  onSelect,
  onToggleStar,
  sortKey,
  sortDir,
  onToggleSort,
  isSearchMode,
  searchError,
  searchLoading,
  canBulkRead,
  onTriggerBulkRead,
  tierFilter,
  onTierFilterChange,
  depthFilter,
  onDepthFilterChange,
  hasEnrichedItems,
}: {
  scopeLabel: string;
  entries: readonly DisplayListEntry[];
  feedsById: Map<string, SyncFeedDTO>;
  selectedId: string | null;
  onSelect: (entry: DisplayListEntry) => void;
  onToggleStar: (entry: DisplayListEntry) => void;
  sortKey: SortKey;
  sortDir: SortDir;
  onToggleSort: (key: SortKey) => void;
  isSearchMode: boolean;
  searchError: string | null;
  searchLoading: boolean;
  canBulkRead: boolean;
  onTriggerBulkRead: (olderThanHours?: number) => void;
  tierFilter: number | null;
  onTierFilterChange: (tier: number | null) => void;
  depthFilter: string | null;
  onDepthFilterChange: (depth: string | null) => void;
  hasEnrichedItems: boolean;
}) {
  const [visibleLimit, setVisibleLimit] = useState(80);
  const [bulkMenuOpen, setBulkMenuOpen] = useState(false);

  const visibleEntries = entries.slice(0, visibleLimit);
  const sortButtons: Array<{ key: SortKey; label: string }> = [
    { key: "relevance", label: "Relevance" },
    { key: "date", label: "Date" },
    { key: "feed", label: "Feed" },
    { key: "depth", label: "Depth" },
  ];

  return (
    <section
      data-testid="reader-item-list"
      className="flex w-full shrink-0 flex-col border-r border-slate-200 bg-white md:w-96 lg:w-[26rem] dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-3 py-2 dark:border-slate-800">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{scopeLabel}</h2>
          <p className="text-[11px] text-slate-500">
            {searchLoading
              ? "Searching…"
              : `${entries.length} item${entries.length === 1 ? "" : "s"}`}
          </p>
        </div>

        {!isSearchMode && (
          <div className="relative">
            <button
              type="button"
              data-testid="bulk-mark-read-menu-btn"
              disabled={!canBulkRead}
              onClick={() => setBulkMenuOpen((v) => !v)}
              className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              Mark Read ▾
            </button>
            {bulkMenuOpen && (
              <div className="absolute right-0 z-20 mt-1 w-48 rounded-md border border-slate-200 bg-white py-1 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-900">
                <button
                  type="button"
                  data-testid="bulk-read-all-btn"
                  onClick={() => {
                    setBulkMenuOpen(false);
                    onTriggerBulkRead(undefined);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  Mark all in view read
                </button>
                <button
                  type="button"
                  data-testid="bulk-read-24h-btn"
                  onClick={() => {
                    setBulkMenuOpen(false);
                    onTriggerBulkRead(24);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  Older than 24 hours
                </button>
                <button
                  type="button"
                  data-testid="bulk-read-7d-btn"
                  onClick={() => {
                    setBulkMenuOpen(false);
                    onTriggerBulkRead(24 * 7);
                  }}
                  className="block w-full px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  Older than 7 days
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Sortable header controls (FR-UI-02) */}
      <div className="flex flex-wrap items-center justify-between gap-1 border-b border-slate-200 bg-slate-50 px-2.5 py-1.5 text-[11px] dark:border-slate-800 dark:bg-slate-950/60">
        <div className="flex items-center gap-1" role="group" aria-label="Sort items">
          {sortButtons.map((btn) => {
            const active = sortKey === btn.key;
            return (
              <button
                key={btn.key}
                type="button"
                data-testid={`sort-btn-${btn.key}`}
                onClick={() => onToggleSort(btn.key)}
                className={`rounded px-2 py-0.5 font-medium ${
                  active
                    ? "bg-sky-600 text-white"
                    : "text-slate-600 hover:bg-slate-200 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                {btn.label}
                {active ? (sortDir === "desc" ? " ↓" : " ↑") : ""}
              </button>
            );
          })}
        </div>

        {hasEnrichedItems && !isSearchMode && (
          <div className="flex items-center gap-1">
            <select
              aria-label="Filter by relevance tier"
              value={tierFilter ?? ""}
              onChange={(e) => onTierFilterChange(e.target.value ? Number(e.target.value) : null)}
              className="rounded border border-slate-300 bg-white px-1 py-0.5 text-[11px] dark:border-slate-700 dark:bg-slate-900"
            >
              <option value="">All tiers</option>
              <option value="5">T5 Must</option>
              <option value="4">T4+</option>
              <option value="3">T3+</option>
            </select>
            <select
              aria-label="Filter by depth"
              value={depthFilter ?? ""}
              onChange={(e) => onDepthFilterChange(e.target.value || null)}
              className="rounded border border-slate-300 bg-white px-1 py-0.5 text-[11px] dark:border-slate-700 dark:bg-slate-900"
            >
              <option value="">All depth</option>
              <option value="deep-dive">Deep Dive</option>
              <option value="paper-report">Paper/Report</option>
              <option value="news-brief">News Brief</option>
              <option value="opinion">Opinion</option>
              <option value="release-notes">Release Notes</option>
            </select>
          </div>
        )}
      </div>

      {searchError && (
        <div
          role="alert"
          data-testid="search-error"
          className="m-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800 dark:border-rose-900 dark:bg-rose-950/60 dark:text-rose-200"
        >
          {searchError}
        </div>
      )}

      <div className="flex-1 overflow-y-auto divide-y divide-slate-100 dark:divide-slate-800">
        {visibleEntries.length === 0 && !searchLoading && !searchError && (
          <div className="p-6 text-center text-xs text-slate-500">No items match this view.</div>
        )}

        {visibleEntries.map((entry) => {
          const item = entry.item;
          const feed = feedsById.get(item.feed_id);
          const feedTitle = feed?.title ?? "Unknown feed";
          const isSelected = selectedId === entry.id;
          const isUnread = item.state === "unread";
          const tier = item.enrichment?.relevance_tier ?? null;
          const tierBadge = tier !== null ? TIER_BADGES[tier] : undefined;
          const dateIso = item.published_at ?? item.fetched_at;

          return (
            <article
              key={entry.id}
              data-testid={`item-row-${entry.id}`}
              data-item-state={item.state}
              data-item-starred={String(item.is_starred)}
              onClick={() => onSelect(entry)}
              className={`cursor-pointer px-3 py-2.5 transition-colors ${
                isSelected
                  ? "bg-sky-50 dark:bg-sky-950/50"
                  : "hover:bg-slate-50 dark:hover:bg-slate-800/50"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-center gap-1.5">
                  {isUnread && (
                    <span
                      className="h-2 w-2 shrink-0 rounded-full bg-sky-500"
                      aria-label="Unread"
                    />
                  )}
                  <span className="truncate text-[11px] font-medium text-slate-500 dark:text-slate-400">
                    {feedTitle}
                  </span>
                  {tierBadge && (
                    <span
                      className={`shrink-0 rounded px-1 py-0.5 text-[10px] font-semibold ${tierBadge.cls}`}
                    >
                      {tierBadge.label}
                    </span>
                  )}
                  {entry.isArchiveOnly && (
                    <span className="shrink-0 rounded bg-slate-200 px-1 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                      Archive
                    </span>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <span className="text-[11px] text-slate-400">{formatRelativeTime(dateIso)}</span>
                  <button
                    type="button"
                    aria-label={item.is_starred ? "Unstar item" : "Star item"}
                    onClick={(e) => {
                      e.stopPropagation();
                      onToggleStar(entry);
                    }}
                    className={`text-sm leading-none ${
                      item.is_starred
                        ? "text-amber-500"
                        : "text-slate-300 hover:text-amber-400 dark:text-slate-600"
                    }`}
                  >
                    {item.is_starred ? "★" : "☆"}
                  </button>
                </div>
              </div>

              <h3
                className={`mt-1 text-sm leading-snug ${
                  isUnread
                    ? "font-semibold text-slate-900 dark:text-slate-100"
                    : "font-normal text-slate-600 dark:text-slate-400"
                }`}
              >
                {item.title}
              </h3>

              {entry.snippet && entry.snippet.length > 0 ? (
                <SnippetText fragments={entry.snippet} />
              ) : (
                item.lead_excerpt && (
                  <p className="mt-1 line-clamp-2 text-xs text-slate-500 dark:text-slate-400">
                    {item.lead_excerpt}
                  </p>
                )
              )}

              {item.tags && item.tags.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {item.tags.slice(0, 4).map((tag) => (
                    <span
                      key={tag}
                      className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                    >
                      #{tag}
                    </span>
                  ))}
                </div>
              )}
            </article>
          );
        })}

        {entries.length > visibleLimit && (
          <div className="p-3 text-center">
            <button
              type="button"
              onClick={() => setVisibleLimit((n) => n + 80)}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              Load more ({entries.length - visibleLimit} remaining)
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
