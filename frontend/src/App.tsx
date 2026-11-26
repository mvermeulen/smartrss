import { useLiveQuery } from "dexie-react-hooks";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiHttpError, SmartRSSApiClient } from "./api/client";
import { PairingScreen } from "./components/auth/PairingScreen";
import { ConfirmDialog } from "./components/common/ConfirmDialog";
import { ShortcutsModal } from "./components/common/ShortcutsModal";
import { FeedManagerView } from "./components/feeds/FeedManagerView";
import { SystemHealthView } from "./components/health/SystemHealthView";
import { AlertStrip } from "./components/layout/AlertStrip";
import { TopBar } from "./components/layout/TopBar";
import { ItemList, type DisplayListEntry } from "./components/reader/ItemList";
import { ReadingPane } from "./components/reader/ReadingPane";
import { Sidebar } from "./components/reader/Sidebar";
import { SettingsDevicesView } from "./components/settings/SettingsDevicesView";
import { DEFAULT_SYNC_META, DEFAULT_UI_PREFS, setUiPrefs } from "./db/clientMeta";
import {
  attachVersionChangeHandler,
  getDefaultDb,
  type AuthMeta,
  type SyncCursorMeta,
} from "./db/schema";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { useStoragePersistence } from "./hooks/useStoragePersistence";
import { LocalSearchIndex } from "./search/localSearch";
import { NegationOnlyQueryError } from "./search/queryParser";
import { safeExternalHref } from "./security/urlSafety";
import { registerServiceWorker } from "./sw/register";
import { SyncEngine, type SyncEngineSnapshot } from "./sync/engine";
import {
  dismissFailedMutation,
  enqueueBulkMarkRead,
  enqueueFeedField,
  enqueueItemStarred,
  enqueueItemState,
  manualQuarantineOutboxRow,
  retryFailedMutation,
} from "./sync/outbox";
import { applyOutboxToSearchItem } from "./sync/reconciliation";
import type { SearchResultDTO } from "./types/api";
import type {
  FeedMutableField,
  SyncAppSettingsDTO,
  SyncFeedDTO,
  SyncPushItemSnapshot,
} from "./types/sync";
import type { AppView, ReaderScope, SortDir, SortKey, ThemeMode, UiPrefs } from "./types/ui";

export function App() {
  const db = useMemo(() => getDefaultDb(), []);
  const searchIndexRef = useRef<LocalSearchIndex | null>(null);
  if (!searchIndexRef.current) {
    searchIndexRef.current = new LocalSearchIndex();
  }
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  const [activeView, setActiveView] = useState<AppView>("reader");
  const [sidebarOpenMobile, setSidebarOpenMobile] = useState(false);
  const [scope, setScope] = useState<ReaderScope>({ kind: "smart", smart: "unread" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("relevance");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [tierFilter, setTierFilter] = useState<number | null>(null);
  const [depthFilter, setDepthFilter] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [localSearchForced, setLocalSearchForced] = useState(false);
  const [archiveResults, setArchiveResults] = useState<SearchResultDTO[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [repairModalOpen, setRepairModalOpen] = useState(false);
  const [bulkConfirm, setBulkConfirm] = useState<{
    olderThanHours?: number;
    label: string;
  } | null>(null);

  const [swUpdateFn, setSwUpdateFn] = useState<(() => void) | null>(null);
  const [schemaUpgradeRequired, setSchemaUpgradeRequired] = useState(false);

  const storage = useStoragePersistence();

  // Reactive Dexie queries
  const authRow = useLiveQuery(() => db.client_meta.get("auth"), [db]);
  const syncRow = useLiveQuery(() => db.client_meta.get("sync"), [db]);
  const settingsRow = useLiveQuery(() => db.client_meta.get("app_settings"), [db]);
  const uiPrefsRow = useLiveQuery(() => db.client_meta.get("ui_prefs"), [db]);

  const authMeta = (authRow?.value as AuthMeta | null | undefined) ?? null;
  const syncMeta: SyncCursorMeta =
    (syncRow?.value as SyncCursorMeta | undefined) ?? DEFAULT_SYNC_META;
  const appSettings = (settingsRow?.value as SyncAppSettingsDTO | null | undefined) ?? null;
  const uiPrefs: UiPrefs = {
    ...DEFAULT_UI_PREFS,
    ...((uiPrefsRow?.value as Partial<UiPrefs> | undefined) ?? {}),
  };

  const rawCategories = useLiveQuery(() => db.categories.orderBy("sort_order").toArray(), [db]);
  const rawFeeds = useLiveQuery(() => db.feeds.orderBy("title").toArray(), [db]);
  const rawItems = useLiveQuery(() => db.items.toArray(), [db]);
  const rawOutboxRows = useLiveQuery(() => db.outbox.orderBy("local_seq").toArray(), [db]);
  const rawFailedRows = useLiveQuery(() => db.failed_mutations.toArray(), [db]);
  const journalCount = useLiveQuery(() => db.replay_journal.count(), [db]) ?? 0;
  const rawNotifications = useLiveQuery(
    () => db.sync_notifications.where("dismissed").equals(0).toArray(),
    [db],
  );

  const categories = useMemo(() => rawCategories ?? [], [rawCategories]);
  const feeds = useMemo(() => rawFeeds ?? [], [rawFeeds]);
  const items = useMemo(() => rawItems ?? [], [rawItems]);
  const outboxRows = useMemo(() => rawOutboxRows ?? [], [rawOutboxRows]);
  const failedRows = useMemo(() => rawFailedRows ?? [], [rawFailedRows]);
  const notifications = useMemo(() => rawNotifications ?? [], [rawNotifications]);

  // Apply archive-only mutation resolution to React search state (U-12, P-17)
  const handleArchiveResolved = useCallback(
    (
      itemId: string,
      outcome: "applied" | "superseded" | "rejected",
      snapshot: SyncPushItemSnapshot | null,
    ) => {
      if (outcome === "superseded" || outcome === "rejected") {
        if (snapshot) {
          setArchiveResults((prev) =>
            prev.map((r) =>
              r.item.id === itemId
                ? {
                    ...r,
                    item: {
                      ...r.item,
                      state: snapshot.state,
                      is_starred: snapshot.is_starred,
                      read_at: snapshot.read_at,
                      starred_at: snapshot.starred_at,
                      expired_at: snapshot.expired_at,
                      unexpired_at: snapshot.unexpired_at,
                      read_state_updated_at: snapshot.read_state_updated_at,
                      starred_updated_at: snapshot.starred_updated_at,
                    },
                  }
                : r,
            ),
          );
        }
      }
    },
    [],
  );

  // SyncEngine lifecycle
  const engineRef = useRef<SyncEngine | null>(null);
  if (!engineRef.current) {
    engineRef.current = new SyncEngine({
      db,
      onItemsMutated: (changedIds, deletedIds) => {
        const idx = searchIndexRef.current;
        if (!idx) return;
        for (const id of deletedIds) idx.discard(id);
        if (changedIds.length > 0) {
          void db.items.bulkGet(changedIds).then((rows) => {
            for (const row of rows) {
              if (row) idx.upsert(row);
            }
          });
        }
      },
      onFullReindex: async () => {
        const all = await db.items.toArray();
        searchIndexRef.current?.rebuild(all);
      },
      onArchiveMutationResolved: handleArchiveResolved,
    });
  }

  const engine = engineRef.current;
  const [engineSnap, setEngineSnap] = useState<SyncEngineSnapshot>(() => engine.getSnapshot());

  useEffect(() => {
    const unsub = engine.subscribe(setEngineSnap);
    attachVersionChangeHandler(db, () => setSchemaUpgradeRequired(true));
    void db.items.toArray().then((all) => searchIndexRef.current?.rebuild(all));
    void engine.start();
    void registerServiceWorker((applyFn) => setSwUpdateFn(() => applyFn));

    return () => {
      unsub();
      engine.stop();
    };
  }, [db, engine]);

  // Apply dark/light theme class on documentElement
  useEffect(() => {
    const root = document.documentElement;
    const prefersDark =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    const isDark = uiPrefs.theme === "dark" || (uiPrefs.theme === "system" && prefersDark);
    root.classList.toggle("dark", isDark);
  }, [uiPrefs.theme]);

  const apiClient = useMemo(() => {
    if (!authMeta) return null;
    return new SmartRSSApiClient(authMeta.api_origin, authMeta.bearer_token);
  }, [authMeta]);

  const isOnline =
    engineSnap.connectionState === "online" || engineSnap.connectionState === "syncing";

  const feedsById = useMemo(() => {
    const m = new Map<string, SyncFeedDTO>();
    for (const f of feeds) m.set(f.id, f);
    return m;
  }, [feeds]);

  const feedCategoryMap = useMemo(() => {
    const m = new Map<string, string | null>();
    for (const f of feeds) m.set(f.id, f.category_id);
    return m;
  }, [feeds]);

  const itemsById = useMemo(() => {
    const m = new Map<string, (typeof items)[number]>();
    for (const i of items) m.set(i.id, i);
    return m;
  }, [items]);

  const unsubscribedFeedIds = useMemo(
    () => new Set(feeds.filter((f) => f.status === "unsubscribed").map((f) => f.id)),
    [feeds],
  );

  // Live unread counts (excluding unsubscribed feeds per data-model.md §3.2)
  const { unreadCountByFeed, smartCounts, hasEnrichedItems } = useMemo(() => {
    const byFeed = new Map<string, number>();
    let unread = 0;
    let starred = 0;
    let all = 0;
    let enriched = false;
    for (const item of items) {
      if (item.enrichment) enriched = true;
      if (item.is_starred) starred += 1;
      const isUnsub = unsubscribedFeedIds.has(item.feed_id);
      if (!isUnsub) {
        all += 1;
        if (item.state === "unread") {
          unread += 1;
          byFeed.set(item.feed_id, (byFeed.get(item.feed_id) ?? 0) + 1);
        }
      }
    }
    return {
      unreadCountByFeed: byFeed,
      smartCounts: { unread, starred, all },
      hasEnrichedItems: enriched,
    };
  }, [items, unsubscribedFeedIds]);

  const isSearchActive = searchQuery.trim().length > 0;
  const isExpiredArchiveView = scope.kind === "smart" && scope.smart === "expired";

  // Execute Connected Archive Search (or Expired view) when online; Detached MiniSearch when offline
  useEffect(() => {
    if (!isSearchActive && !isExpiredArchiveView) {
      setArchiveResults([]);
      setSearchError(null);
      setSearchLoading(false);
      return;
    }

    const useLocal = !isOnline || localSearchForced;
    if (useLocal) {
      setSearchLoading(false);
      try {
        const searchIndex = searchIndexRef.current;
        if (!searchIndex) {
          setArchiveResults([]);
          return;
        }
        const res = searchIndex.search({
          q: isSearchActive ? searchQuery : "",
          state: isExpiredArchiveView ? "expired" : null,
          feed_id: scope.kind === "feed" ? scope.feedId : null,
          category_id: scope.kind === "category" ? scope.categoryId : null,
          feedCategoryMap,
          sort: isSearchActive ? "relevance" : "newest",
          limit: 100,
        });
        setArchiveResults(res.results);
        setSearchError(null);
      } catch (err) {
        setArchiveResults([]);
        setSearchError(
          err instanceof NegationOnlyQueryError
            ? err.message
            : err instanceof Error
              ? err.message
              : String(err),
        );
      }
      return;
    }

    if (!apiClient) return;
    let cancelled = false;
    setSearchLoading(true);
    setSearchError(null);
    const timer = window.setTimeout(() => {
      void apiClient
        .searchArchive({
          q: isSearchActive ? searchQuery : null,
          state: isExpiredArchiveView ? "expired" : null,
          feed_id: scope.kind === "feed" ? scope.feedId : null,
          category_id: scope.kind === "category" ? scope.categoryId : null,
          sort: isSearchActive ? "relevance" : "newest",
          limit: 50,
        })
        .then((res) => {
          if (!cancelled) {
            setArchiveResults(res.results);
            setSearchLoading(false);
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setArchiveResults([]);
            setSearchLoading(false);
            setSearchError(
              err instanceof ApiHttpError
                ? err.message
                : err instanceof Error
                  ? err.message
                  : String(err),
            );
          }
        });
    }, 180);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    apiClient,
    feedCategoryMap,
    isExpiredArchiveView,
    isOnline,
    isSearchActive,
    localSearchForced,
    scope,
    searchQuery,
    items, // re-run local search if local items mutate
  ]);

  // Build sorted, filtered item list entries
  const listEntries: DisplayListEntry[] = useMemo(() => {
    if (isSearchActive || isExpiredArchiveView) {
      return archiveResults.map((r) => {
        const localCopy = itemsById.get(r.item.id);
        if (localCopy) {
          return {
            id: localCopy.id,
            item: localCopy,
            isArchiveOnly: false,
            snippet: r.snippet,
          };
        }
        // P-17 + U-12: overlay pending outbox reactively without writing to Dexie
        return {
          id: r.item.id,
          item: applyOutboxToSearchItem(r.item, outboxRows),
          isArchiveOnly: true,
          snippet: r.snippet,
        };
      });
    }

    const filtered = items.filter((item) => {
      const isUnsub = unsubscribedFeedIds.has(item.feed_id);
      if (scope.kind === "smart") {
        if (scope.smart === "unread") {
          if (isUnsub || item.state !== "unread") return false;
        } else if (scope.smart === "starred") {
          if (!item.is_starred) return false;
        } else if (scope.smart === "all") {
          if (isUnsub) return false;
        }
      } else if (scope.kind === "category") {
        if (isUnsub || feedCategoryMap.get(item.feed_id) !== scope.categoryId) return false;
      } else if (scope.kind === "feed") {
        if (item.feed_id !== scope.feedId) return false;
      }

      if (tierFilter !== null && (item.enrichment?.relevance_tier ?? 0) < tierFilter) {
        return false;
      }
      if (depthFilter && item.enrichment?.depth_type !== depthFilter) {
        return false;
      }
      return true;
    });

    const dirMult = sortDir === "desc" ? -1 : 1;
    filtered.sort((a, b) => {
      if (sortKey === "relevance") {
        const ta = a.enrichment?.relevance_tier ?? -1;
        const tb = b.enrichment?.relevance_tier ?? -1;
        if (ta !== tb) return (ta - tb) * dirMult;
        if (a.effective_date !== b.effective_date) {
          return a.effective_date < b.effective_date ? 1 : -1;
        }
      } else if (sortKey === "date") {
        if (a.effective_date !== b.effective_date) {
          return (a.effective_date < b.effective_date ? -1 : 1) * dirMult;
        }
      } else if (sortKey === "feed") {
        const fa = (feedsById.get(a.feed_id)?.title ?? "").toLowerCase();
        const fb = (feedsById.get(b.feed_id)?.title ?? "").toLowerCase();
        if (fa !== fb) return fa.localeCompare(fb) * dirMult;
      } else if (sortKey === "depth") {
        const da = a.enrichment?.depth_type ?? "zzz";
        const dbStr = b.enrichment?.depth_type ?? "zzz";
        if (da !== dbStr) return da.localeCompare(dbStr) * dirMult;
      }
      return b.id.localeCompare(a.id);
    });

    return filtered.map((item) => ({
      id: item.id,
      item,
      isArchiveOnly: false,
    }));
  }, [
    archiveResults,
    depthFilter,
    feedCategoryMap,
    feedsById,
    isExpiredArchiveView,
    isSearchActive,
    items,
    itemsById,
    outboxRows,
    scope,
    sortDir,
    sortKey,
    tierFilter,
    unsubscribedFeedIds,
  ]);

  const selectedEntry = useMemo<DisplayListEntry | null>(() => {
    if (!selectedId) return null;
    const inList = listEntries.find((e) => e.id === selectedId);
    if (inList) return inList;
    const cached = itemsById.get(selectedId);
    if (cached) {
      return { id: cached.id, item: cached, isArchiveOnly: false };
    }
    return null;
  }, [itemsById, listEntries, selectedId]);

  // Mutations (all go through persistent outbox -> immediate optimistic update + debounced push)
  const toggleItemRead = useCallback(
    async (entry: DisplayListEntry | null) => {
      if (!entry) return;
      const nextState = entry.item.state === "unread" ? "read" : "unread";
      await enqueueItemState(db, entry.item.id, nextState, entry.item.title);
      const updated = await db.items.get(entry.item.id);
      if (updated) searchIndexRef.current?.upsert(updated);
      engineRef.current?.notifyOutboxEnqueued();
    },
    [db],
  );

  const toggleItemStar = useCallback(
    async (entry: DisplayListEntry | null) => {
      if (!entry) return;
      await enqueueItemStarred(db, entry.item.id, !entry.item.is_starred, entry.item.title);
      const updated = await db.items.get(entry.item.id);
      if (updated) searchIndexRef.current?.upsert(updated);
      engineRef.current?.notifyOutboxEnqueued();
    },
    [db],
  );

  const openSelectedExternal = useCallback(() => {
    if (!selectedEntry) return;
    const href =
      safeExternalHref(selectedEntry.item.canonical_url) ??
      safeExternalHref(selectedEntry.item.raw_url);
    if (href) {
      window.open(href, "_blank", "noopener,noreferrer");
    }
    if (selectedEntry.item.state === "unread") {
      void toggleItemRead(selectedEntry);
    }
  }, [selectedEntry, toggleItemRead]);

  const mutateFeedField = useCallback(
    async (feed: SyncFeedDTO, field: FeedMutableField, value: string | number | boolean | null) => {
      await enqueueFeedField(db, feed.id, field, value, feed.title);
      engineRef.current?.notifyOutboxEnqueued();
    },
    [db],
  );

  const executeConfirmedBulkRead = useCallback(async () => {
    if (!bulkConfirm) return;
    const olderThanTs = bulkConfirm.olderThanHours
      ? new Date(Date.now() - bulkConfirm.olderThanHours * 3_600_000).toISOString()
      : null;

    if (scope.kind === "feed" && scope.feedId) {
      await enqueueBulkMarkRead(db, {
        scope: "feed",
        feedId: scope.feedId,
        olderThanTs,
        displayLabel: bulkConfirm.label,
      });
    } else if (scope.kind === "category" && scope.categoryId) {
      await enqueueBulkMarkRead(db, {
        scope: "category",
        categoryId: scope.categoryId,
        olderThanTs,
        displayLabel: bulkConfirm.label,
      });
    } else {
      await enqueueBulkMarkRead(db, {
        scope: "all",
        olderThanTs,
        displayLabel: bulkConfirm.label,
      });
    }
    setBulkConfirm(null);
    const all = await db.items.toArray();
    searchIndexRef.current?.rebuild(all);
    engineRef.current?.notifyOutboxEnqueued();
  }, [bulkConfirm, db, scope]);

  const scopeLabel = useMemo(() => {
    if (isSearchActive) {
      return `Search: "${searchQuery.trim()}"`;
    }
    if (scope.kind === "feed" && scope.feedId) {
      return feedsById.get(scope.feedId)?.title ?? "Feed";
    }
    if (scope.kind === "category" && scope.categoryId) {
      return categories.find((c) => c.id === scope.categoryId)?.name ?? "Category";
    }
    switch (scope.smart) {
      case "starred":
        return "Starred";
      case "all":
        return "All Active";
      case "expired":
        return "Expired / Archive";
      default:
        return "Unread";
    }
  }, [categories, feedsById, isSearchActive, scope, searchQuery]);

  // Keyboard shortcuts (FR-UI-01)
  useKeyboardShortcuts(
    useMemo(
      () => ({
        onNext: () => {
          if (activeView !== "reader" || listEntries.length === 0) return;
          const idx = listEntries.findIndex((e) => e.id === selectedId);
          const next = listEntries[Math.min(listEntries.length - 1, idx + 1)];
          if (next) setSelectedId(next.id);
        },
        onPrev: () => {
          if (activeView !== "reader" || listEntries.length === 0) return;
          const idx = listEntries.findIndex((e) => e.id === selectedId);
          const prev = listEntries[Math.max(0, idx <= 0 ? 0 : idx - 1)];
          if (prev) setSelectedId(prev.id);
        },
        onToggleRead: () => {
          if (activeView === "reader") void toggleItemRead(selectedEntry);
        },
        onToggleStar: () => {
          if (activeView === "reader") void toggleItemStar(selectedEntry);
        },
        onOpenExternal: () => {
          if (activeView === "reader") openSelectedExternal();
        },
        onFocusSearch: () => {
          setActiveView("reader");
          searchInputRef.current?.focus();
        },
        onSync: () => {
          void engineRef.current?.triggerSync();
        },
        onBulkRead: () => {
          if (activeView === "reader" && syncMeta.sync_cursor !== null) {
            setBulkConfirm({ label: `Mark ${scopeLabel} read` });
          }
        },
        onToggleHelp: () => setShortcutsOpen((v) => !v),
        onEscape: () => {
          setShortcutsOpen(false);
          setBulkConfirm(null);
          searchInputRef.current?.blur();
        },
      }),
      [
        activeView,
        listEntries,
        openSelectedExternal,
        scopeLabel,
        selectedEntry,
        selectedId,
        syncMeta.sync_cursor,
        toggleItemRead,
        toggleItemStar,
      ],
    ),
    Boolean(authMeta) && !repairModalOpen,
  );

  // Pairing / Re-pair screen
  if (engineSnap.connectionState === "unpaired" || repairModalOpen) {
    return (
      <PairingScreen
        runtimeConfig={engineSnap.runtimeConfig}
        configLoaded={engineSnap.configLoaded}
        isRepair={repairModalOpen}
        bootstrapProgress={engineSnap.bootstrapProgress}
        onPaired={async (auth, isRepair) => {
          setRepairModalOpen(false);
          await engineRef.current?.completePairing(auth, isRepair);
        }}
        onSignOutAndClear={
          repairModalOpen
            ? async () => {
                setRepairModalOpen(false);
                await engineRef.current?.signOutAndClear();
              }
            : undefined
        }
      />
    );
  }

  const handleCycleTheme = () => {
    const order: ThemeMode[] = ["system", "light", "dark"];
    const next = order[(order.indexOf(uiPrefs.theme) + 1) % order.length] ?? "system";
    void setUiPrefs(db, { ...uiPrefs, theme: next });
  };

  return (
    <div className="flex h-full flex-col overflow-hidden bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <TopBar
        activeView={activeView}
        onSelectView={setActiveView}
        connectionState={engineSnap.connectionState}
        statusDetail={engineSnap.statusDetail}
        pendingOutboxCount={outboxRows.length}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        searchInputRef={searchInputRef}
        localSearchForced={localSearchForced}
        onToggleLocalSearchForced={() => setLocalSearchForced((v) => !v)}
        theme={uiPrefs.theme}
        onCycleTheme={handleCycleTheme}
        onManualSync={() => void engineRef.current?.triggerSync()}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        onToggleSidebar={() => setSidebarOpenMobile((v) => !v)}
      />

      <AlertStrip
        connectionState={engineSnap.connectionState}
        statusDetail={engineSnap.statusDetail}
        runtimeConfig={engineSnap.runtimeConfig}
        pendingOriginChange={engineSnap.pendingOriginChange}
        onConfirmOriginChange={() => void engineRef.current?.confirmOriginChange()}
        onSignOutAndClear={() => void engineRef.current?.signOutAndClear()}
        clockDriftMs={engineSnap.clockDriftMs}
        storageStatus={storage.status}
        swUpdateAvailable={swUpdateFn !== null}
        onApplySwUpdate={() => swUpdateFn?.()}
        schemaUpgradeRequired={schemaUpgradeRequired}
        cspReloadRequired={engineSnap.cspReloadRequired}
        notifications={notifications}
        onDismissNotification={(id) => void db.sync_notifications.update(id, { dismissed: 1 })}
        onOpenRepair={() => setRepairModalOpen(true)}
      />

      {activeView === "reader" && (
        <div className="flex min-h-0 flex-1 overflow-hidden">
          <Sidebar
            openMobile={sidebarOpenMobile}
            scope={scope}
            onSelectScope={(next) => {
              setScope(next);
              setSearchQuery("");
              setSidebarOpenMobile(false);
            }}
            categories={categories}
            feeds={feeds}
            unreadCountByFeed={unreadCountByFeed}
            smartCounts={smartCounts}
            isOnline={isOnline}
            onOpenAddFeed={() => setActiveView("feeds")}
            onReactivateFeed={(f) => void mutateFeedField(f, "status", "active")}
          />

          <ItemList
            scopeLabel={scopeLabel}
            entries={listEntries}
            feedsById={feedsById}
            selectedId={selectedId}
            onSelect={(entry) => {
              setSelectedId(entry.id);
              if (uiPrefs.auto_mark_read_on_select && entry.item.state === "unread") {
                void toggleItemRead(entry);
              }
            }}
            onToggleStar={(entry) => void toggleItemStar(entry)}
            sortKey={sortKey}
            sortDir={sortDir}
            onToggleSort={(key) => {
              if (key === sortKey) {
                setSortDir((d) => (d === "desc" ? "asc" : "desc"));
              } else {
                setSortKey(key);
                setSortDir(key === "feed" || key === "depth" ? "asc" : "desc");
              }
            }}
            isSearchMode={isSearchActive || isExpiredArchiveView}
            searchError={searchError}
            searchLoading={searchLoading}
            canBulkRead={syncMeta.sync_cursor !== null && syncMeta.instance_epoch !== null}
            onTriggerBulkRead={(olderThanHours) =>
              setBulkConfirm({
                olderThanHours,
                label: olderThanHours
                  ? `Mark ${scopeLabel} older than ${olderThanHours}h read`
                  : `Mark ${scopeLabel} read`,
              })
            }
            tierFilter={tierFilter}
            onTierFilterChange={setTierFilter}
            depthFilter={depthFilter}
            onDepthFilterChange={setDepthFilter}
            hasEnrichedItems={hasEnrichedItems}
          />

          <ReadingPane
            item={selectedEntry?.item ?? null}
            isArchiveOnly={selectedEntry?.isArchiveOnly ?? false}
            feed={selectedEntry ? feedsById.get(selectedEntry.item.feed_id) : undefined}
            categories={categories}
            isOnline={isOnline}
            apiClient={apiClient}
            onToggleRead={() => void toggleItemRead(selectedEntry)}
            onToggleStar={() => void toggleItemStar(selectedEntry)}
            onOpenExternal={() => {
              if (selectedEntry && selectedEntry.item.state === "unread") {
                void toggleItemRead(selectedEntry);
              }
            }}
            onMutateFeedField={(f, field, val) => void mutateFeedField(f, field, val)}
          />
        </div>
      )}

      {activeView === "feeds" && (
        <FeedManagerView
          isOnline={isOnline}
          apiClient={apiClient}
          categories={categories}
          feeds={feeds}
          onSyncAfterOnlineWrite={() => engineRef.current?.triggerSync() ?? Promise.resolve()}
        />
      )}

      {activeView === "health" && (
        <SystemHealthView
          isOnline={isOnline}
          apiClient={apiClient}
          syncMeta={syncMeta}
          storageStatus={storage.status}
          storageEstimate={storage.estimate}
          localCounts={{
            items: items.length,
            outbox: outboxRows.length,
            journal: journalCount,
            quarantined: failedRows.length,
          }}
        />
      )}

      {activeView === "settings" && (
        <SettingsDevicesView
          isOnline={isOnline}
          apiClient={apiClient}
          authMeta={authMeta}
          appSettings={appSettings}
          outboxRows={outboxRows}
          failedRows={failedRows}
          onSyncNow={() => void engineRef.current?.triggerSync()}
          onForceFullResync={() => engineRef.current?.forceFullResync() ?? Promise.resolve()}
          onSignOutAndClear={() => engineRef.current?.signOutAndClear() ?? Promise.resolve()}
          onManualQuarantine={async (seq) => {
            await manualQuarantineOutboxRow(db, seq);
            const all = await db.items.toArray();
            searchIndexRef.current?.rebuild(all);
          }}
          onRetryFailed={async (id) => {
            await retryFailedMutation(db, id);
            engineRef.current?.notifyOutboxEnqueued();
          }}
          onDismissFailed={async (id) => {
            await dismissFailedMutation(db, id);
          }}
          onSettingsUpdated={() => engineRef.current?.triggerSync() ?? Promise.resolve()}
        />
      )}

      <ShortcutsModal open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <ConfirmDialog
        open={bulkConfirm !== null}
        title={bulkConfirm?.label ?? "Mark Read"}
        message={`Mark unread items in "${scopeLabel}" up to sync sequence #${syncMeta.sync_cursor ?? 0} as read? Any newer unseen items on the server will stay unread.`}
        confirmLabel="Mark Read"
        onConfirm={() => void executeConfirmedBulkRead()}
        onCancel={() => setBulkConfirm(null)}
      />
    </div>
  );
}
