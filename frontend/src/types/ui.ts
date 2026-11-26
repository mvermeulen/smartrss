export type ConnectionState =
  | "unpaired"
  | "origin_confirm"
  | "bootstrapping"
  | "online"
  | "syncing"
  | "detached"
  | "misconfigured"
  | "sync_error"
  | "auth_revoked";

export type AppView = "reader" | "feeds" | "health" | "settings";

export type StateFilter = "unread" | "starred" | "all" | "expired";

export type SortKey = "relevance" | "date" | "feed" | "depth";
export type SortDir = "asc" | "desc";

export type ThemeMode = "light" | "dark" | "system";

export interface UiPrefs {
  theme: ThemeMode;
  default_sort: SortKey;
  density: "compact" | "comfortable";
  auto_mark_read_on_select: boolean;
}

export interface ReaderScope {
  kind: "smart" | "category" | "feed";
  smart?: StateFilter;
  categoryId?: string;
  feedId?: string;
}

export type StoragePersistenceStatus = "unknown" | "granted" | "denied" | "unsupported";
