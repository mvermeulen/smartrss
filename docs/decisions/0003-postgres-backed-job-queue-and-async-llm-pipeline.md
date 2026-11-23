# 0003: PostgreSQL-Backed Job Queue & Async LLM Pipeline

- **Status**: Proposed
- **Date**: 2026-11-22

## Context
Feed polling, full-text extraction, PDF mini-research, local LLM enrichment, and clustering are I/O- and compute-bound tasks that can take seconds to tens of seconds per item and may face transient failures (down feeds, busy/restarting local LLM server). At ~25 feeds and ~200 items/day, throughput is modest, but durability, retryability, observability, and low operational complexity are critical.

## Decision
1. Use a **PostgreSQL `jobs` table** consumed via `SELECT ... FOR UPDATE SKIP LOCKED` rather than introducing Redis, Celery, or RabbitMQ.
2. Split processing into discrete, idempotent pipeline stages (`poll_feed` → `extract_content` → `mini_research` → `enrich_item` → `cluster_item`, plus periodic `expire_items`), each independently retryable with backoff.
3. Make items immediately visible to clients as soon as ingested (status `pending_enrichment`) and incrementally update them as enrichment and clustering complete.

## Consequences
- **Positive**: Zero additional infrastructure containers; transactional consistency between domain writes and job enqueuing (no lost jobs on crash); trivial SQL-backed visibility into queue depth, failures, and manual retries.
- **Trade-offs**: Not intended for thousands of jobs per second—well above SmartRSS's scale of ~200 items/day (~1,000 stage jobs/day).
