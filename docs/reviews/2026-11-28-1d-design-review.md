# Design Review — Milestone `1d` Component Design (LLM Adapter, Enrichment, Prompt/Profile Config & Scheduling)

- **Date**: 2026-11-28
- **Scope**: [`docs/design/1d-llm-adapter-and-enrichment.md`](../design/1d-llm-adapter-and-enrichment.md) (status `In Design Review`, commit `98f155b` on branch `milestone/1d`). The review also covers the existing code the design changes or depends on: `backend/src/smartrss/jobs/queue.py`, `jobs/locks.py`, `worker/runner.py`, `worker/stages/extract_content.py`, `worker/stages/poll_feed.py`, `db/sync_log.py`, `sync/service.py`, `security/safe_http.py`, `scripts/setup-server.sh`, `scripts/check-critical-coverage.py`, and `frontend/src/components/reader/ReadingPane.tsx`, `frontend/src/sync/engine.ts`, `frontend/src/types/sync.ts`.
- **Specs checked against**:
  - `requirements.md` (`FR-FEED-05`, `FR-FEED-06`, `FR-LLM-01`, `FR-LLM-02`, `FR-LLM-04`, `FR-LLM-05`, `FR-STATE-04`, `NFR-SEC-03`, `NFR-SEC-04`, `NFR-SYNC-02`, `NFR-LLM-01`, `NFR-OPS-01`, §6 `1d` row);
  - `data-model.md` (§3.1, §3.3, §4.2, §4.6, §4.8, §4.9, §5.1, §5.2);
  - `architecture.md` §5;
  - ADRs `0001`, `0003`, `0004` and `0006`;
  - `development-process.md` §2.4 and §7, and `review-checklist.md`;
  - the `1a`, `1b`, `1c` and `1d-ops` designs.
- **Reviewer**: Claude Code (Claude Opus 5.5)
- **Finding prefix**: `W` (`W-01`–`W-20`)
- **Status**: `Addressed (2026-11-28, pass 2) — awaiting verification` (verification pass 2026-11-28 found follow-ups `W-01-F1`, `W-02-F1`, `W-07-F1`, `W-01-F2`; all 4 follow-ups addressed alongside `W-01`–`W-20` & `Q-1`–`Q-7`)

## Review outcome

The design is thorough and fits the existing code well. Its strongest parts:

- **The I/O pattern around the LLM call.** There is a short read transaction, no transaction is open during inference, and the write transaction is lease-fenced. On lease loss it rolls back completely. This is the right shape for `C1`/`C3`.
- **Concurrency is enforced twice.** Separate I/O and LLM worker pools plus a cross-process `LOCK_LLM_SLOT` session lock give global concurrency `1` across the daemon and `run-once`. The locks are released by Postgres if a process crashes.
- **Prompt safety.** Placeholders are substituted in one regex pass, never with `str.format()`. Each call gets a random fence nonce, and embedded closing tags are escaped. Untrusted metadata (title, URL) is fenced along with the body.
- **Prompt seeding never overwrites user edits.** A new default revision is inserted inactive when the user has an active `origin='user'` prompt, and `has_newer_default` drives the diff banner.
- **The `S2` boundary stays narrow.** The LLM endpoint comes only from env vars, redirects are refused, responses are capped at 1 MiB, and a static guard test covers it.
- **Sync stays simple.** Enrichment and tags reach clients as part of the parent `item` aggregate. Hydration is chunked, and the strict `SyncLogWriter` guard is extended to the new tables.

**Main risks:**

1. **A short LLM outage dead-letters the whole backlog** (`W-01`). `LLMUnavailableError` goes through `fail_job`, which uses up attempts. A connection refusal fails instantly, so the single LLM slot works through every queued job one after another. With `max_attempts=5` and backoff of 60/120/240/480 s, about 15 minutes of Ollama downtime (host reboot, model reload, upgrade) marks every pending item `status='failed'` for good. A mistyped or removed model (`404`) does the same. This is the opposite of `NFR-LLM-01`'s "graceful degradation if the LLM server is offline".
2. **Enrichment can be stale, and the fix path can deadlock** (`W-02`, `W-03`). If an item's feed entry changes, or the user asks to re-enrich, while an enrichment is running, the new `enrich_item` enqueue hits the dedupe key and is silently dropped. The old-text result is then committed as current. Step A of the design (complete the job first, then lock the item) also takes locks in the opposite order to `extract_content`, which now holds the item row and enqueues into the same dedupe slot. **I reproduced this deadlock on PostgreSQL 16** (see `W-03`).
3. **Upgrade and cron-mode operation** (`W-04`, `W-05`, `W-06`):
   - `run-once` counts gated `enrich_item` jobs as ready work, so a paused or unconfigured LLM makes every cron run spin until `--max-duration` (30 min).
   - Nothing enqueues enrichment for items that existed before the upgrade.
   - Seeding prompts at API startup runs before `setup-server.sh` migrates the schema.

| Severity | Count | IDs |
| :--- | :--- | :--- |
| Blocker | 0 | — |
| Major | 6 | `W-01`–`W-06` |
| Minor | 10 | `W-07`–`W-16` |
| Nit | 4 | `W-17`–`W-20` |

**Recommendation:** resolve `W-01`–`W-06` in the design text and test matrix before approval. None of them needs an architectural change: they come down to failure classification, lock order, rerun flags, drain accounting, an upgrade backfill, and seeding placement. Settle the Minors in the same pass where they are cheap. `W-07`, `W-08`, `W-10` and `W-12` are the ones most worth doing now. The owner also needs to answer the design's `Q-1`–`Q-3` and the extra questions in [§Owner questions](#owner-questions).

### Claims verified vs. inferred

| Claim in design | Result |
| :--- | :--- |
| Deadlock-free ordering of Step A (complete the job, then lock the item `FOR UPDATE`) | **Refuted.** Reproduced on the `db-test` PG16 container with scratch tables that mirror `jobs`' partial unique index (`W-03`). |
| `enqueue_job(... dedupe_key=f"item:{id}")` re-enqueues work for a running item | **Refuted.** Without `bump_existing`, `ON CONFLICT DO NOTHING` returns `None`. With `bump_existing=True`, the update is gated by `WHERE status='queued'`, so a running row is never touched (`jobs/queue.py:74-92`). This affects `W-02`. |
| `run-once` exits cleanly with `llm_unconfigured` / `llm_paused` | **Not supported by the current drain loop.** `_count_ready_running_and_deferred` counts every queued job with `run_after <= now()` in `job_types` as ready (`worker/runner.py:337-347`), and `_worker_loop` returns only when that count is `0` (`runner.py:451-458`). This affects `W-04`. |
| `httpx.AsyncClient(follow_redirects=False)` is enough to pin the LLM target | **Incomplete.** httpx 0.28.1 defaults to `trust_env=True`, and with `HTTP_PROXY` set a plain client mounts a proxy transport for `http://` (checked in `.venv`). `SafeHttpClient` avoids this only because it passes a custom `transport=` (`allow_env_proxies = trust_env and transport is None`, `httpx/_client.py:685`). This affects `W-12`. |
| No existing module outside `safe_http.py` constructs an httpx client (`1d-T01`) | **Verified.** `poll_feed.py` and `extract_content.py` import `httpx` only for exception types. |
| `setup-server.sh` migrates before the API starts | **Refuted.** It runs `up -d --wait db api` and then `db migrate` (`scripts/setup-server.sh:346-349`). This affects `W-06`. |
| Migrations may import app code | **Verified as precedent.** `0002` and `0003` import `smartrss.*`. This affects `W-19`. |
| 1c renders enrichment, tags and tier, and `localSearch.ts` indexes `tldr`/`takeaways`/`tags` | **Verified** (`ReadingPane.tsx:231-286`, `localSearch.ts:25-51`). `status='failed'` is **not** rendered specially (`W-15`). |
| 1c client consumes `interest_profile` pull changes | **Verified** (`sync/engine.ts:1117`). Bootstrap and pull currently send `interest_profile=None` (`sync/service.py:211,241`). This affects `W-13`. |
| Ollama `format: <schema>` and `think: false`, ns timings; llama.cpp `response_format.json_schema`, `chat_template_kwargs`, `timings.{prompt_ms,predicted_ms}` | **Inferred** from upstream documentation as I know it. Not run against a live runtime in this session. `1d-T05` fixtures must be recorded from real servers, not hand-written. |
| `ceil(utf8_bytes / 3.5)` is "conservative on English, code, and CJK" | **Inferred and questionable** (`W-07`). No tokenizer is available locally to measure it. |
| `croniter` is a maintained pure-Python dependency | **Not verified.** Its maintenance status changed during 2024–2025. The owner should confirm it as part of `Q-3`. |

---

## 1. Major

### W-01 — An LLM outage or a missing model dead-letters every queued `enrich_item` within ~15 minutes, permanently marking items `failed`

**Location:** §4.3 (error taxonomy: `LLMUnavailableError` → "Retryable via `fail_job` exponential backoff"; `LLMClientError` → `fail_job`), §6.4 step 5, `jobs/queue.py:246-324`, `domain/backoff.py:117-120`.

**Problem:** `fail_job` uses up an attempt on every failure, and the job dead-letters at `claimed_attempt >= max_attempts` (5). The retry delays add up to 60+120+240+480 s ≈ 15 min. Nothing stops the LLM loop from claiming while the runtime is down. A connection refusal returns in milliseconds, so the single slot claims and fails every queued job in turn. After the fifth round, `mark_enrich_item_failed` writes `ItemEnrichment(status='failed', is_current=True)` for each item and emits an `item` upsert. Nothing retries them later. Recovery takes per-item manual "Re-enrich" (or per-feed, capped at 200) or dead-letter retries one at a time.

`LLMClientError` behaves the same way. If the model isn't present (Ollama `404` after a typo in `LLM_CHAT_MODEL` or `model_override`, a pruned model, or `llama-server` started with a different model), every job fails deterministically, and there is no point retrying it per job.

**Failure scenario:** the owner reboots the host, or Ollama takes 20 minutes to upgrade and re-pull the model. A backfill import has queued 300 `enrich_item` jobs. Within ~15 minutes all 300 are dead-lettered and their items show the failure state. The milestone's own acceptance criterion ("raw items visible within 2 min even if LLM offline") still passes, but the items never get enriched.

**Suggested fix:**
- Treat "the runtime can't serve requests" as a **slot-level** condition, not a job failure. On `LLMUnavailableError`, or on `LLMClientError` for model-not-found, release the job without using up an attempt. The existing `release_job_on_shutdown` already does this (`attempts = greatest(attempts-1, 0)`), with `run_after = now() + backoff`.
- Then open a process-local circuit breaker: the LLM loop stops claiming for an exponentially growing interval (for example 30 s → 10 min), or until `check_llm_health()` returns `ok`.
- Keep attempt-consuming `fail_job` for errors specific to one item: `EnrichmentParseError`, `LLMResponseTooLargeError`, and context-length `400`s (see also `W-09`).
- Surface "LLM unreachable / breaker open" in System Health.
- Tests:
  - Add `1d-T10d`. With the adapter raising a real `httpx.ConnectError` for N minutes of fake time and 50 queued jobs, assert that `attempts` doesn't grow past 1, nothing dead-letters, no `status='failed'` rows appear, and all items enrich once the fake runtime returns.
  - Add the same test for a model-not-found `404`.
  - Prove each test fails with the old `fail_job` path.

### W-02 — Updates that arrive during a running enrichment are dropped by the dedupe key, and the stale result is committed as current (`C1`, `C4`)

**Location:** §6.2 (`_set_extraction_status` → `enqueue_job(... dedupe_key=f"item:{item_id}")`), §6.4 steps 2–4, §7.3 `POST /items/{id}/re-enrich`, `data-model.md` §3.3 ("Updated Entries … re-extract, and enqueue one re-enrichment"), `jobs/queue.py:74-92`.

**Problem:** the enqueue is `ON CONFLICT DO NOTHING` against `uq_jobs_active_dedupe WHERE status IN ('queued','running')`. Even with `bump_existing=True`, the conflict update applies only `WHERE status = 'queued'`. So when an `enrich_item` job for the item is **running** (10–60 s of inference, more with parse retries), any new request for that item is a silent no-op:

- **Entry update:** `poll_feed` sees a changed `entry_hash` on an unread item, updates `title`/`raw_summary`, and re-extracts. `_set_extraction_status` then tries to enqueue the re-enrichment that `data-model.md` §3.3 requires, and gets `None`. The running job finishes with a prompt built from the *old* text in step 2. Step B writes it as `is_current=True` without checking that the inputs are still current. The item now has an updated body and a TL;DR, tags, tier and FTS terms that describe the old one, and nothing will ever correct it.
- **Manual re-enrich:** the user edits their Interest Profile or a feed's `curation_instructions`, then presses "Re-enrich" on an item that is being enriched. The API truthfully says `{queued: false, active_job_status: "running"}`, but the running job uses the old profile. `C4`'s correct pattern is "return the true outcome; **re-enqueue after the running job closes**", and the codebase already does this for polls (`repoll_needed`, `runner.py:183, 210-220`).

**Suggested fix:**
- Add an input fingerprint to the step 2 read, for example `content_hash` / `entry_hash` (or `updated_at`), plus the active profile version and prompt IDs.
- In step B, compare it again under the item's `FOR UPDATE` lock. If it has changed, still write the enrichment (it's better than nothing) but return `rerun_needed=True`. After the fenced completion, enqueue a fresh `enrich_item` with `bump_existing=True`, as the `I-04` repoll does.
- Separately, when `enqueue_job` returns `None` because a **running** row exists (from `_set_extraction_status` and from `/re-enrich`), set a durable "rerun requested" marker. That can be a `jobs.payload->>'rerun'` update on the running row (the fenced completion reads it), or an `items.enrich_requested_at` column compared in step B.
- Specify that the `/re-enrich` response then reports `{queued: true, after_running_job: <id>}` instead of asking the user to retry.
- Tests:
  - Force an `entry_hash` update and re-extract while a fake LLM call is blocked on a barrier. Assert that a second `enrich_item` runs and the final current enrichment reflects the new text.
  - Run the same test for `/re-enrich` during a running job.
  - Each test must fail without the rerun path.

### W-03 — Step A's lock order (`jobs` row, then `items` row) deadlocks against `_set_extraction_status` (`items` row, then a `jobs` insert) — reproduced

**Location:** §6.4 step 4 ("Step A (Lease Fence FIRST, zero item-lock contention on lost lease)" → "Step B (Lock Item …)"), §6.2 (`enqueue_job` inside `_set_extraction_status`, same transaction as the `items` `UPDATE`).

**Problem:**
- **Tx1 (enrich write transaction)** runs `UPDATE jobs SET status='done' WHERE id=J …`. That locks job `J` and marks its live tuple in `uq_jobs_active_dedupe` with Tx1's `xmax`.
- **Tx2 (extract for the same item)** runs `UPDATE items SET extraction_status=… WHERE id=X`, which locks item `X`. It then runs `INSERT INTO jobs … ('enrich_item','item:X') ON CONFLICT DO NOTHING`. The speculative insertion finds `J`'s index entry with an in-progress `xmax` and **waits for Tx1** to learn whether the conflict is real.
- Tx1 then runs `SELECT … FROM items WHERE id=X FOR UPDATE` and **waits for Tx2**. The result is a deadlock.

**Reproduction** (PG16, `smartrss-db-test-1`, scratch tables `n_jobs`/`n_items` with the same partial unique index, two threads, Tx1 started 0.2 s earlier): `{'extract': 'DeadlockDetected', 'enrich': 'ok'}`. Postgres aborts one side after `deadlock_timeout`.

- If the extract side is aborted, it goes through the generic `fail_job` retry, wastes an attempt, and leaves `extraction_status` unchanged for a cycle.
- If the enrich side is aborted, a 10–60 s GPU result is thrown away and the job is retried.

The trigger is the same realistic interleaving as `W-02`: an unread item updated or re-extracted while it is being enriched. `poll_feed`'s `entry_hash` path also holds the item lock when it enqueues `extract_content`. That doesn't conflict with this case, but it confirms that "items, then jobs" is the established order.

**Suggested fix:** use the codebase's order in the enrich write transaction:
1. `SELECT … FROM items WHERE id=:id FOR UPDATE`.
2. Then the fenced `complete_job(...)`. If it returns `False`, roll back. Holding the item lock for one extra `UPDATE` costs nothing, so the "zero item-lock contention on lost lease" rationale isn't worth a deadlock.
3. Then the enrichment, tag and FTS writes and `sync_log.record`.

Apply the same order in `mark_enrich_item_failed`. Write the rule into §6.4 as "lock order: `items` → `jobs` → `sync_state`". Add a `T6`-style forced-overlap test (barrier between the two statements) that runs `_set_extraction_status` + enqueue against the enrich write transaction with **no** retry. It must assert that neither transaction sees `40P01`, and it must fail with the step A/B order reversed.

### W-04 — `run-once` treats gated `enrich_item` jobs as ready work and spins until `--max-duration`

**Location:** §4.3 (`LLMUnconfiguredError` → "`run-once --stage enrich` exits cleanly with `status="partial"`"), §6.1 item 2 ("reports deferred work (`run-once`, `C2`)"), §6.3 rule 2 (run-once respects `llm_paused`), §6.5; `worker/runner.py:316-368, 429-458, 554-566`.

**Problem:** the drain loop ends a slot only when `_count_ready_running_and_deferred()` returns `ready_or_own == 0`, and that count includes every `queued` job in `job_types` with `run_after <= now()`. The design lists three conditions in which `enrich_item` jobs stay queued and ready while the LLM slot refuses to claim them:
- `LLM_BASE_URL` is empty (the shipped default, `Q-1`);
- `llm_paused = TRUE`;
- `LOCK_LLM_SLOT` is held by the daemon or another run.

In each case, `run-once --stage all` and `--stage enrich` loop on the 0.5 s wait until `max_duration_seconds` (1800 s). They finish `partial` with `max_duration_reached`. In cron mode the systemd timer fires every 15 minutes, so with the LLM unconfigured or paused, every run holds `LOCK_RUN_ONCE` for 30 minutes. That blocks the next two polls, and poll/extract latency rises from minutes to half an hour (`NFR-SYNC-02`). The design doesn't say how quiet hours affect `run-once` either: rule 3 says "in daemon mode", and rule 4 bypasses only the cron window.

**Suggested fix:**
- Evaluate the gate **once per `run-once` invocation**, and again before each LLM claim.
- When it disallows a priority band, remove those jobs from the "ready" count and add them to `deferred` with a reason (`notes="llm_paused; jobs_deferred=N"`).
- If `LOCK_LLM_SLOT` can't be acquired, do the same with reason `llm_slot_busy`.
- State explicitly whether quiet hours apply to `run-once`. I recommend that they do, because the owner set quiet hours to keep the GPU free. Explicit `--stage enrich` could be an exception, since running it is a deliberate act.
- Specify whether `run-once` holds `LOCK_LLM_SLOT` for the whole run or per job, and what happens to the I/O stages when it can't get the lock. They must still run.
- Tests: `--stage all` with (a) an unconfigured LLM, (b) `llm_paused`, and (c) `LOCK_LLM_SLOT` held by another connection. Each must return within a few seconds with poll and extract drained, `partial` status, `jobs_deferred` equal to the enrich count, and the right note. Prove each case fails with today's counter.

### W-05 — Items that exist before the upgrade are never enriched

**Location:** §6.2 (chaining happens only inside `_set_extraction_status`), §7.3 (`re-enrich-unread` is per-feed with `limit ≤ 200`), §3 (migration `0004`).

**Problem:** at upgrade time, every item ingested under `1a`–`1c` already has a final `extraction_status`. Enrichment is enqueued only when extraction status changes, so none of these items will ever get an `enrich_item` job. On the owner's live deployment (30+ hours of soak test, several hundred unread items), the unread list stays "AI enrichment pending" forever. The only remedy is pressing "Re-enrich Unread" feed by feed, at backfill priority, at most 200 items per feed. Relevance sorting and the tier filter (`1c`) stay useless for that backlog. `FR-LLM-01` says to generate metadata "per item".

**Suggested fix:**
- Add an explicit, idempotent backfill. One option is a CLI command such as `smartrss llm backfill [--state unread] [--since DAYS] [--limit N]`. The other is a one-time enqueue in the first worker start after `0004`.
- Either way, it enqueues `enrich_item` at backfill priority `90` for items with no `is_current` enrichment that match the auto-enrichment rules (`unread`, from an `active` feed). Chunk the enqueue (`D1`) and use the dedupe key so a re-run is a no-op.
- Have `setup-server.sh` / the README upgrade notes call it once, and decide the default scope (`Q-5`).
- Test: seed items with `extraction_status` in `ok`/`fallback_rss`/`failed` and no enrichment, run the backfill twice, and assert exactly one job per eligible item and none for muted, expired or already-enriched items.

### W-06 — Seeding prompts at API startup runs before the setup script migrates the schema

**Location:** §3.1 ("`ensure_default_prompts(session)` runs … during API startup, worker startup, and migration `0004`"), `scripts/setup-server.sh:346-352`.

**Problem:** the approved `1d-ops` setup and upgrade path does `docker compose up -d --wait db api` **before** `smartrss db migrate`, which is the fix for `O-10`. On the first `make setup` after pulling `1d`, the new API image starts against a `0003` schema. If the lifespan hook runs `ensure_default_prompts` and lets the `UndefinedTable` error propagate, the API fails its health check, `--wait` fails, and setup aborts before it reaches the migration. A fresh install hits the same problem. If the hook swallows the error instead, the design doesn't say so, and seeding at startup adds nothing, because the migration already seeds.

**Suggested fix:** pick one owner for seeding.
- **Recommended:** seed in migration `0004` and in `smartrss db migrate`, which runs after every upgrade. Seeding in `db migrate` picks up later `DEFAULT_*_REVISION` bumps without needing a new migration.
- If startup seeding stays, it must be best-effort: catch `ProgrammingError`/`UndefinedTable`, log `prompt_seed_skipped_schema_not_ready`, and never fail the lifespan.
- In both cases, the worker and the `enrich_item` read step must handle a missing active prompt (skip claiming, with reason `prompts_not_seeded`) instead of failing jobs.
- Test: start the API app against a database at `0003`, assert that `/api/v1/health` returns `ok`, then migrate and assert that the prompts exist. Add a `setup-server.sh` upgrade-path check if `1d-ops`'s harness allows it.

---

## 2. Minor

### W-07 — The token budget doesn't cover parse-retry turns and isn't checked against what the runtime actually saw

**Location:** §5.3, §4.2 step 5, §2.1 (`LLM_NUM_CTX`).

**Problems:**
1. **Retry turns don't fit.** `prompt_budget = 8192 − 1024 − 256 = 6912` is fully allocated to attempt 0. Attempts 1 and 2 append a corrective turn ("exact Pydantic validation errors + compact reminder"), and probably the previous assistant output, which can be up to `LLM_MAX_OUTPUT_TOKENS` = 1024. A full-length article plus the retry turn therefore exceeds the 256-token margin. Ollama doesn't reject an over-long prompt; it truncates it to `num_ctx`. If it drops the *beginning*, the system rubric and the injection rules are lost on exactly the turns where the model is already struggling.
2. **The estimator is optimistic.** `bytes / 3.5` undercounts for emoji (4 bytes, often 2–3 tokens), many non-Latin scripts, base64 or minified code, and URL-heavy text. "Conservative on CJK" depends on the tokenizer.
3. **llama.cpp ignores the setting.** With `llamacpp`, `LLM_NUM_CTX` has no effect on the server. The context comes from `llama-server -c`, divided across `--parallel` slots. If the server's per-slot context is smaller, requests fail with a `400` about exceeding the context size. The design then classifies that as `LLMClientError`, so it is retried 5 times and dead-lettered.

**Suggested fix:**
- Reserve a fixed retry allowance in the budget (for example 400 tokens). On retries, resend `[system, user]` plus a short corrective user turn, **not** the previous raw output (or truncate it to ~200 tokens).
- After every call, compare the runtime's `prompt_eval_count` / `usage.prompt_tokens` with the estimate. Log a warning when it is more than ~15% above it.
- Treat `prompt_tokens >= num_ctx − num_predict` as a truncation error.
- For `llamacpp`, read `n_ctx` from `GET /props` in the health probe and clamp the budget to it.
- Tests: add a `1d-T03` case for a retry with a maximum-length article, plus fixtures for emoji-heavy and minified-JS text whose recorded `prompt_eval_count` exceeds the estimate.

### W-08 — User-written instructions are silently truncated far below what the editors accept

**Location:** §5.3 (profile cap 800 tokens, feed instructions 450 tokens, system template hard cap 1,200 tokens), §7.3 (`profile_markdown` max 32,768 chars, `template_text` 1..32,768 chars).

**Problem:**
- The 800-token profile cap is about 2,800 bytes. The editor accepts 32,768 characters, so roughly 90% of a long profile can be dropped by head truncation without the user being told. Profiles tend to list exclusions last, and those are exactly what gets cut.
- A system template over 1,200 tokens has no defined behaviour (truncate? reject? fail every job?).
- Precedence level 2 (feed instructions) is truncated at 450 tokens with no indication in the 1c editor.

**Suggested fix:**
- Validate on write: return `422` for `enrichment_system` templates whose estimated tokens exceed the hard cap.
- Return `estimated_tokens` and `budget_tokens` from the profile, prompt and feed `GET`/`PUT` endpoints, and have the 1d editors show a warning ("only the first ~N characters are used").
- Alternatively, lower the max length to match the budget.
- Record a `prompt_truncated` flag (or the per-section truncated token counts) in `stage_metrics`/`item_enrichments` so the System Health view can show it.

### W-09 — Errors specific to one item are retried 5 × 3 times

**Location:** §4.3 (`EnrichmentParseError`, `LLMResponseTooLargeError`, `LLMClientError` → `fail_job` until `max_attempts`).

**Problem:** at temperature `0.1`, an article that defeats the schema three times in one call will almost certainly do it again. Five job attempts × (1 + 2 parse retries) means up to 15 inference calls, potentially more than 10 GPU-minutes for one item. Meanwhile the slot holds back the rest of the queue, against `architecture.md` §5.1's daily GPU budget. The same applies to oversize responses and context-length `400`s.

**Suggested fix:** pass `permanent=True` (it already exists in `fail_job`) for `EnrichmentParseError`, `LLMResponseTooLargeError` and context-length `400`s, or give `enrich_item` its own `max_attempts=2`. Keep normal retries only for genuinely transient classes. Together with `W-01`, the classification is:

| Class | Handling |
| :--- | :--- |
| Unavailable | Release the job and open the breaker |
| Item-deterministic | Fail once or twice, then dead-letter |
| Unexpected | Normal retry |

### W-10 — Enrichment provenance records only the system prompt, not the user prompt or Interest Profile version

**Location:** §3.3 (`prompt_config_id` "references the active `enrichment_system` … used during the call"), §7.3 metrics `llm_breakdown` grouped by `prompt_config_id`, `data-model.md` §4.6 ("Exact prompt version"), §4.8 (`feedback_events.payload` snapshot of `prompt_config_id` for offline evaluation in `1g`).

**Problem:**
- Two independently versioned templates produce each enrichment, but only one is recorded. If the user edits only `enrichment_user`, enrichments before and after the edit share a `prompt_config_id`. The throughput and parse-retry breakdown (`NFR-OPS-01` "by model/prompt") then can't tell them apart.
- `1g`'s offline relevance evaluation can't attribute tier changes to a prompt edit.
- The Interest Profile version, the main input to the relevance tier, isn't recorded at all.

**Suggested fix:**
- Add `user_prompt_config_id UUID NULL` and `interest_profile_version INT NULL` to `item_enrichments`, and the same pair to `stage_metrics`, or record a single `prompt_set_hash`.
- Group `llm_breakdown` by `(model_name, system_id, user_id)`.
- Update `data-model.md` §4.6/§4.9 in the same commit.

### W-11 — `readiness_lag_ms` is recorded for re-enrichments, which skews the readiness metric

**Location:** §6.4 step A (`readiness_lag_ms = db_now − item.fetched_at` on every successful `enrich_item`), §7.3 `readiness_lag`.

**Problem:** `NFR-OPS-01` defines readiness lag as `fetched_at → enriched_at` for an item's first enrichment. A manual re-enrich of a three-month-old item records about 7.8 × 10⁹ ms. A few re-enrichments, or the `W-05` upgrade backfill, then dominate `max`, `avg` and `p95`.

**Suggested fix:** record `readiness_lag_ms` only when no previous `status='completed'` enrichment exists. Consider also limiting it to background priority (`>= 50`) and excluding backfill (`payload.is_backfill`), or report backfill separately. Add a test that a re-enrich records `NULL` lag.

### W-12 — The LLM adapter's httpx client honours `HTTP(S)_PROXY` from the environment (verified)

**Location:** §2.3 items 2 and 5.

**Problem:** `httpx.AsyncClient` defaults to `trust_env=True`. When no custom transport is passed, it routes `http://` through `HTTP_PROXY` (checked against httpx 0.28.1 in `.venv`: the mount type is a proxy transport). `NO_PROXY` doesn't normally list `host.docker.internal`. If the operator's Docker daemon or shell exports a proxy, which is common on corporate or VPN laptops and with `~/.docker/config.json` `proxies`, then every prompt and the `Authorization: Bearer <LLM_API_KEY>` header go to the proxy. Prompts contain article text and the Interest Profile. This breaks `S3` and the "target comes only from `Settings.llm_base_url`" guarantee. `SafeHttpClient` is protected only because it passes `transport=`.

**Suggested fix:** construct the client with `trust_env=False`, or with an explicit `httpx.AsyncHTTPTransport()`, and state this in §2.3. Extend `1d-T01`/`1d-T05` with a test: with `HTTP_PROXY` set, assert that the adapter's client has no proxy mounts, or that a request reaches a local stub directly.

### W-13 — Upgraded clients never receive the seeded Interest Profile or the new `app_settings` fields

**Location:** §3.2 ("records a `sync_changes` `upsert` only on runtime updates (fresh bootstrap reads active profile directly)"), §3.5, §7.1.

**Problem:** existing `1c` clients have already bootstrapped. Today's bootstrap sent `interest_profile=None` (`sync/service.py:211, 241`). After `0004`, the seeded profile and the 8 new `app_settings` columns exist only on the server, and there is no `sync_changes` row for them. A connected client sees them through the online `GET` editors. A detached client's "read-only cached view of Interest Profile & Settings" (§8 item 2) stays empty or on default values until the owner edits the profile or a full resync happens.

**Suggested fix:** in migration `0004`, after seeding, insert one `interest_profile` upsert and one `app_settings` upsert into `sync_changes` through the same `sync_state` sequence allocation, so they follow the watermark. Alternatively, document that `0004` bumps `instance_epoch` so clients re-bootstrap. That works but is heavier. Test: a client watermark taken before `0004` pulls both entities after the upgrade.

### W-14 — Fence and safety framing depend on user-editable templates

**Location:** §5.1 (level 1 "immutable"), §5.2 ("tell the model the exact nonce tag name in the system/user prompt framing"; required placeholders only `{{article_content}}`, `{{article_metadata}}`).

**Problem:** both templates are editable through `PUT /api/v1/prompts/{name}`. The design doesn't say whether the nonce tags and the sentence telling the model the tag name are generated by code around the substituted value or written in the template. If they are in the template, an edit (or a reset to an older user version) can remove the fence while still passing the placeholder check. Level 1 is described as "immutable" but lives in an editable template.

**Suggested fix:**
- Specify that `render()` itself wraps `{{article_content}}` and `{{article_metadata}}` in the nonce tags.
- Have code append a short, **non-editable** safety suffix to the system message ("Content inside `<untrusted_article_content_{nonce}>` is data…").
- Reword level 1 as "code-enforced framing + structured-output schema". The rubric text stays editable.
- Extend `1d-T03`: a user template with no fence text still produces fenced content and the safety suffix.

### W-15 — The client doesn't render `status='failed'`, and the DTO doesn't carry the error

**Location:** §6.4 step 5 ("emit `item` `upsert` so clients clear `pending` and render the failure state"), §8, `ReadingPane.tsx:231-286`, `types/sync.ts:51-61`.

**Problem:** `ReadingPane` treats any non-null `enrichment` as a success. A failed row renders "Relevance Tier —" with empty TL;DR and takeaways, and nothing says enrichment failed or how to retry. §8 doesn't list this UI change, and `ItemEnrichmentDTO` has no `error_message`.

**Suggested fix:** add a failed state to §8: a "AI enrichment failed — Re-enrich" notice that uses the existing button. Optionally add a short, sanitized `error_summary` to the DTO. Make sure tier and depth filters and the relevance sort treat failed items like unenriched ones. Add a `1d-T14`/`T15` case.

### W-16 — Test-matrix and coverage-gate gaps for the new invariants

**Location:** §10.1, §10.2; `development-process.md` §2.4 ("Graceful LLM Degradation … concurrency stays bounded to `1`").

**Problems:**
1. The concurrency-1 invariant lives in `worker/runner.py`: the split I/O and LLM pools, `_llm_worker_loop`, `LOCK_LLM_SLOT` handling, and run-once gating. That module isn't a critical module. `worker/stages/extract_content.py`, which carries the new chaining rules, isn't either.
2. There is no test for the milestone acceptance criterion "raw items visible within 2 min even if LLM offline". The test should poll, then pull with the LLM stub down, and assert the items arrive with `enrichment=null`.
3. There are no tests for `W-01`–`W-04` (outage behaviour, rerun on update, lock-order deadlock, gated run-once drain).
4. `1d-T09(a)` should force overlap between the **daemon LLM loop and `run-once --stage enrich`**, not only between two daemons, because that is the cross-process case the session lock exists for.

**Suggested fix:**
- Add `worker/runner.py`, or extract the LLM loop into `worker/llm_slot.py` and list that, to `CRITICAL_MODULES`.
- Add the tests above to §10.2 with IDs.
- Each concurrency test must disable internal retries and be shown to fail with its invariant broken (`T6`).

---

## 3. Nit

### W-17 — The `LOCK_LLM_SLOT` connection stays "idle in transaction" for the whole LLM call

`advisory_session_lock` runs `pg_try_advisory_lock` on an autobegun `AsyncConnection` and never commits (`jobs/locks.py:35-38`). For the 1–9 minutes of an enrichment, that connection shows as `idle in transaction`. If anyone later sets `idle_in_transaction_session_timeout` (a common hardening step), Postgres kills the session. That silently releases the lock mid-call and allows a second concurrent LLM call. `LOCK_RUN_ONCE` has the same pattern. Suggest calling `await conn.commit()` right after acquiring (a session-level advisory lock survives the commit). Add a comment.

### W-18 — Edge cases in capability fallback and model checks

- §4.1: "HTTP 400 while `think` was sent → retry without it and disable `supports_think_param`" fires on *any* `400`, including context-length errors. Match on the error body (Ollama: `"does not support thinking"`) before downgrading.
- §4.3: the `model_missing` health check should normalise Ollama's implicit `:latest` tag (`qwen3` vs `qwen3:latest`).
- `prompt_configs.model_override` is accepted through `PUT` without checking it against the runtime's model list. That's a cheap way to hit `W-01`'s 404 case. Validate it against the cached health result and return a warning, not an error, if it is unknown.

### W-19 — Migration `0004` would seed from live `prompts.py`

§3.1 runs `ensure_default_prompts` inside migration `0004`. Importing app code has precedent (`0002`, `0003`), but because this imports *template text*, the migration's result changes whenever `prompts.py` gains a new revision. The interest-profile starter text has the same property. Put a frozen copy of the `1d-v1` text in the migration and let `db migrate`/startup seeding (`W-06`) handle later revisions. Also say which mechanism seeds `display_name` for `tags` (the raw LLM label or the slug).

### W-20 — Spec text loose ends

- §6.3 rule 2 contains an unresolved inline question ("`--ignore-pause`? Default: …"). Turn it into a decision.
- `evaluate_enrichment_gate(settings_row, *, now_utc, is_run_once)` reads `cfg.llm_base_url`, which isn't in its signature. Pass `llm_configured: bool` so the function stays pure.
- §6.4 step 2 skips only `unsubscribed` feeds for background jobs. Say whether a job queued before the user muted the feed, or for an item since marked `read`/`expired`, still runs. The data model's "LLM enrichment skipped by default" for muted feeds suggests it should not.
- `item_enrichments.status='pending'` is never written by `1d`. Note that "no current row" is how pending is represented.

---

## Owner questions

In addition to the design's own `Q-1`–`Q-3`:

- **`Q-4` — Outage policy (`W-01`):** should an unreachable runtime or a missing model ever dead-letter enrichment jobs? **Recommended:** never. Release the job, back off the slot, and show "LLM unreachable since …" in System Health. Dead-letter only after errors specific to one item.
- **`Q-5` — Upgrade backfill scope (`W-05`):** enrich all existing `unread` items from active feeds at priority `90` (recommended), only items fetched in the last N days, or nothing until the owner runs the CLI command?
- **`Q-6` — Quiet hours and `run-once` (`W-04`):** should external-cron `run-once` honour quiet hours? **Recommended:** yes for `--stage all`, no for an explicit `--stage enrich`.
- **`Q-7` — Ollama `keep_alive`:** the owner pauses the LLM to free the GPU (for example for gaming). Should the adapter send `keep_alive` (for example `"5m"` by default, configurable through `LLM_KEEP_ALIVE`), and should switching the pause on send a `keep_alive: 0` unload request so VRAM is released right away?
- **`Q-3` addendum:** confirm `croniter`'s current maintenance status before adding it. The 5-field subset the design needs is small enough to write in-repo if not.

### Owner decisions (2026-11-28)

The project owner answered all seven questions. The design must be updated to match them when the findings are addressed.

| ID | Decision | Implications for the design update |
| :--- | :--- | :--- |
| **`Q-1`** — default runtime and model | **Ollama, with `LLM_CHAT_MODEL=qwen3.5:35b-a3b-q8_0-ctx256k`** (not `qwen3:30b-a3b`). Other recommended defaults stand: `LLM_RUNTIME=ollama`, `LLM_BASE_URL` empty (opt-in), `LLM_DISABLE_THINKING=true`. | Update §2.1, §11 and `.env.example`. Mark `requirements.md` §7 open question #3 (chat model) resolved. The tag looks like a locally built Modelfile variant rather than a registry tag, so `README.md`/`.env.example` should say it must exist on the host (`ollama list`) and give a registry-pullable alternative. The `-ctx256k` Modelfile context is overridden per request by `options.num_ctx = LLM_NUM_CTX` (8192), which is intended: the token budget in §5.3 assumes 8192. Raising `LLM_NUM_CTX` is a separate tuning decision for the `1d-T16` benchmark. Record `1d-T16` fixtures and the `think: false` behaviour against this model, and check that the `W-18` capability fallback matches its actual error body. |
| **`Q-2`** — muted feeds and failed re-enrich | **As recommended**: no auto-enrichment for `muted` feeds or backfill-`expired` items (manual Re-enrich still works). A failed manual re-enrich keeps the existing `completed` enrichment as `is_current=True` and records the failure with `is_current=False`. | The design text already matches. Resolve the `W-20` question about jobs queued before a mute in the same direction: skip them in background priority. |
| **`Q-3`** — cron parser | **`croniter`, verified first**: the implementer confirms `croniter` is actively maintained at implementation time and pins it in `uv.lock`. If it isn't maintained, write an in-repo 5-field matcher with `zoneinfo`/DST tests instead. | Record the maintenance check (release date, repository owner) in the implementation commit or the `1d` implementation review. |
| **`Q-4`** — outage policy (`W-01`) | **Never dead-letter on outage**. `LLMUnavailableError` and model-not-found release the job without using up an attempt. The LLM slot backs off with a circuit breaker until `check_llm_health()` returns `ok`. System Health shows "LLM unreachable since …". Only item-specific errors (parse failure, oversize response, context-length `400`) dead-letter. | This fixes `W-01` and sets the `W-09` classification. Add the outage tests listed in `W-01`. |
| **`Q-5`** — upgrade backfill (`W-05`) | **All unread items from active feeds**. One idempotent backfill (`smartrss llm backfill`, run once by the setup/upgrade path) enqueues `enrich_item` at backfill priority `90` for every `unread` item from an `active` feed that has no current enrichment. | Wire it into `setup-server.sh`/the README upgrade steps. It must be a no-op on re-run (dedupe key plus the "no current enrichment" filter). Exclude its jobs from the readiness-lag metric (`W-11`). |
| **`Q-6`** — quiet hours and `run-once` (`W-04`) | **Never honour** (this departs from the recommendation). Quiet hours and the cron window apply only to the daemon. Under external cron, the owner controls timing through the timer schedule. `run-once` still respects `llm_paused` (§6.3 rule 2). | Update §6.3 rule 3 so it says "daemon only" and `is_run_once=True` skips it. `W-04`'s gated-drain fix is still required for `llm_paused`, an unconfigured LLM and `LOCK_LLM_SLOT` contention. Document in the README that a cron-mode user who wants quiet hours must shape the systemd timer (`OnCalendar`). Add a `1d-T04` case: quiet hours active plus `is_run_once=True` allows all priorities. |
| **`Q-7`** — Ollama `keep_alive` | **Configurable, and unload on pause**. Add `LLM_KEEP_ALIVE` (default `"5m"`, validated as an Ollama duration string or an integer number of seconds), sent on every `ollama` chat request. When `llm_paused` changes `FALSE → TRUE` (through `PATCH /api/v1/settings` or `smartrss settings set`), send a best-effort `keep_alive: 0` unload request for the effective model(s) so VRAM is freed at once. | Add `LLM_KEEP_ALIVE` to §2.1 and §4.1 (with `llamacpp` as a documented no-op). The unload must run **after** the settings commit, outside the request transaction (`C6`), with a short timeout. Its failure must never fail the settings update; log it and report it in the response or health view. Let any in-flight call finish first: the worker checks pause before each claim. Unload `system`/`user` `model_override` models as well as `LLM_CHAT_MODEL`. Tests: an adapter fixture for the unload request, and an API test that pausing triggers exactly one unload attempt and still returns `200` when Ollama is down. |


---

## Resolutions (2026-11-28)

All 20 findings (`W-01`–`W-20`) and all 7 owner decisions (`Q-1`–`Q-7`) have been addressed in [`docs/design/1d-llm-adapter-and-enrichment.md`](../design/1d-llm-adapter-and-enrichment.md) and synchronized across [`docs/data-model.md`](../data-model.md), [`docs/requirements.md`](../requirements.md), [`docs/review-checklist.md`](../review-checklist.md), and [`docs/README.md`](../README.md).

### Major Findings
- **W-01** — Fixed (with owner decision **`Q-4`**). Partitioned LLM failures into 3 explicit operational classes in `1d-llm-adapter-and-enrichment.md` §4.3 & §6.4 step 5: `LLMUnavailableError` and `LLMModelNotFoundError` (`404`) release the job without consuming an attempt (`release_job_without_attempt`, `attempts = GREATEST(attempts - 1, 0)`), open a slot-level circuit breaker (`30s → 600s` until `check_llm_health(force=True)` returns `ok`), and persist internal operational columns `app_settings.llm_unavailable_since` and `llm_last_error` (never client-synced, surfaced in `GET /api/v1/system/health` and `SystemHealthView` as `"LLM unreachable since ..."`). Added test `1d-T10d` (50 queued jobs under `httpx.ConnectError` and `404` model-not-found, proven to fail under `fail_job`).
- **W-02** — Fixed. Added durable running-job rerun signaling (`mark_running_job_rerun` setting `jobs.payload.rerun_requested = true` + `rerun_priority` when `enqueue_job` hits a `running` row, with retry-once guard against finish races) in both `_set_extraction_status` (§6.2) and `POST /api/v1/items/{id}/re-enrich` (§7.3, returning `{queued: true, job_id: null, after_running_job: <id>}`). Added Step 2 `EnrichInputFingerprint` comparison in Step C under `Item` `FOR UPDATE` (and rerun-on-failure check in step 5) so any content, profile, feed-instruction, or prompt change during an in-flight call enqueues a fresh `enrich_item` immediately after completion (§6.4). Added forced-overlap barrier test `1d-T09c` (proven to fail without rerun path).
- **W-03** — Fixed. Enforced mandatory cross-stage lock order **`items` (`FOR UPDATE`) → `jobs` → `sync_state`** in `run_enrich_item` (§6.4 Steps A–C) and `mark_enrich_item_failed` (§6.4 step 5), matching `poll_feed` and `extract_content`. Lease-fenced `complete_job` runs right after locking `Item` and rolls back the entire transaction on lease loss. Added barrier-forced deadlock test `1d-T09d` (`max_attempts=1`, zero retries, proven to deadlock with `jobs → items` order) and added recurring failure class **`C10`** to `docs/review-checklist.md`.
- **W-04** — Fixed (with owner decision **`Q-6`**). Specified gated drain accounting in `_count_ready_running_and_deferred` (§6.3, §6.1, §6.5): when `enrich_item` is disallowed in `run-once` (`llm_unconfigured`, `llm_paused`, `prompts_not_seeded`, `llm_slot_busy`, or `llm_unavailable`), queued `enrich_item` jobs are excluded from `ready_or_own` and counted in `jobs_deferred` with explicit `notes`, while I/O stages always run and `run-once` exits immediately instead of spinning to `--max-duration`. Documented `LOCK_LLM_SLOT` scope (per-job in daemon, non-blocking per-drain in `run-once`) and made quiet hours & cron windows **daemon-only** per `Q-6` (`is_run_once=True` bypasses both; README guides cron-mode users to systemd `OnCalendar`). Added tests `1d-T11b` and `1d-T04` (`is_run_once=True` + quiet hours).
- **W-05** — Fixed (with owner decision **`Q-5`**). Added idempotent `smartrss llm backfill [--state unread|all] [--since-days N] [--limit N] [--json]` (§6.5) defaulting to all `unread` items from `active` feeds with `extraction_status != 'pending'` and no `is_current = TRUE` enrichment, chunked at 500 (`D1`), enqueued at `priority=90, is_backfill=True` via dedupe key, wired into `scripts/setup-server.sh` after `smartrss db migrate`, and excluded from readiness lag (§6.4 step B). Added test `1d-T11c`.
- **W-06** — Fixed. Removed blocking prompt seeding from API startup (§3.1) so `scripts/setup-server.sh` (`up -d --wait db api` before `db migrate`) always starts cleanly against schema `0003`. Assigned seeding to migration `0004` (frozen `1d-v1` literals) + `smartrss db migrate` (`ensure_default_prompts` under `LOCK_PROMPT_SEED`), with best-effort worker startup seeding and a `prompts_not_seeded` gate in `evaluate_enrichment_gate` / `enrich_item` (§6.3, §6.4). Synced `docs/data-model.md` §4.9 and added test `1d-T06b`.

### Minor Findings
- **W-07** — Fixed. Reserved `RETRY_BUDGET_TOKENS = 400` in `prompt_budget` (`6,512` tokens at `num_ctx=8192`) and banned echoing previous raw assistant output on parse-retry turns (§4.2 step 6, §5.3). Replaced "conservative on CJK" claim with honest heuristic framing plus post-call verification against runtime-reported `prompt_tokens` (logging `llm_prompt_token_estimate_drift` at `> 15%` overshoot and raising item-deterministic `LLMContextTruncatedError` when `prompt_tokens >= effective_num_ctx - max_output_tokens`, §4.2 step 5). Added `llamacpp` `GET /props` `n_ctx` probing and budget clamping (§4.1, §4.3). Extended `1d-T03` and `1d-T05`.
- **W-08** — Fixed. Added write-time validation rejecting `PUT /api/v1/prompts/enrichment_system` above `1,200` estimated tokens with `422` (§5.3, §7.3), added `estimated_tokens` and `budget_tokens` to `interest-profile`, `prompts`, and `feeds` API responses plus live counters & truncation warnings in all three editors (§7.3, §8), and added `prompt_truncated BOOLEAN NOT NULL DEFAULT FALSE` to `item_enrichments`, `stage_metrics`, `docs/data-model.md` §4.6/§4.9, and `GET /api/v1/system/metrics` (`truncated_calls`, §3.3, §7.3).
- **W-09** — Fixed (with owner decision **`Q-4`**). Classified `EnrichmentParseError`, `LLMResponseTooLargeError`, `LLMContextLengthError`, `LLMContextTruncatedError`, and non-404 `LLMClientError` as item-deterministic → `fail_job(..., permanent=True)` (immediate dead-letter after 1 job attempt), while keeping `max_attempts = 3` for unexpected exceptions (§4.3, §6.4 step 5). Extended `1d-T10`.
- **W-10** — Fixed. Added `user_prompt_config_id UUID NULL` and `interest_profile_version INT NULL` to both `item_enrichments` and `stage_metrics` in migration `0004` (§3.3), grouped `GET /api/v1/system/metrics` `llm_breakdown` by `(model_name, prompt_config_id, user_prompt_config_id)` (§7.3, §8), synced `docs/data-model.md` §4.6/§4.9, and updated `1d-T08`/`1d-T13`.
- **W-11** — Fixed (with owner decision **`Q-5`**). Scoped `readiness_lag_ms` strictly to an item's first enrichment (`no prior status='completed' row`) on normal steady-state background jobs (`priority >= 50` and `not is_backfill`); re-enrichments and backfills store `readiness_lag_ms = NULL` (§6.4 step B, §7.3, `docs/data-model.md` §4.9). Extended `1d-T10`.
- **W-12** — Fixed. Required `httpx.AsyncClient(follow_redirects=False, trust_env=False)` in `LLMAdapter` (§2.3 item 2) so ambient `HTTP_PROXY`/`HTTPS_PROXY` variables never intercept prompts or `LLM_API_KEY`. Updated `docs/review-checklist.md` `S2` and extended `1d-T01`/`1d-T05` with an `HTTP_PROXY` test asserting zero proxy mounts and direct local-stub delivery.
- **W-13** — Fixed. Migration `0004` allocates the next 2 sequence numbers from `sync_state` and inserts `sync_changes` `upsert` rows for `interest_profile` and `app_settings` so already-bootstrapped `1c` clients pull both on their next `sync/pull` without a full resync (§3.2). Extended `1d-T06`.
- **W-14** — Fixed. Specified that `render()` wraps `{{article_metadata}}` and `{{article_content}}` in per-call randomized XML nonce tags in Python code and appends a non-editable security suffix to the `system` message naming the exact nonce tags (§5.1, §5.2). Reworded Precedence Level 1 accordingly and extended `1d-T03` (custom templates with zero fence/safety text still emit fenced data + safety suffix).
- **W-15** — Fixed. Added sanitized `error_summary` (<= 200 chars, when `status == 'failed'`) to `ItemEnrichmentDTO` (§7.1) and specified frontend failed-state rendering in §8 (`ReadingPane` `"AI enrichment failed"` notice + Re-enrich button, `ItemList` compact `"Failed"` badge, and tier/depth filters + relevance sort treating failed items identically to unenriched items). Extended `1d-T14` and `1d-T15`.
- **W-16** — Fixed. Extracted dedicated LLM loop, circuit breaker, `LOCK_LLM_SLOT`, and gated `run-once` logic into `src/smartrss/worker/llm_slot.py` and added both `worker/llm_slot.py` and `worker/stages/extract_content.py` to `CRITICAL_MODULES` (`>= 90%`, 7 new modules total, §10.1). Added test IDs `1d-T06b`, `1d-T08b` (offline-LLM end-to-end acceptance), `1d-T09a` (daemon + `run-once` cross-process overlap), `1d-T09c`, `1d-T09d`, `1d-T10d`, `1d-T11b`, and `1d-T11c`, requiring retries disabled and revert-proof for all concurrency/outage tests (`T6`, §10.2).

### Nits
- **W-17** — Fixed. Specified `await conn.commit()` immediately after acquiring (and releasing) `pg_try_advisory_lock` in `advisory_session_lock` (`jobs/locks.py`, §6.1 item 2) with explanatory comment and `pg_stat_activity.state == 'idle'` assertion in `1d-T09a`.
- **W-18** — Fixed. Narrowed HTTP `400` thinking-param fallback to match response-body indicators so context-length `400`s never trigger fallback (§4.1); added Ollama `:latest` tag normalization in `check_llm_health` (§4.3); added non-blocking `warnings: list[str]` on `PUT /api/v1/prompts/{name}` when `model_override` is not in cached runtime models (§7.3, §8, `1d-T05`, `1d-T13`).
- **W-19** — Fixed. Embedded frozen `1d-v1` template and starter interest-profile literals directly in migration `0004` (§3.1, §3.2) and defined `tags.display_name` seeding from trimmed, single-space-collapsed raw LLM tag labels (<= 48 chars, fallback to `slug`, first-seen canonical on conflict, §3.4, `1d-T02`).
- **W-20** — Fixed. Removed inline `--ignore-pause?` question from §6.3 rule 2 (definitively: `run-once` honours `llm_paused`, no `--ignore-pause` flag in `1d`); passed `llm_configured: bool` and `prompts_seeded: bool` into pure `evaluate_enrichment_gate` (§6.3); skipped background jobs (`priority >= 50`) when `feed.status != 'active'` OR `item.state != 'unread'` (§6.4 step 2, aligned with `Q-2`); explicitly documented that `1d` never writes `item_enrichments.status='pending'` (represented by no `is_current` row, §3.3, `docs/data-model.md` §4.6).

### Owner Decisions Incorporated (`Q-1`–`Q-7`)
- **`Q-1`**: Default `LLM_CHAT_MODEL=qwen3.5:35b-a3b-q8_0-ctx256k`, `LLM_RUNTIME=ollama`, `LLM_BASE_URL=""` (opt-in), `LLM_DISABLE_THINKING=true`, `LLM_NUM_CTX=8192` (§2.1, §10.2 `1d-T16`, §11, §12, `docs/requirements.md` §7 #3, `docs/data-model.md` §4.6).
- **`Q-2`**: No auto-enrichment for `muted` feeds or backfill-`expired` items (including pre-mute queued background jobs); failed manual re-enrich preserves existing `completed` row as `is_current=True` (§6.2, §6.4 steps 2 & 5, §11).
- **`Q-3`**: `croniter` maintenance check required at implementation time before pinning in `uv.lock`, with in-repo 5-field `zoneinfo`/DST matcher fallback (§4, §6.3 rule 5, §11, §12).
- **`Q-4`**: Never dead-letter on outage/model-not-found (`W-01`, `W-09`, §3.5, §4.3, §6.4, §11).
- **`Q-5`**: Idempotent `smartrss llm backfill` for all `unread` items from `active` feeds wired into `setup-server.sh` after `db migrate` and excluded from readiness lag (`W-05`, `W-11`, §6.5, §11).
- **`Q-6`**: Quiet hours and cron windows daemon-only; `run-once` bypasses both and honours `llm_paused` (`W-04`, §3.5, §6.3, §11, §12).
- **`Q-7`**: Added `LLM_KEEP_ALIVE` (default `"5m"`, validated duration/seconds) sent on Ollama requests (`llamacpp` no-op) + post-commit best-effort `keep_alive: 0` unload on `llm_paused` `FALSE → TRUE` across `LLM_CHAT_MODEL` and active prompt `model_override`s in `PATCH /api/v1/settings` and `smartrss settings set` (§2.1, §4.1, §4.3, §6.5, §7.3, §11, `1d-T05`, `1d-T13`).

### Verification (`make check`)
- `make check` passed (exit 0): backend `ruff check`/`ruff format`/`mypy` clean, 219 pytest passed (92.94% total branch coverage, all 17 critical modules >= 90%); client-runner 32 passed (94.87% branch coverage); frontend lint/typecheck clean, 62 vitest passed (93.3% lines / 89.2% branches, all critical >= 90% and core >= 80% gates passed).

## Verification pass (2026-11-28)

- **Verifier**: Claude Code (Claude Opus 5.5), in a fresh session.
- **Scope**: resolution commit `413e1d2`, which changes the `1d` design, `data-model.md`, `requirements.md`, `review-checklist.md` and `docs/README.md`.
- **What a design-only fix means for this pass**: no application code changed, so the revert-the-fix check doesn't apply here. Each fix was checked against the design text, against the existing code it will change (`jobs/queue.py`, `worker/runner.py`, `worker/stages/extract_content.py`, `jobs/locks.py`), and for sibling paths with the same shape. Each proposed test was checked to see whether it would fail with its invariant broken. The revert-the-fix check is deferred to the `1d` implementation review.
- **`make check`**: green (exit 0).
  - Backend: 219 passed, 93% total coverage (the recorded figure was 92.94%), and every critical module is ≥ 90%.
  - `client-runner`: 32 passed (95%).
  - Frontend: 62 passed (93.3% lines, 89.2% branches), and both the critical and core gates pass.
  - The test counts match the resolution notes. That is expected, because the commit changes only docs.
- **Docs and status drift**: there is one index row for this review, with no duplicates. The `1d` status agrees across the design header, `docs/design/README.md`, `AGENTS.md` and `docs/README.md` (`In Design Review`). The `data-model.md` §4.6/§4.9 changes for the new columns are present. `C10` was added and `S2` updated in `review-checklist.md`. The working tree was clean before this pass.

### Verdicts

| ID | Verdict | Evidence |
| :--- | :--- | :--- |
| `W-01` | ⚠️ Partially resolved | Connection and timeout errors and model-not-found `404`s now release the job without using up an attempt and open the breaker (§4.3, §6.4 step 5, `1d-T10d`). But the new **class 2** dead-letters `401`/`403` and other `400`s at once with `permanent=True`, and those are configuration-wide failures, not item-specific ones. See `W-01-F1`. The health endpoint now writes to the DB (`W-01-F2`, nit). |
| `W-02` | ⚠️ Partially resolved | `mark_running_job_rerun`, the input fingerprint, the `complete_job … RETURNING payload` rerun check, rerun-on-dead-letter, the `/re-enrich` `after_running_job` response and `1d-T09c` are all present. The lock order is consistent: `mark_running_job_rerun` runs after the `items` lock in `_set_extraction_status`. If it waits on the row being completed, the retried `enqueue_job` succeeds once the dedupe slot is free. **Gap:** the paths that return a job to the queue don't clear the rerun flag or carry over its priority (`W-02-F1`). |
| `W-03` | ✅ Resolved | §6.4 now locks `items` → `jobs` → `sync_state` in Steps A/B/C and in `mark_enrich_item_failed`. `_set_extraction_status` already holds the `items` lock before it enqueues. `1d-T09d` forces the overlap with retries disabled, and would deadlock with the order reversed (this matches my PG16 reproduction). The rule is added to the checklist as `C10`. Clearing `app_settings.llm_unavailable_since` in Step C adds `app_settings` between `jobs` and `sync_state`. That is consistent with `PATCH /settings` (`app_settings` → `sync_state`); nothing locks `sync_state` before `app_settings`. |
| `W-04` | ✅ Resolved | §6.3 defines gated drain accounting with explicit reasons (`llm_unconfigured`, `llm_paused`, `prompts_not_seeded`, `llm_slot_busy`, `llm_unavailable`). I/O slots always run. The `run-once` LLM slot takes `LOCK_LLM_SLOT` without blocking, or exits as deferred. Quiet hours are daemon-only (`Q-6`). `1d-T11b` covers all three gate cases with `< 5s` bounds, and would fail with today's counter (`runner.py:337-347`). |
| `W-05` | ✅ Resolved | `smartrss llm backfill` (§6.5) uses the eligibility filter from `Q-5`, chunks at 500, relies on dedupe so a re-run is a no-op, is wired into `setup-server.sh` after `db migrate` and documented in the README plan (§12), and is excluded from readiness lag. Re-running setup is safe: enriched or dead-lettered items have an `is_current` row and are skipped. `1d-T11c` covers eligibility and the double run. |
| `W-06` | ✅ Resolved | The API lifespan no longer seeds. Seeding happens in `0004` (frozen literals) and in `smartrss db migrate`. Worker startup seeding is best-effort, and the gate has a `prompts_not_seeded` reason, so jobs are released without using up an attempt. `1d-T06b` starts the API against a `0003` schema. |
| `W-07` | ⚠️ Partially resolved | The 400-token retry reserve is in place (6,512-token budget, arithmetic checked). Raw output is no longer echoed on retries, and the corrective turn is capped at ≤ 300 tokens. The llama.cpp `/props` `n_ctx` clamp, drift logging and `LLMContextTruncatedError` are added. **Gap (inferred):** the truncation and drift checks rely on `prompt_eval_count`, which on Ollama is believed to exclude prompt-cache hits (`W-07-F1`). |
| `W-08` | ✅ Resolved | The system template now gets a `422` above 1,200 tokens. `estimated_tokens`/`budget_tokens` are returned on the profile, prompt and feed endpoints, the editors show counters, and `prompt_truncated` is stored on `item_enrichments`/`stage_metrics` and in metrics. |
| `W-09` | ✅ Resolved for item-specific errors | Parse, oversize and context-length errors dead-letter after one attempt, and unexpected errors use `max_attempts=3`. The `LLMClientError` part of class 2 belongs to `W-01-F1`. |
| `W-10` | ✅ Resolved | `user_prompt_config_id` and `interest_profile_version` are on both tables, the breakdown groups by `(model, system, user)`, and `data-model.md` is synced. |
| `W-11` | ✅ Resolved | Lag is recorded only for the first completed, non-backfill, priority ≥ 50 enrichment (§6.4 Step B). `1d-T10(c)` asserts `NULL` for re-enrich and backfill. |
| `W-12` | ✅ Resolved | `trust_env=False` is set on the adapter, the health probe and the unload client. `1d-T01` asserts zero proxy mounts with `HTTP_PROXY` set, which would fail on httpx 0.28.1's default as verified in the original review. `S2` is updated. |
| `W-13` | ✅ Resolved | Migration `0004` allocates two `sync_state` sequence numbers and inserts `interest_profile` and `app_settings` upserts. `1d-T06` pulls them from a pre-`0004` watermark. *(Implementation note: the inserted rows need `changed_at`. Use `clock_timestamp()` as `SyncLogWriter` does.)* |
| `W-14` | ✅ Resolved | `render()` wraps both untrusted values in nonce tags in code and appends a non-editable safety suffix naming the tags. Level 1 is reworded. `1d-T03` checks templates that contain no fence text at all. |
| `W-15` | ✅ Resolved | `error_summary` is on the DTO. The failed notice in `ReadingPane`, the `ItemList` badge and the filter/sort treatment are specified (§8), with `1d-T14`/`T15` cases. |
| `W-16` | ✅ Resolved | `worker/llm_slot.py` and `extract_content.py` are added to the critical modules (§10.1). The new tests `T06b`, `T08b`, `T09a` (daemon + `run-once`), `T09c`, `T09d`, `T10d`, `T11b` and `T11c` are listed with the `T6` revert-proof requirement. `CRITICAL_MODULES` in the script is updated at implementation time (§10.1). |
| `W-17` | ✅ Resolved | `advisory_session_lock` commits after acquire and after unlock. `1d-T09a` asserts `pg_stat_activity.state == 'idle'`. |
| `W-18` | ✅ Resolved | The thinking fallback now matches on the response body, Ollama's `:latest` tag is normalised, and an unknown `model_override` produces a non-blocking warning. |
| `W-19` | ✅ Resolved | Frozen `1d-v1` literals are in `0004`, and the `tags.display_name` rule is defined (§3.4). |
| `W-20` | ✅ Resolved | The pause decision is final. The gate function now receives `llm_configured`/`prompts_seeded` as arguments. Background jobs are skipped when the feed isn't active or the item isn't unread. "Pending" is defined as no current row. |
| `Q-1`–`Q-7` | ✅ Incorporated | §11 table. `Q-6`: quiet hours and the cron window are daemon-only, and pause still applies to `run-once`. `Q-7`: the unload runs after commit, outside the request transaction, and a failure is reported in the `PATCH` response without failing the update. Both match the owner decisions recorded above. |

**Totals:** 17 resolved, 3 partially resolved (`W-01`, `W-02`, `W-07`), 0 not resolved, 0 deferred.

### Follow-ups

#### `W-01-F1` (Major) — Authentication and other configuration-wide `4xx` errors now dead-letter the whole backlog within seconds

**Location:** §4.3 class 2 (`LLMClientError` "Other HTTP `400`/`401`/`403`" → `fail_job(..., permanent=True)`), §6.4 step 5.

**Problem:** `Q-4` says to dead-letter *only item-specific* errors. A `401`/`403` is not item-specific. It comes from a rotated or mistyped `LLM_API_KEY`, `llama-server` restarted with `--api-key`, or an authenticating reverse proxy in front of Ollama. Neither is a non-context `400` caused by configuration. Two examples: an older Ollama that rejects the `format` JSON-schema object, or an Ollama that rejects a `keep_alive` value that passed our regex but not Ollama's parser.

These errors return in milliseconds and are now `permanent=True`. So the single LLM slot dead-letters **every** queued job, one after another, within seconds, and writes `status='failed'` for each item. That is faster than the ~15 minutes in the original `W-01`. `1d-T10d` covers only `ConnectError` and `404`, so it would not catch this.

**Expected fix:**
- Move `401`/`403` into class 1 with breaker reason `auth_failed`, surfaced in health. `check_llm_health` already maps them to `unreachable`.
- For non-context `400`s, add a configuration-error guard. If the same normalised error signature (status plus a body prefix with IDs and numbers stripped) occurs for **two different items in a row**, treat it as slot-level: release the job without using up an attempt, open the breaker with reason `client_error`, and keep `llm_last_error`. Item-specific `400`s that don't repeat still dead-letter after one attempt.
- Extend `1d-T10d` with (c) a `401` and (d) the same `400` body for 50 jobs. Assert that fewer than two dead-letters occur and that the breaker opens. Each case must fail with the current class-2 mapping.

#### `W-01-F2` (Nit) — The health `GET` writes to the database

§3.5 sets `app_settings.llm_unavailable_since` "when the worker circuit breaker opens **or a live probe fails**". If the API's `GET /api/v1/system/health` probe writes that column, a `GET` has a side effect on the request session (`C6`). It would also mark an outage when only the API container can't reach the host (for example, `extra_hosts` missing on `api`). Make the API probe read-only, with only the worker owning the outage columns, or require a separate short session. State which in §3.5.

#### `W-02-F1` (Minor) — The rerun flag survives when a job goes back to the queue

**Location:** §6.2 (`mark_running_job_rerun` sets `payload.rerun_requested` and `rerun_priority` on the running row); §4.3/§6.4 step 5 (`release_job_without_attempt`, non-permanent `fail_job` retry); the reaper requeue; `release_job_on_shutdown` (`jobs/queue.py:327-358, 400-418`).

**Problem:** when a job marked for rerun returns to `queued` instead of finishing, `payload.rerun_requested` stays set. This happens on an outage release (`Q-4`), an unexpected-error retry, a lease-expiry requeue or a `SIGTERM`. The next attempt re-reads fresh inputs anyway, but when it completes it sees `rerun_requested=true` and enqueues a second, redundant enrichment, costing one wasted GPU call per affected item. The reverse also happens: a manual re-enrich (`rerun_priority=10`) marked on a background job (priority `50`/`90`) that is then requeued stays at the old priority. During a breaker-open window, the user's request waits behind the whole backlog.

**Expected fix:** every requeue path (`release_job_without_attempt`, the retry branch of `fail_job`, the reaper requeue, `release_job_on_shutdown`) sets `payload = payload - 'rerun_requested' - 'rerun_priority'` and `priority = LEAST(priority, COALESCE((payload->>'rerun_priority')::int, priority))` in the same fenced `UPDATE`. Extend `1d-T09c` with (c): mark a running job for rerun, release it through the outage path, recover, and assert exactly one further enrichment that ran at the bumped priority.

#### `W-07-F1` (Minor, inferred) — Truncation and drift checks may miss cache-reused prompt tokens

**Location:** §4.2 step 5 (`prompt_tokens` from Ollama `prompt_eval_count`), §4.1.

**Problem:** I believe, but did not verify in this session, that Ollama's `prompt_eval_count` counts only the prompt tokens actually evaluated, so a prefix reused from the KV cache is left out. That prefix is the shared system message and template across consecutive items, and the whole `[system, user]` prefix on parse-retry turns. llama.cpp similarly reports cached tokens separately (`timings.prompt_n` versus `cache_n`).

If so, on warm-cache calls the reported count understates the real prompt length. The `prompt_tokens >= effective_num_ctx − max_output` truncation check can then miss real truncation, and the drift check can't detect undercounting at all. Accumulating `prompt_tokens` across attempts also double-counts or under-counts inconsistently in `stage_metrics`.

**Expected fix:**
- In `1d-T16`, record two back-to-back calls and one parse-retry call against `qwen3.5:35b-a3b-q8_0-ctx256k` to confirm the behaviour.
- If confirmed, base the truncation and drift checks on a total-prompt figure: llama.cpp `timings.prompt_n + cache_n`, and for Ollama use only the first, cold attempt or skip the check when the count is implausibly low (below the system-prompt estimate).
- Document which field feeds `stage_metrics.prompt_tokens` (evaluated versus total).
- Add a recorded warm-cache fixture to `1d-T05`.

### Residual risks for the owner

None proposed for acceptance. `W-01-F1` is a Major that directly affects owner decision `Q-4`.

### Status

`Addressed — verification found follow-ups` (`W-01-F1` Major; `W-02-F1`, `W-07-F1` Minor; `W-01-F2` Nit). Hand back to Antigravity (`address-review-findings`) for these four items. After that, a second verification pass is needed before the owner's design-approval decision.


---

## Follow-up Resolutions (2026-11-28)

All four verification-pass follow-ups (`W-01-F1`, `W-01-F2`, `W-02-F1`, `W-07-F1`) and the `W-13` `clock_timestamp()` note have been addressed in [`docs/design/1d-llm-adapter-and-enrichment.md`](../design/1d-llm-adapter-and-enrichment.md), [`docs/data-model.md`](../data-model.md), and [`docs/README.md`](../README.md):

- **`W-01-F1` (Major)** — Fixed. Moved HTTP `401`/`403` into Class 1 as `LLMAuthError` (releases job without consuming an attempt via `release_job_without_attempt`, opens slot circuit breaker with `reason="auth_failed"`, persists worker-owned `llm_unavailable_since`/`llm_last_error`, surfaced as `unreachable` in health; §4.3, §6.4 step 5). Added a slot-level consecutive config-error guard for non-context HTTP `400` (`LLMClientError`): normalizes `(http_status, body_prefix_without_ids_or_numbers)` and tracks `(sig, item_id)` (cleared on any successful enrichment); the 1st item dead-letters (`permanent=True`), and if a 2nd distinct item in a row hits the same signature it escalates to Class 1 (releases without consuming an attempt, opens breaker with `reason="client_error"`, leaving at most 1 dead-letter across the backlog). Extended `1d-T05` (`LLMAuthError`) and `1d-T10d` with `(c)` HTTP `401` (`0` dead-letters) and `(d)` identical non-context `400` across 50 queued jobs (`< 2` dead-letters, breaker opens, remaining `>= 49` enrich on recovery), both proven to fail under the prior class-2 mapping.
- **`W-01-F2` (Nit)** — Fixed. Scoped `app_settings.llm_unavailable_since` and `llm_last_error` strictly as **worker-owned** (`worker/llm_slot.py` only, §3.5, `docs/data-model.md` §4.9). `GET /api/v1/system/health` is strictly read-only (`C6`), combining its cached non-blocking live probe with worker-owned DB outage columns without ever writing to `app_settings` (§7.3).
- **`W-02-F1` (Minor)** — Fixed. Specified in §6.4 step 5 that **every** requeue path (`release_job_without_attempt`, the non-permanent retry branch of `fail_job`, `reap_stale_leases` requeue, and `release_job_on_shutdown`) atomically strips stale rerun markers (`payload = COALESCE(payload, '{}'::jsonb) - 'rerun_requested' - 'rerun_priority'`) and carries over any bumped priority (`priority = LEAST(priority, COALESCE(CAST(payload->>'rerun_priority' AS int), priority))`) in the same fenced `UPDATE`. Extended `1d-T09c` with `(c)` testing a background job marked for manual rerun (`rerun_priority=10`), released via outage (plus coverage of `fail_job` retry, reaper requeue, and `release_job_on_shutdown`), verifying `priority == 10`, stripped flags, and exactly one further enrichment after recovery.
- **`W-07-F1` (Minor, inferred)** — Fixed. Clarified in §4.1 & §4.2 steps 5–6 that `stage_metrics.prompt_tokens` / `item_enrichments.prompt_tokens` records **evaluated** prompt tokens (GPU prompt-eval work: Ollama `prompt_eval_count`, llama.cpp `timings.prompt_n` fallback `usage.prompt_tokens`), while drift/truncation checks run on **attempt `0` only** using `total_prompt_tokens` (llama.cpp `timings.prompt_n + cache_n`; Ollama `prompt_eval_count` only when plausible `>= estimate_tokens(system_msg) * 0.5`, skipping warm-cache partial hits with debug log `llm_prompt_cache_hit_detected`). Extended `1d-T05` with warm-cache fixtures for both Ollama and llama.cpp, and extended `1d-T16` (`pytest -m live_llm`) to record two back-to-back calls and one parse-retry call against `qwen3.5:35b-a3b-q8_0-ctx256k` to empirically confirm KV-cache reporting.
- **`W-13` implementation note** — Noted `changed_at = clock_timestamp()` explicitly on migration `0004`'s `sync_changes` seed inserts (§3.2).

### Verification (`make check`)
- `make check` passed (exit 0): backend `ruff check`/`ruff format`/`mypy` clean, 219 pytest passed (92.94% total branch coverage, all 17 critical modules >= 90%); client-runner 32 passed (94.87% branch coverage); frontend lint/typecheck clean, 62 vitest passed (93.3% lines / 89.21% branches, all critical >= 90% and core >= 80% gates passed).
