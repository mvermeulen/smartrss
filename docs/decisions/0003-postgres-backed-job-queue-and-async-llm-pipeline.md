# 0003: PostgreSQL-Backed Job Queue & Async LLM Pipeline

- **Status**: Accepted
- **Date**: 2026-11-22 (Updated 2026-11-23 for cron/batch execution & stage timing metrics; accepted 2026-11-23)

## Context
Feed polling, full-text extraction, PDF mini-research, local LLM enrichment, and clustering are I/O- and compute-bound tasks that can take seconds to tens of seconds per item and may face transient failures (down feeds, busy/restarting local LLM server). At ~25 feeds and ~200 items/day, throughput is modest, but durability, deduplication, stale-lease recovery, priority ordering, observability, and low operational complexity are critical.

## Decision
1. Use a **PostgreSQL `jobs` table** consumed via `SELECT ... FOR UPDATE SKIP LOCKED` rather than introducing Redis, Celery, or RabbitMQ.
   - *Build vs. library note*: The queue surface is tiny (~150 lines of SQL/Python) and tightly coupled to SmartRSS's `dedupe_key`, priority tiers, LLM concurrency semaphore, and System Health UI; keeping it in-repo avoids framework magic and schema lock-in (we may still inspect `procrastinate` during `1a` component design if needed, but the default is a small tailored table).
2. **Operational queue semantics**:
   - **Deduplication**: Partial unique index on `(job_type, dedupe_key) WHERE status IN ('queued', 'running')` so duplicate `poll_feed` or `enrich_item` jobs are never enqueued for the same entity.
   - **Leases & Stale-Job Reclaim**: Workers set `lease_expires_at` and extend via `heartbeat_at`; a periodic reaper resets expired `running` leases back to `queued` (incrementing `attempts`) or moves exhausted jobs to `failed` (dead-letter).
   - **Priorities & Concurrency**: Priority levels (`10` interactive/user-triggered > `50` steady-state ingestion > `90` backfill/re-enrich) and a global LLM concurrency limit of `1` so the host GPU is never oversubscribed.
   - **Scheduler Leadership & Cron-Friendly Batch Mode**: Periodic schedulers (`poll_feed` dispatch, `expire_items`, stale-lease reaper, retention cleanup) acquire a PostgreSQL session advisory lock (`pg_try_advisory_lock`) so running multiple worker replicas never double-schedules. In addition to continuous daemon mode (with optional cron enrichment windows / quiet hours), expose an idempotent one-shot CLI (`smartrss worker run-once [--stage all|poll|enrich]`) so external host `cron` scripts or `systemd` timers can periodically pre-generate metadata ahead of reading time and emit a timing summary.
   - **Retention, Dead-Letter & Performance Telemetry**: Retain `done` jobs for 30 days and `failed` jobs until manually retried or dismissed via the System Health UI. Record compact per-stage execution timings (`stage_metrics`: queue wait, duration, LLM prompt/gen ms, tokens, readiness lag, `feed_id`, `model_name`) and batch run summaries (`pipeline_runs`) retained for 180 days so users can quantitatively tune cron schedules, models, token budgets, and expensive feeds.
3. **Pipeline stage ordering & idempotency**:
   - Discrete, idempotent stages: `poll_feed` → `extract_content` → `cluster_item` (cheap pre-enrichment URL + excerpt dedup/cluster) → `mini_research` (when triggered) → `enrich_item` (cluster-aware LLM enrichment) → `update_cluster_summary` (when needed), plus periodic `expire_items`.
   - Every stage is safe to re-run without creating duplicate rows or corrupting state; items are immediately visible to clients as `pending` enrichment upon ingest.

## Consequences
- **Positive**: Zero additional infrastructure containers; transactional consistency between domain writes and job enqueuing; crash-safe via lease expiry; trivial SQL-backed queue visibility, dead-letter retry, and priority scheduling.
- **Trade-offs**: Requires disciplined lease heartbeats for long-running LLM/PDF jobs and explicit retention cleanup; not intended for thousands of jobs/sec (orders of magnitude above SmartRSS's ~200 items/day scale).
