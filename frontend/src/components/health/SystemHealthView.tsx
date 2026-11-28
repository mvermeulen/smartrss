import { useCallback, useEffect, useState } from "react";
import { ApiHttpError, type SmartRSSApiClient } from "../../api/client";
import type { StorageEstimateInfo } from "../../db/clientMeta";
import type { SyncCursorMeta } from "../../db/schema";
import type {
  DeadLetterListResponse,
  FeedResponse,
  MetricsWindow,
  SystemHealthResponse,
  SystemMetricsResponse,
} from "../../types/api";
import type { StoragePersistenceStatus } from "../../types/ui";

function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function SystemHealthView({
  isOnline,
  apiClient,
  syncMeta,
  storageStatus,
  storageEstimate,
  localCounts,
}: {
  isOnline: boolean;
  apiClient: SmartRSSApiClient | null;
  syncMeta: SyncCursorMeta;
  storageStatus: StoragePersistenceStatus;
  storageEstimate: StorageEstimateInfo;
  localCounts: { items: number; outbox: number; journal: number; quarantined: number };
}) {
  const [windowVal, setWindowVal] = useState<MetricsWindow>("24h");
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [metrics, setMetrics] = useState<SystemMetricsResponse | null>(null);
  const [feedHealth, setFeedHealth] = useState<FeedResponse[]>([]);
  const [deadLetters, setDeadLetters] = useState<DeadLetterListResponse | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const loadAll = useCallback(async () => {
    if (!apiClient || !isOnline) return;
    setLoading(true);
    try {
      const [h, m, fh, dl] = await Promise.all([
        apiClient.getSystemHealth(),
        apiClient.getSystemMetrics(windowVal),
        apiClient.getFeedsHealth(),
        apiClient.listDeadLetterJobs(25, 0),
      ]);
      setHealth(h);
      setMetrics(m);
      setFeedHealth(fh);
      setDeadLetters(dl);
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [apiClient, isOnline, windowVal]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const handleRetryJob = async (jobId: number) => {
    if (!apiClient || !isOnline) return;
    setActionMsg(null);
    try {
      await apiClient.retryDeadLetterJob(jobId);
      setActionMsg(`Re-queued dead-letter job #${jobId}.`);
      await loadAll();
    } catch (err) {
      if (err instanceof ApiHttpError && err.status === 409) {
        setActionMsg(`Job #${jobId} already has an active queued/running job.`);
      } else {
        setActionMsg(err instanceof Error ? err.message : String(err));
      }
    }
  };

  const handleDismissJob = async (jobId: number) => {
    if (!apiClient || !isOnline) return;
    setActionMsg(null);
    try {
      await apiClient.dismissDeadLetterJob(jobId);
      setActionMsg(`Dismissed dead-letter job #${jobId}.`);
      await loadAll();
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    }
  };

  const handlePollNow = async (feedId: string) => {
    if (!apiClient || !isOnline) return;
    setActionMsg(null);
    try {
      const res = await apiClient.triggerFeedPoll(feedId);
      setActionMsg(
        res.queued
          ? `Poll job #${res.job_id} queued.`
          : "Poll already queued or running for this feed.",
      );
      await loadAll();
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    }
  };

  const maxStageP95 = Math.max(1, ...(metrics?.stages.map((s) => s.p95_duration_ms) ?? [1]));

  return (
    <div data-testid="system-health-view" className="flex-1 overflow-y-auto p-5">
      <div className="mx-auto max-w-6xl space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-lg font-bold">System Health &amp; Pipeline Performance Metrics</h1>
            <p className="text-xs text-slate-500">
              Monitor PostgreSQL health, job queue depth, stage latency distributions, per-feed
              processing cost, feed health, and dead-letter jobs.
            </p>
          </div>
          <button
            type="button"
            disabled={!isOnline || loading}
            onClick={() => void loadAll()}
            className="rounded border border-slate-300 px-3 py-1.5 text-xs font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>

        {!isOnline && (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-100">
            Online server metrics are unavailable while detached. Local client storage and outbox
            diagnostics are shown below.
          </div>
        )}

        {actionMsg && (
          <div
            role="status"
            data-testid="health-action-status"
            className="rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-900 dark:border-sky-900 dark:bg-sky-950/50 dark:text-sky-200"
          >
            {actionMsg}
          </div>
        )}

        {/* Overview Cards */}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-xs">
          <div className="rounded-lg border border-slate-200 bg-white p-3.5 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-slate-500">Server DB &amp; Sync</div>
            <div className="mt-1 text-base font-bold">
              {health ? (health.db === "ok" ? "DB OK" : "DB Error") : "Detached"}
            </div>
            <div className="mt-1 font-mono text-[11px] text-slate-500">
              Server seq: {health?.current_seq ?? "—"} / Local: {syncMeta.sync_cursor ?? "—"}
            </div>
            <div className="truncate font-mono text-[11px] text-slate-400">
              Epoch: {(health?.instance_epoch ?? syncMeta.instance_epoch ?? "—").slice(0, 8)}
            </div>
          </div>

          <div className="rounded-lg border border-slate-200 bg-white p-3.5 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-slate-500">Dead-Letter Jobs</div>
            <div className="mt-1 text-base font-bold" data-testid="dead-letter-count">
              {health?.dead_letter_count ?? "—"}
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              Failed jobs awaiting retry or dismiss
            </div>
          </div>

          <div className="rounded-lg border border-slate-200 bg-white p-3.5 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-slate-500">Local Client Storage</div>
            <div className="mt-1 text-base font-bold capitalize">{storageStatus}</div>
            <div className="mt-1 text-[11px] text-slate-500">
              Used {formatBytes(storageEstimate.usageBytes)} /{" "}
              {formatBytes(storageEstimate.quotaBytes)}
            </div>
          </div>

          <div className="rounded-lg border border-slate-200 bg-white p-3.5 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-slate-500">Local Cache &amp; Outbox</div>
            <div className="mt-1 text-base font-bold">{localCounts.items} cached items</div>
            <div className="mt-1 text-[11px] text-slate-500">
              Outbox: {localCounts.outbox} • Journal: {localCounts.journal} • Quarantined:{" "}
              {localCounts.quarantined}
            </div>
          </div>
        </div>

        {/* Job Queue Matrix */}
        {health && (
          <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold">Job Queue Depth</h2>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-left">
                <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="py-1.5 pr-4">Job Type</th>
                    <th className="py-1.5 pr-4">Queued</th>
                    <th className="py-1.5 pr-4">Running</th>
                    <th className="py-1.5 pr-4">Done</th>
                    <th className="py-1.5">Failed</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {Object.entries(health.queue_counts).length === 0 ? (
                    <tr>
                      <td colSpan={5} className="py-2 text-slate-500">
                        No jobs recorded yet.
                      </td>
                    </tr>
                  ) : (
                    Object.entries(health.queue_counts).map(([jt, counts]) => (
                      <tr key={jt}>
                        <td className="py-1.5 pr-4 font-mono">{jt}</td>
                        <td className="py-1.5 pr-4">{counts.queued ?? 0}</td>
                        <td className="py-1.5 pr-4">{counts.running ?? 0}</td>
                        <td className="py-1.5 pr-4">{counts.done ?? 0}</td>
                        <td className="py-1.5">{counts.failed ?? 0}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Pipeline Performance Metrics */}
        {metrics && (
          <div className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">Pipeline Stage Latency &amp; Per-Feed Cost</h2>
              <div className="flex gap-1">
                {(["24h", "7d", "30d"] as MetricsWindow[]).map((w) => (
                  <button
                    key={w}
                    type="button"
                    data-testid={`metrics-window-${w}`}
                    onClick={() => setWindowVal(w)}
                    className={`rounded px-2.5 py-1 font-medium ${
                      windowVal === w
                        ? "bg-sky-600 text-white"
                        : "border border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    }`}
                  >
                    {w}
                  </button>
                ))}
              </div>
            </div>

            <div className="overflow-x-auto">
              <table data-testid="stage-metrics-table" className="w-full text-left">
                <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="py-1.5 pr-3">Stage</th>
                    <th className="py-1.5 pr-3">Total</th>
                    <th className="py-1.5 pr-3">OK (304) / Fail</th>
                    <th className="py-1.5 pr-3">Fail %</th>
                    <th className="py-1.5 pr-3">Avg (ms)</th>
                    <th className="py-1.5 pr-3">p50 (ms)</th>
                    <th className="py-1.5 pr-3">p95 (ms)</th>
                    <th className="py-1.5 pr-3">Max (ms)</th>
                    <th className="py-1.5">Queue Wait (ms)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {metrics.stages.length === 0 ? (
                    <tr>
                      <td colSpan={9} className="py-2 text-slate-500">
                        No stage metrics in this window.
                      </td>
                    </tr>
                  ) : (
                    metrics.stages.map((s) => (
                      <tr key={s.job_type}>
                        <td className="py-1.5 pr-3 font-mono font-medium">{s.job_type}</td>
                        <td className="py-1.5 pr-3">{s.total_count}</td>
                        <td className="py-1.5 pr-3">
                          {s.ok_count}
                          {(s.not_modified_count ?? 0) > 0
                            ? ` (${s.not_modified_count} 304)`
                            : ""}{" "}
                          / {s.failed_count}
                        </td>
                        <td className="py-1.5 pr-3">{(s.failure_rate * 100).toFixed(1)}%</td>
                        <td className="py-1.5 pr-3">{s.avg_duration_ms.toFixed(1)}</td>
                        <td className="py-1.5 pr-3">{s.p50_duration_ms.toFixed(1)}</td>
                        <td className="py-1.5 pr-3">
                          <div className="flex items-center gap-2">
                            <span>{s.p95_duration_ms.toFixed(1)}</span>
                            <progress
                              value={s.p95_duration_ms}
                              max={maxStageP95}
                              className="h-1.5 w-16"
                            />
                          </div>
                        </td>
                        <td className="py-1.5 pr-3">{s.max_duration_ms}</td>
                        <td className="py-1.5">{s.avg_queue_wait_ms.toFixed(1)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <div>
              <h3 className="mb-1.5 font-semibold">Per-Feed Processing Cost Rollup</h3>
              <div className="overflow-x-auto">
                <table data-testid="feed-cost-table" className="w-full text-left">
                  <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                    <tr>
                      <th className="py-1.5 pr-3">Feed</th>
                      <th className="py-1.5 pr-3">Jobs</th>
                      <th className="py-1.5 pr-3">Failed</th>
                      <th className="py-1.5 pr-3">Total (ms)</th>
                      <th className="py-1.5">Avg (ms)</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                    {metrics.per_feed.length === 0 ? (
                      <tr>
                        <td colSpan={5} className="py-2 text-slate-500">
                          No per-feed metrics recorded in this window.
                        </td>
                      </tr>
                    ) : (
                      metrics.per_feed.map((f) => (
                        <tr key={f.feed_id}>
                          <td className="py-1.5 pr-3 font-medium">{f.feed_title ?? f.feed_id}</td>
                          <td className="py-1.5 pr-3">{f.total_jobs}</td>
                          <td className="py-1.5 pr-3">{f.failed_jobs}</td>
                          <td className="py-1.5 pr-3">{f.total_duration_ms}</td>
                          <td className="py-1.5">{f.avg_duration_ms.toFixed(1)}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* Recent Pipeline Runs */}
        {health && (
          <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold">Recent Pipeline / Cron Runs</h2>
            <div className="mt-2 overflow-x-auto">
              <table data-testid="pipeline-runs-table" className="w-full text-left">
                <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="py-1.5 pr-3">Trigger</th>
                    <th className="py-1.5 pr-3">Stages</th>
                    <th className="py-1.5 pr-3">Status</th>
                    <th className="py-1.5 pr-3">Started</th>
                    <th className="py-1.5 pr-3">Duration (ms)</th>
                    <th className="py-1.5 pr-3">Feeds (304)</th>
                    <th className="py-1.5 pr-3">Ingested / Extracted / Enriched</th>
                    <th className="py-1.5">Failed</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {health.recent_runs.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="py-2 text-slate-500">
                        No pipeline runs recorded yet.
                      </td>
                    </tr>
                  ) : (
                    health.recent_runs.map((run) => (
                      <tr key={run.id}>
                        <td className="py-1.5 pr-3 font-mono">{run.trigger}</td>
                        <td className="py-1.5 pr-3 font-mono">{run.stages.join(", ")}</td>
                        <td className="py-1.5 pr-3 capitalize">{run.status}</td>
                        <td className="py-1.5 pr-3">{new Date(run.started_at).toLocaleString()}</td>
                        <td className="py-1.5 pr-3">{run.duration_ms ?? "—"}</td>
                        <td className="py-1.5 pr-3">
                          {run.feeds_polled ?? 0}
                          {(run.feeds_not_modified ?? 0) > 0 ? ` (${run.feeds_not_modified})` : ""}
                        </td>
                        <td className="py-1.5 pr-3">
                          {run.items_polled} / {run.items_extracted} / {run.items_enriched}
                        </td>
                        <td className="py-1.5">{run.jobs_failed}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Feed Health */}
        {isOnline && (
          <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold">Feed Health (Severity Sorted)</h2>
            <div className="mt-2 overflow-x-auto">
              <table data-testid="feed-health-table" className="w-full text-left">
                <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="py-1.5 pr-3">Feed</th>
                    <th className="py-1.5 pr-3">User Intent</th>
                    <th className="py-1.5 pr-3">Server Health</th>
                    <th className="py-1.5 pr-3">Errors</th>
                    <th className="py-1.5 pr-3">Last Error</th>
                    <th className="py-1.5 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {feedHealth.map((f) => (
                    <tr key={f.id}>
                      <td className="py-1.5 pr-3 font-medium">{f.title}</td>
                      <td className="py-1.5 pr-3 capitalize">{f.status}</td>
                      <td className="py-1.5 pr-3 capitalize">{f.health_state}</td>
                      <td className="py-1.5 pr-3">{f.consecutive_errors}</td>
                      <td className="max-w-xs truncate py-1.5 pr-3 text-rose-600 dark:text-rose-400">
                        {f.last_error ?? "—"}
                      </td>
                      <td className="py-1.5 text-right">
                        <button
                          type="button"
                          onClick={() => void handlePollNow(f.id)}
                          className="rounded border border-slate-300 px-2 py-0.5 font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                        >
                          Poll Now
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Dead-Letter Queue */}
        {deadLetters && (
          <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold">Dead-Letter Queue ({deadLetters.total})</h2>
            <div className="mt-2 overflow-x-auto">
              <table data-testid="dead-letter-table" className="w-full text-left">
                <thead className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="py-1.5 pr-3">ID</th>
                    <th className="py-1.5 pr-3">Type</th>
                    <th className="py-1.5 pr-3">Attempts</th>
                    <th className="py-1.5 pr-3">Last Error</th>
                    <th className="py-1.5 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {deadLetters.jobs.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="py-2 text-slate-500">
                        No dead-letter jobs.
                      </td>
                    </tr>
                  ) : (
                    deadLetters.jobs.map((job) => (
                      <tr key={job.id}>
                        <td className="py-1.5 pr-3 font-mono">#{job.id}</td>
                        <td className="py-1.5 pr-3 font-mono">{job.job_type}</td>
                        <td className="py-1.5 pr-3">
                          {job.attempts} / {job.max_attempts}
                        </td>
                        <td className="max-w-sm truncate py-1.5 pr-3 text-rose-600 dark:text-rose-400">
                          {job.last_error ?? "—"}
                        </td>
                        <td className="space-x-2 py-1.5 text-right">
                          <button
                            type="button"
                            data-testid={`dl-retry-${job.id}`}
                            onClick={() => void handleRetryJob(job.id)}
                            className="rounded bg-sky-600 px-2 py-0.5 font-medium text-white hover:bg-sky-700"
                          >
                            Retry
                          </button>
                          <button
                            type="button"
                            data-testid={`dl-dismiss-${job.id}`}
                            onClick={() => void handleDismissJob(job.id)}
                            className="rounded border border-rose-300 px-2 py-0.5 font-medium text-rose-700 hover:bg-rose-50 dark:border-rose-800 dark:text-rose-300"
                          >
                            Dismiss
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
