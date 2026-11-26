import { useState } from "react";
import type { SyncCategoryDTO, SyncFeedDTO } from "../../types/sync";
import type { ReaderScope, StateFilter } from "../../types/ui";

function healthDotClass(health: SyncFeedDTO["health_state"]): string {
  switch (health) {
    case "degraded":
      return "bg-amber-500";
    case "broken":
    case "gone":
      return "bg-rose-500";
    default:
      return "bg-emerald-500";
  }
}

export function Sidebar({
  openMobile,
  scope,
  onSelectScope,
  categories,
  feeds,
  unreadCountByFeed,
  smartCounts,
  isOnline,
  onOpenAddFeed,
  onReactivateFeed,
}: {
  openMobile: boolean;
  scope: ReaderScope;
  onSelectScope: (next: ReaderScope) => void;
  categories: readonly SyncCategoryDTO[];
  feeds: readonly SyncFeedDTO[];
  unreadCountByFeed: Map<string, number>;
  smartCounts: { unread: number; starred: number; all: number };
  isOnline: boolean;
  onOpenAddFeed: () => void;
  onReactivateFeed: (feed: SyncFeedDTO) => void;
}) {
  const [collapsedCats, setCollapsedCats] = useState<Set<string>>(new Set());
  const [showUnsubscribed, setShowUnsubscribed] = useState(false);

  const activeFeeds = feeds.filter((f) => f.status !== "unsubscribed");
  const unsubscribedFeeds = feeds.filter((f) => f.status === "unsubscribed");
  const catIds = new Set(categories.map((c) => c.id));
  const uncategorizedFeeds = activeFeeds.filter(
    (f) => !f.category_id || !catIds.has(f.category_id),
  );

  const toggleCat = (id: string) => {
    setCollapsedCats((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const smartItems: Array<{ id: StateFilter; label: string; count?: number }> = [
    { id: "unread", label: "Unread", count: smartCounts.unread },
    { id: "starred", label: "Starred", count: smartCounts.starred },
    { id: "all", label: "All Active", count: smartCounts.all },
    { id: "expired", label: "Expired / Archive" },
  ];

  const isSelectedSmart = (id: StateFilter) => scope.kind === "smart" && scope.smart === id;

  return (
    <aside
      data-testid="reader-sidebar"
      className={`${
        openMobile ? "flex" : "hidden md:flex"
      } w-64 shrink-0 flex-col border-r border-slate-200 bg-slate-50/70 dark:border-slate-800 dark:bg-slate-900/60`}
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2 dark:border-slate-800">
        <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
          Library
        </span>
        <button
          type="button"
          data-testid="sidebar-add-feed-btn"
          disabled={!isOnline}
          title={isOnline ? "Subscribe to a new feed" : "Requires home LAN connection"}
          onClick={onOpenAddFeed}
          className="rounded bg-sky-600 px-2 py-0.5 text-xs font-medium text-white hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          + Add Feed
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-2 text-xs">
        <div className="space-y-0.5">
          {smartItems.map((s) => (
            <button
              key={s.id}
              type="button"
              data-testid={`smart-filter-${s.id}`}
              onClick={() => onSelectScope({ kind: "smart", smart: s.id })}
              className={`flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-left font-medium ${
                isSelectedSmart(s.id)
                  ? "bg-sky-100 text-sky-950 dark:bg-sky-900/50 dark:text-sky-100"
                  : "text-slate-700 hover:bg-slate-200/70 dark:text-slate-300 dark:hover:bg-slate-800"
              }`}
            >
              <span>{s.label}</span>
              {s.count !== undefined && (
                <span className="rounded-full bg-slate-200 px-1.5 py-0.5 text-[11px] font-semibold text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  {s.count}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="mt-4 space-y-2">
          {categories.map((cat) => {
            const catFeeds = activeFeeds.filter((f) => f.category_id === cat.id);
            const catUnread = catFeeds.reduce(
              (acc, f) => acc + (unreadCountByFeed.get(f.id) ?? 0),
              0,
            );
            const collapsed = collapsedCats.has(cat.id);
            const selected = scope.kind === "category" && scope.categoryId === cat.id;

            return (
              <div key={cat.id}>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => toggleCat(cat.id)}
                    aria-label={collapsed ? `Expand ${cat.name}` : `Collapse ${cat.name}`}
                    className="rounded px-1 py-0.5 text-slate-500 hover:bg-slate-200 dark:hover:bg-slate-800"
                  >
                    {collapsed ? "▸" : "▾"}
                  </button>
                  <button
                    type="button"
                    data-testid={`sidebar-category-${cat.id}`}
                    onClick={() => onSelectScope({ kind: "category", categoryId: cat.id })}
                    className={`flex flex-1 items-center justify-between rounded-md px-2 py-1 text-left font-semibold ${
                      selected
                        ? "bg-sky-100 text-sky-950 dark:bg-sky-900/50 dark:text-sky-100"
                        : "text-slate-700 hover:bg-slate-200/70 dark:text-slate-200 dark:hover:bg-slate-800"
                    }`}
                  >
                    <span className="truncate">{cat.name}</span>
                    {catUnread > 0 && (
                      <span className="ml-1 text-[11px] text-slate-500">{catUnread}</span>
                    )}
                  </button>
                </div>

                {!collapsed && catFeeds.length > 0 && (
                  <div className="ml-4 mt-0.5 space-y-0.5 border-l border-slate-200 pl-2 dark:border-slate-800">
                    {catFeeds.map((feed) => (
                      <FeedRowButton
                        key={feed.id}
                        feed={feed}
                        unreadCount={unreadCountByFeed.get(feed.id) ?? 0}
                        selected={scope.kind === "feed" && scope.feedId === feed.id}
                        onSelect={() => onSelectScope({ kind: "feed", feedId: feed.id })}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}

          {uncategorizedFeeds.length > 0 && (
            <div>
              <div className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                Uncategorized
              </div>
              <div className="space-y-0.5">
                {uncategorizedFeeds.map((feed) => (
                  <FeedRowButton
                    key={feed.id}
                    feed={feed}
                    unreadCount={unreadCountByFeed.get(feed.id) ?? 0}
                    selected={scope.kind === "feed" && scope.feedId === feed.id}
                    onSelect={() => onSelectScope({ kind: "feed", feedId: feed.id })}
                  />
                ))}
              </div>
            </div>
          )}

          {unsubscribedFeeds.length > 0 && (
            <div className="border-t border-slate-200 pt-2 dark:border-slate-800">
              <button
                type="button"
                onClick={() => setShowUnsubscribed((v) => !v)}
                className="flex w-full items-center justify-between px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
              >
                <span>Unsubscribed ({unsubscribedFeeds.length})</span>
                <span>{showUnsubscribed ? "▾" : "▸"}</span>
              </button>
              {showUnsubscribed && (
                <div className="mt-1 space-y-1">
                  {unsubscribedFeeds.map((feed) => (
                    <div
                      key={feed.id}
                      className="flex items-center justify-between rounded px-2 py-1 text-slate-500"
                    >
                      <span className="truncate">{feed.title}</span>
                      <button
                        type="button"
                        onClick={() => onReactivateFeed(feed)}
                        className="ml-1 shrink-0 rounded border border-slate-300 px-1.5 py-0.5 text-[10px] font-medium text-slate-700 hover:bg-slate-200 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                      >
                        Re-subscribe
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}

function FeedRowButton({
  feed,
  unreadCount,
  selected,
  onSelect,
}: {
  feed: SyncFeedDTO;
  unreadCount: number;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={`sidebar-feed-${feed.id}`}
      onClick={onSelect}
      className={`flex w-full items-center justify-between gap-1 rounded-md px-2 py-1 text-left ${
        selected
          ? "bg-sky-100 font-medium text-sky-950 dark:bg-sky-900/50 dark:text-sky-100"
          : "text-slate-700 hover:bg-slate-200/70 dark:text-slate-300 dark:hover:bg-slate-800"
      }`}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${healthDotClass(feed.health_state)}`}
          title={`Health: ${feed.health_state}`}
        />
        <span className="truncate">{feed.title}</span>
        {feed.status === "paused" && (
          <span className="shrink-0 rounded bg-slate-200 px-1 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-400">
            Paused
          </span>
        )}
        {feed.status === "muted" && (
          <span className="shrink-0 rounded bg-slate-200 px-1 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-400">
            Muted
          </span>
        )}
      </span>
      {unreadCount > 0 && (
        <span className="shrink-0 text-[11px] font-semibold text-slate-500">{unreadCount}</span>
      )}
    </button>
  );
}
