import { useEffect, useState } from "react";
import type { SmartRSSApiClient } from "../../api/client";
import type { LocalItemRecord } from "../../db/schema";
import { safeExternalHref } from "../../security/urlSafety";
import type { ItemResponse, SearchItemDTO } from "../../types/api";
import type { FeedMutableField, FeedStatus, SyncCategoryDTO, SyncFeedDTO } from "../../types/sync";
import { SafeHtml } from "./SafeHtml";

export function ReadingPane({
  item,
  isArchiveOnly,
  feed,
  categories,
  isOnline,
  apiClient,
  onToggleRead,
  onToggleStar,
  onOpenExternal,
  onMutateFeedField,
}: {
  item: LocalItemRecord | SearchItemDTO | null;
  isArchiveOnly: boolean;
  feed: SyncFeedDTO | undefined;
  categories: readonly SyncCategoryDTO[];
  isOnline: boolean;
  apiClient: SmartRSSApiClient | null;
  onToggleRead: () => void;
  onToggleStar: () => void;
  onOpenExternal: () => void;
  onMutateFeedField: (
    feed: SyncFeedDTO,
    field: FeedMutableField,
    value: string | number | boolean | null,
  ) => void;
}) {
  const [bodyTab, setBodyTab] = useState<"summary" | "excerpt" | "extracted">("summary");
  const [fullItem, setFullItem] = useState<ItemResponse | null>(null);
  const [loadingFull, setLoadingFull] = useState(false);
  const [fullError, setFullError] = useState<string | null>(null);
  const [feedMenuOpen, setFeedMenuOpen] = useState(false);

  useEffect(() => {
    setBodyTab("summary");
    setFullItem(null);
    setFullError(null);
    setFeedMenuOpen(false);
  }, [item?.id]);

  if (!item) {
    return (
      <main
        data-testid="reading-pane-empty"
        className="flex flex-1 items-center justify-center bg-slate-50 p-6 text-sm text-slate-500 dark:bg-slate-950"
      >
        Select an item to read (use <code className="mx-1 font-mono">j</code>/
        <code className="mx-1 font-mono">k</code> to navigate,{" "}
        <code className="mx-1 font-mono">?</code> for shortcuts).
      </main>
    );
  }

  const externalHref = safeExternalHref(item.canonical_url) ?? safeExternalHref(item.raw_url);
  const localRawSummary =
    "raw_summary" in item ? item.raw_summary : (fullItem?.raw_summary ?? null);
  const summaryTruncated = "summary_truncated" in item ? item.summary_truncated : false;
  const extractedText = fullItem?.extracted_text ?? null;

  const handleLoadFull = async () => {
    if (!apiClient || !isOnline) return;
    setLoadingFull(true);
    setFullError(null);
    try {
      const res = await apiClient.getItem(item.id);
      setFullItem(res);
      if (res.extracted_text) {
        setBodyTab("extracted");
      }
    } catch (err) {
      setFullError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingFull(false);
    }
  };

  return (
    <main
      data-testid="reading-pane"
      className="flex flex-1 flex-col overflow-y-auto bg-white p-5 dark:bg-slate-950"
    >
      <div className="mx-auto w-full max-w-3xl">
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
          <span className="font-semibold text-slate-700 dark:text-slate-300">
            {feed?.title ?? "Unknown feed"}
          </span>
          {item.author && <span>• {item.author}</span>}
          <span>• {new Date(item.published_at ?? item.fetched_at).toLocaleString()}</span>
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] uppercase dark:bg-slate-800">
            {item.state}
          </span>
          {isArchiveOnly && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
              Archive result (display-only)
            </span>
          )}
        </div>

        <h1 className="mt-2 text-xl font-bold leading-snug tracking-tight">
          {externalHref ? (
            <a
              href={externalHref}
              target="_blank"
              rel="noopener noreferrer"
              onClick={onOpenExternal}
              className="hover:text-sky-600 hover:underline dark:hover:text-sky-400"
            >
              {item.title}
            </a>
          ) : (
            item.title
          )}
        </h1>

        {/* Action bar */}
        <div className="mt-3 flex flex-wrap items-center gap-2 border-y border-slate-200 py-2 text-xs dark:border-slate-800">
          {externalHref && (
            <a
              href={externalHref}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="open-external-btn"
              onClick={onOpenExternal}
              className="rounded-md bg-sky-600 px-2.5 py-1 font-medium text-white hover:bg-sky-700"
            >
              Open in Browser (o)
            </a>
          )}
          <button
            type="button"
            data-testid="toggle-read-btn"
            onClick={onToggleRead}
            className="rounded-md border border-slate-300 px-2.5 py-1 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            {item.state === "unread" ? "Mark Read (m)" : "Mark Unread (m)"}
          </button>
          <button
            type="button"
            data-testid="toggle-star-btn"
            onClick={onToggleStar}
            className="rounded-md border border-slate-300 px-2.5 py-1 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            {item.is_starred ? "★ Starred (s)" : "☆ Star (s)"}
          </button>

          {feed && (
            <div className="relative ml-auto">
              <button
                type="button"
                data-testid="feed-quick-actions-btn"
                onClick={() => setFeedMenuOpen((v) => !v)}
                className="rounded-md border border-slate-300 px-2.5 py-1 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              >
                Feed Actions ▾
              </button>
              {feedMenuOpen && (
                <div className="absolute right-0 z-20 mt-1 w-64 space-y-2 rounded-md border border-slate-200 bg-white p-3 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-900">
                  <div className="font-semibold text-slate-700 dark:text-slate-200">
                    Offline-Capable Feed Controls
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {(["active", "paused", "muted", "unsubscribed"] as FeedStatus[]).map((st) => (
                      <button
                        key={st}
                        type="button"
                        data-testid={`quick-feed-status-${st}`}
                        onClick={() => onMutateFeedField(feed, "status", st)}
                        className={`rounded px-2 py-0.5 capitalize ${
                          feed.status === st
                            ? "bg-sky-600 text-white"
                            : "border border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                        }`}
                      >
                        {st}
                      </button>
                    ))}
                  </div>
                  <div>
                    <label className="block text-[11px] text-slate-500">Category</label>
                    <select
                      data-testid="quick-feed-category-select"
                      value={feed.category_id ?? ""}
                      onChange={(e) =>
                        onMutateFeedField(feed, "category_id", e.target.value || null)
                      }
                      className="mt-0.5 w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-950"
                    >
                      <option value="">Uncategorized</option>
                      {categories.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Mini-research enabled</span>
                    <input
                      type="checkbox"
                      checked={Boolean(feed.research_policy.enabled)}
                      onChange={(e) =>
                        onMutateFeedField(feed, "research_policy.enabled", e.target.checked)
                      }
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Fetch PDFs for feed</span>
                    <input
                      type="checkbox"
                      checked={Boolean(feed.research_policy.fetch_pdfs)}
                      onChange={(e) =>
                        onMutateFeedField(feed, "research_policy.fetch_pdfs", e.target.checked)
                      }
                    />
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Forward-compatible 1d–1f Smart Metadata Card */}
        {item.enrichment ? (
          <div className="mt-4 rounded-lg border border-sky-200 bg-sky-50/70 p-4 text-xs dark:border-sky-900 dark:bg-sky-950/30">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold text-sky-900 dark:text-sky-200">
                Relevance Tier {item.enrichment.relevance_tier ?? "—"}{" "}
                {item.enrichment.depth_type ? `• ${item.enrichment.depth_type}` : ""}
              </span>
              {item.enrichment.model_name && (
                <span className="font-mono text-[11px] text-slate-500">
                  {item.enrichment.model_name}
                </span>
              )}
            </div>
            {item.enrichment.relevance_rationale && (
              <p className="mt-1 text-slate-700 dark:text-slate-300">
                <strong>Why:</strong> {item.enrichment.relevance_rationale}
              </p>
            )}
            {item.enrichment.tldr && (
              <p className="mt-2 text-sm text-slate-900 dark:text-slate-100">
                {item.enrichment.tldr}
              </p>
            )}
            {item.enrichment.takeaways && item.enrichment.takeaways.length > 0 && (
              <ul className="mt-2 list-disc space-y-1 pl-4 text-slate-700 dark:text-slate-300">
                {item.enrichment.takeaways.map((t, i) => (
                  <li key={i}>{t}</li>
                ))}
              </ul>
            )}
            {item.enrichment.research_context && (
              <p className="mt-2 border-t border-sky-200 pt-2 text-slate-700 dark:border-sky-900 dark:text-slate-300">
                <strong>Primary Source Synthesis:</strong> {item.enrichment.research_context}
              </p>
            )}
            {item.research_sources && item.research_sources.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-2">
                {item.research_sources.map((src, i) => {
                  const href = safeExternalHref(src.source_url);
                  return href ? (
                    <a
                      key={i}
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="rounded border border-sky-300 px-2 py-0.5 text-[11px] font-medium text-sky-800 hover:bg-sky-100 dark:border-sky-800 dark:text-sky-300"
                    >
                      [{src.source_type}] {src.title || src.source_url}
                    </a>
                  ) : null;
                })}
              </div>
            )}
          </div>
        ) : (
          <p className="mt-3 text-xs italic text-slate-400">AI enrichment pending</p>
        )}

        {/* Content body view selector */}
        <div className="mt-4 flex flex-wrap items-center gap-2 text-xs">
          <button
            type="button"
            onClick={() => setBodyTab("summary")}
            className={`rounded px-2 py-1 font-medium ${
              bodyTab === "summary"
                ? "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
                : "text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
            }`}
          >
            Feed Summary
          </button>
          <button
            type="button"
            onClick={() => setBodyTab("excerpt")}
            className={`rounded px-2 py-1 font-medium ${
              bodyTab === "excerpt"
                ? "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
                : "text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
            }`}
          >
            Plain Excerpt
          </button>
          <button
            type="button"
            data-testid="load-extracted-btn"
            disabled={!isOnline || loadingFull}
            title={isOnline ? "Fetch full extracted plain text from server" : "Requires home LAN"}
            onClick={() => void handleLoadFull()}
            className="rounded border border-slate-300 px-2 py-1 font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            {loadingFull ? "Loading extracted text…" : "Load full extracted text"}
          </button>
        </div>

        {fullError && <p className="mt-2 text-xs text-rose-600 dark:text-rose-400">{fullError}</p>}

        <div className="mt-4">
          {bodyTab === "summary" && (
            <>
              {localRawSummary ? (
                <SafeHtml html={localRawSummary} baseUrl={item.canonical_url} />
              ) : (
                <p className="text-sm text-slate-600 dark:text-slate-300">
                  {item.lead_excerpt || "No summary text available."}
                </p>
              )}
              {summaryTruncated && (
                <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                  Summary truncated — open the original article to read in full.
                </p>
              )}
            </>
          )}

          {bodyTab === "excerpt" && (
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700 dark:text-slate-300">
              {item.lead_excerpt || "No plain excerpt available."}
            </p>
          )}

          {bodyTab === "extracted" && (
            <pre
              data-testid="extracted-plain-text"
              className="whitespace-pre-wrap font-sans text-sm leading-relaxed text-slate-800 dark:text-slate-200"
            >
              {extractedText || "Server has no extracted text for this item."}
            </pre>
          )}
        </div>
      </div>
    </main>
  );
}
