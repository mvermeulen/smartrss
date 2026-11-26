import { useState, type ChangeEvent, type FormEvent } from "react";
import { ApiHttpError, type SmartRSSApiClient } from "../../api/client";
import type { FeedUpdateRequest, OpmlImportResponse } from "../../types/api";
import type { SyncCategoryDTO, SyncFeedDTO } from "../../types/sync";

export function FeedManagerView({
  isOnline,
  apiClient,
  categories,
  feeds,
  onSyncAfterOnlineWrite,
}: {
  isOnline: boolean;
  apiClient: SmartRSSApiClient | null;
  categories: readonly SyncCategoryDTO[];
  feeds: readonly SyncFeedDTO[];
  onSyncAfterOnlineWrite: () => Promise<void>;
}) {
  // Add feed state
  const [newFeedUrl, setNewFeedUrl] = useState("");
  const [newFeedTitle, setNewFeedTitle] = useState("");
  const [newFeedCatId, setNewFeedCatId] = useState("");
  const [addStatus, setAddStatus] = useState<string | null>(null);

  // Category state
  const [newCatName, setNewCatName] = useState("");
  const [catError, setCatError] = useState<string | null>(null);

  // OPML state
  const [opmlXml, setOpmlXml] = useState("");
  const [opmlDefaultCat, setOpmlDefaultCat] = useState("");
  const [opmlResult, setOpmlResult] = useState<OpmlImportResponse | null>(null);
  const [opmlError, setOpmlError] = useState<string | null>(null);

  // Edit feed modal + 409 curation_instructions conflict diff state (U-21 diff-only PATCH)
  const [editingFeed, setEditingFeed] = useState<SyncFeedDTO | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editUrl, setEditUrl] = useState("");
  const [editPoll, setEditPoll] = useState(60);
  const [editFullText, setEditFullText] = useState(true);
  const [editInstructions, setEditInstructions] = useState("");
  const [editError, setEditError] = useState<string | null>(null);
  const [conflictState, setConflictState] = useState<{
    serverVersion: number;
    serverInstructions: string;
    draftInstructions: string;
  } | null>(null);

  const openEdit = (feed: SyncFeedDTO) => {
    setEditingFeed(feed);
    setEditTitle(feed.title);
    setEditUrl(feed.feed_url);
    setEditPoll(feed.poll_interval_minutes);
    setEditFullText(feed.fetch_full_text);
    setEditInstructions(feed.curation_instructions);
    setEditError(null);
    setConflictState(null);
  };

  const handleAddFeed = async (e: FormEvent) => {
    e.preventDefault();
    if (!apiClient || !isOnline) return;
    setAddStatus(null);
    try {
      await apiClient.createFeed({
        feed_url: newFeedUrl.trim(),
        title: newFeedTitle.trim() || null,
        category_id: newFeedCatId || null,
      });
      setNewFeedUrl("");
      setNewFeedTitle("");
      setAddStatus("Feed added and queued for polling.");
      await onSyncAfterOnlineWrite();
    } catch (err) {
      setAddStatus(err instanceof Error ? err.message : String(err));
    }
  };

  const handleCreateCategory = async (e: FormEvent) => {
    e.preventDefault();
    if (!apiClient || !isOnline || !newCatName.trim()) return;
    setCatError(null);
    try {
      await apiClient.createCategory({ name: newCatName.trim(), sort_order: categories.length });
      setNewCatName("");
      await onSyncAfterOnlineWrite();
    } catch (err) {
      setCatError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleDeleteCategory = async (catId: string) => {
    if (!apiClient || !isOnline) return;
    setCatError(null);
    try {
      await apiClient.deleteCategory(catId);
      await onSyncAfterOnlineWrite();
    } catch (err) {
      setCatError(err instanceof Error ? err.message : String(err));
    }
  };

  /** Diff-only PATCH (U-21 / D3): sends ONLY fields actually changed by the user! */
  const submitEditPatch = async (overrideVersion?: number, overrideInstructions?: string) => {
    if (!apiClient || !isOnline || !editingFeed) return;
    setEditError(null);

    const patch: FeedUpdateRequest = {};
    if (editTitle.trim() !== editingFeed.title) patch.title = editTitle.trim();
    if (editUrl.trim() !== editingFeed.feed_url) patch.feed_url = editUrl.trim();
    if (editPoll !== editingFeed.poll_interval_minutes) patch.poll_interval_minutes = editPoll;
    if (editFullText !== editingFeed.fetch_full_text) patch.fetch_full_text = editFullText;

    const targetInstructions = overrideInstructions ?? editInstructions;
    if (targetInstructions !== editingFeed.curation_instructions) {
      patch.curation_instructions = targetInstructions;
      patch.expected_config_version = overrideVersion ?? editingFeed.config_version;
    }

    if (Object.keys(patch).length === 0) {
      setEditingFeed(null);
      return;
    }

    try {
      await apiClient.updateFeed(editingFeed.id, patch);
      setEditingFeed(null);
      setConflictState(null);
      await onSyncAfterOnlineWrite();
    } catch (err) {
      if (err instanceof ApiHttpError && err.status === 409) {
        const latest = await apiClient.getFeed(editingFeed.id);
        setConflictState({
          serverVersion: latest.config_version,
          serverInstructions: latest.curation_instructions,
          draftInstructions: targetInstructions,
        });
      } else {
        setEditError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  const handleOpmlFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    void file.text().then((txt) => setOpmlXml(txt));
  };

  const handleImportOpml = async (e: FormEvent) => {
    e.preventDefault();
    if (!apiClient || !isOnline || !opmlXml.trim()) return;
    setOpmlError(null);
    setOpmlResult(null);
    try {
      const res = await apiClient.importOpml({
        opml_xml: opmlXml,
        default_category_id: opmlDefaultCat || null,
      });
      setOpmlResult(res);
      await onSyncAfterOnlineWrite();
    } catch (err) {
      setOpmlError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleExportOpml = async () => {
    if (!apiClient || !isOnline) return;
    try {
      const xml = await apiClient.exportOpml();
      const blob = new Blob([xml], { type: "application/xml" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "smartrss-feeds.opml";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setOpmlError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div data-testid="feed-manager-view" className="flex-1 overflow-y-auto p-5">
      <div className="mx-auto max-w-5xl space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-lg font-bold">Feeds, Categories &amp; OPML</h1>
            <p className="text-xs text-slate-500">
              Online-only management (adding feeds, categories, OPML, and standing curation
              instructions require home LAN connectivity).
            </p>
          </div>
          <button
            type="button"
            data-testid="export-opml-btn"
            disabled={!isOnline}
            onClick={() => void handleExportOpml()}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            Export OPML
          </button>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          {/* Add Feed */}
          <form
            onSubmit={handleAddFeed}
            className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
          >
            <h2 className="text-sm font-semibold">Add Feed</h2>
            <div className="mt-3 space-y-2 text-xs">
              <input
                type="url"
                required
                disabled={!isOnline}
                value={newFeedUrl}
                onChange={(e) => setNewFeedUrl(e.target.value)}
                placeholder="https://example.com/feed.xml"
                className="w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              />
              <input
                type="text"
                disabled={!isOnline}
                value={newFeedTitle}
                onChange={(e) => setNewFeedTitle(e.target.value)}
                placeholder="Optional title override"
                className="w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              />
              <select
                disabled={!isOnline}
                value={newFeedCatId}
                onChange={(e) => setNewFeedCatId(e.target.value)}
                className="w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              >
                <option value="">Uncategorized</option>
                {categories.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <button
                type="submit"
                disabled={!isOnline}
                className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700 disabled:opacity-40"
              >
                Subscribe
              </button>
              {addStatus && <p className="text-slate-600 dark:text-slate-300">{addStatus}</p>}
            </div>
          </form>

          {/* Categories */}
          <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold">Categories</h2>
            <form onSubmit={handleCreateCategory} className="mt-3 flex gap-2 text-xs">
              <input
                type="text"
                disabled={!isOnline}
                value={newCatName}
                onChange={(e) => setNewCatName(e.target.value)}
                placeholder="New category name"
                className="flex-1 rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              />
              <button
                type="submit"
                disabled={!isOnline}
                className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700 disabled:opacity-40"
              >
                Create
              </button>
            </form>
            {catError && <p className="mt-1 text-xs text-rose-600">{catError}</p>}
            <ul className="mt-3 divide-y divide-slate-100 text-xs dark:divide-slate-800">
              {categories.map((c) => (
                <li key={c.id} className="flex items-center justify-between py-1.5">
                  <span>{c.name}</span>
                  <button
                    type="button"
                    disabled={!isOnline}
                    onClick={() => void handleDeleteCategory(c.id)}
                    className="text-rose-600 hover:underline disabled:opacity-40"
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* OPML Import */}
        <form
          onSubmit={handleImportOpml}
          className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900"
        >
          <h2 className="text-sm font-semibold">Import OPML</h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              type="file"
              accept=".opml,.xml,text/xml,application/xml"
              disabled={!isOnline}
              onChange={handleOpmlFile}
              className="text-xs"
            />
            <select
              disabled={!isOnline}
              value={opmlDefaultCat}
              onChange={(e) => setOpmlDefaultCat(e.target.value)}
              className="rounded border border-slate-300 bg-white px-2 py-1 dark:border-slate-700 dark:bg-slate-950"
            >
              <option value="">Default category: None</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <textarea
            data-testid="opml-xml-textarea"
            rows={4}
            disabled={!isOnline}
            value={opmlXml}
            onChange={(e) => setOpmlXml(e.target.value)}
            placeholder="Paste OPML XML here…"
            className="mt-2 w-full rounded border border-slate-300 bg-white p-2 font-mono text-xs dark:border-slate-700 dark:bg-slate-950"
          />
          <div className="mt-2 flex items-center gap-3">
            <button
              type="submit"
              data-testid="import-opml-btn"
              disabled={!isOnline || !opmlXml.trim()}
              className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700 disabled:opacity-40"
            >
              Import OPML
            </button>
            {opmlResult && (
              <span
                data-testid="opml-import-result"
                className="text-emerald-700 dark:text-emerald-400"
              >
                Imported {opmlResult.imported}, reactivated {opmlResult.reactivated}, skipped{" "}
                {opmlResult.skipped_duplicates}, rejected {opmlResult.rejected_invalid}.
              </span>
            )}
            {opmlError && <span className="text-rose-600">{opmlError}</span>}
          </div>
        </form>

        {/* Subscribed Feeds Table */}
        <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-sm font-semibold">Feeds ({feeds.length})</h2>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="py-1.5 pr-3">Title</th>
                  <th className="py-1.5 pr-3">Status</th>
                  <th className="py-1.5 pr-3">Health</th>
                  <th className="py-1.5 pr-3">Poll (min)</th>
                  <th className="py-1.5 pr-3">Version</th>
                  <th className="py-1.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {feeds.map((feed) => (
                  <tr key={feed.id}>
                    <td className="py-2 pr-3 font-medium">{feed.title}</td>
                    <td className="py-2 pr-3 capitalize">{feed.status}</td>
                    <td className="py-2 pr-3 capitalize">{feed.health_state}</td>
                    <td className="py-2 pr-3">{feed.poll_interval_minutes}</td>
                    <td className="py-2 pr-3 font-mono">v{feed.config_version}</td>
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        data-testid={`edit-feed-btn-${feed.id}`}
                        disabled={!isOnline}
                        onClick={() => openEdit(feed)}
                        className="rounded border border-slate-300 px-2 py-0.5 font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Edit Feed Modal + 409 Conflict Diff Dialog */}
      {editingFeed && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Edit feed"
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4"
        >
          <div className="w-full max-w-lg rounded-lg border border-slate-200 bg-white p-5 text-xs shadow-xl dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-base font-semibold">Edit Feed: {editingFeed.title}</h2>
            <div className="mt-3 space-y-2.5">
              <div>
                <label className="block font-medium">Title</label>
                <input
                  type="text"
                  value={editTitle}
                  onChange={(e) => setEditTitle(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
                />
              </div>
              <div>
                <label className="block font-medium">Feed URL</label>
                <input
                  type="url"
                  value={editUrl}
                  onChange={(e) => setEditUrl(e.target.value)}
                  className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 font-mono dark:border-slate-700 dark:bg-slate-950"
                />
              </div>
              <div className="flex gap-4">
                <div className="flex-1">
                  <label className="block font-medium">Poll Interval (minutes)</label>
                  <input
                    type="number"
                    min={5}
                    max={10080}
                    value={editPoll}
                    onChange={(e) => setEditPoll(Number(e.target.value))}
                    className="mt-1 w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-950"
                  />
                </div>
                <label className="flex items-end gap-2 pb-1.5 font-medium">
                  <input
                    type="checkbox"
                    checked={editFullText}
                    onChange={(e) => setEditFullText(e.target.checked)}
                  />
                  Fetch full text
                </label>
              </div>
              <div>
                <label className="block font-medium">
                  Standing Curation Instructions (v{editingFeed.config_version})
                </label>
                <textarea
                  data-testid="curation-instructions-input"
                  rows={4}
                  value={editInstructions}
                  onChange={(e) => setEditInstructions(e.target.value)}
                  placeholder="e.g. Prioritize deep technical postmortems; skip weekly sponsor roundups."
                  className="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-700 dark:bg-slate-950"
                />
              </div>

              {conflictState && (
                <div
                  data-testid="conflict-diff-dialog"
                  className="rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950/50"
                >
                  <p className="font-semibold text-amber-900 dark:text-amber-200">
                    Version Conflict (HTTP 409): server is now at v{conflictState.serverVersion}
                  </p>
                  <div className="mt-2 grid gap-2 md:grid-cols-2">
                    <div>
                      <div className="font-medium text-slate-600 dark:text-slate-300">
                        Server version
                      </div>
                      <pre className="mt-1 whitespace-pre-wrap rounded bg-white p-2 dark:bg-slate-900">
                        {conflictState.serverInstructions || "(empty)"}
                      </pre>
                    </div>
                    <div>
                      <div className="font-medium text-slate-600 dark:text-slate-300">
                        Your draft
                      </div>
                      <pre className="mt-1 whitespace-pre-wrap rounded bg-white p-2 dark:bg-slate-900">
                        {conflictState.draftInstructions || "(empty)"}
                      </pre>
                    </div>
                  </div>
                  <div className="mt-2 flex gap-2">
                    <button
                      type="button"
                      data-testid="conflict-overwrite-btn"
                      onClick={() =>
                        void submitEditPatch(
                          conflictState.serverVersion,
                          conflictState.draftInstructions,
                        )
                      }
                      className="rounded bg-amber-700 px-2.5 py-1 font-medium text-white"
                    >
                      Overwrite with my draft
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setEditInstructions(conflictState.serverInstructions);
                        setEditingFeed({
                          ...editingFeed,
                          config_version: conflictState.serverVersion,
                          curation_instructions: conflictState.serverInstructions,
                        });
                        setConflictState(null);
                      }}
                      className="rounded border border-amber-400 px-2.5 py-1 font-medium"
                    >
                      Reload server version
                    </button>
                  </div>
                </div>
              )}

              {editError && <p className="text-rose-600">{editError}</p>}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => {
                    setEditingFeed(null);
                    setConflictState(null);
                  }}
                  className="rounded border border-slate-300 px-3 py-1.5 font-medium dark:border-slate-700"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  data-testid="save-feed-edit-btn"
                  onClick={() => void submitEditPatch()}
                  className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white hover:bg-sky-700"
                >
                  Save Changes
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
