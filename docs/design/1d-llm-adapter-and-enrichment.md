# Component Design 1d: Local LLM Adapter, Structured Enrichment, Prompt/Profile Config & Scheduling

> **Status:** **In Design Review** *(Drafted 2026-11-28 on branch `milestone/1d`, base `main` at `5de122b`)*
> **Milestone:** `1d` — Local LLM Adapter, Enrichment & Prompt Config ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-FEED-05`, `FR-FEED-06`, `FR-LLM-01`, `FR-LLM-02`, `FR-LLM-04`, `FR-LLM-05`, `FR-STATE-04`, `FR-SYNC-01`, `FR-SYNC-03`, `FR-UI-01`, `FR-UI-02`, `NFR-SEC-03`, `NFR-SEC-04`, `NFR-SYNC-02`, `NFR-LLM-01`, `NFR-OPS-01`
> **Related ADRs:** [ADR 0001](../decisions/0001-tech-stack-and-container-topology.md), [ADR 0003](../decisions/0003-postgres-backed-job-queue-and-async-llm-pipeline.md), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)

---

## 1. Scope & Boundaries

Milestone `1d` introduces SmartRSS's local LLM enrichment pipeline on top of the verified `1a`–`1c` + `1d-ops` foundation, turning raw extracted articles into structured display metadata (TL;DR, 2–5 takeaways, LLM topic tags, content depth) and calibrated 5-tier curation metadata (relevance tier + "why" rationale) while preserving non-blocking ingestion and detached client reliability.

### In Scope for `1d`
1. **Dual-Runtime `LLMAdapter` (`Ollama` & `llama.cpp`)**: Dedicated operator-configured host-LLM HTTP client (`follow_redirects=False`, 1 MiB response cap, strict timeouts), runtime capability profiles, `<think>` stripping, structured JSON schema validation via Pydantic v2 with bounded parse-retry recovery (`<= 2` retries), normalized `LLMCallMetrics` (`prompt_eval_ms`, `generation_ms`, tokens, tok/s), and cached non-blocking health/model checks.
2. **Database Schema & Reversible Migration `0004`**: New tables `prompt_configs`, `interest_profiles`, `item_enrichments`, `tags`, `item_tags`, plus `app_settings` LLM pause/quiet-hours/cron-window columns; strict `SyncLogWriter` guard coverage on all new synced tables.
3. **Prompt Construction, Precedence & Injection Delimitation**: Shipped versioned default templates (`enrichment_system`, `enrichment_user`) seeded safely without clobbering user edits, per-call randomized XML fence delimiters around untrusted article content, deterministic UTF-8 token budgeting with 75%/25% head+tail article truncation, per-feed `curation_instructions` injection, and canonical `feed_instructions_hash`.
4. **`enrich_item` Worker Stage, Global Concurrency-1 & Scheduling Controls**: Dedicated LLM worker slot separated from I/O workers in both `worker run` and `worker run-once`, cross-process PostgreSQL advisory lock (`LOCK_LLM_SLOT`), `extract_content` → `enrich_item` chaining (respecting `FR-FEED-05` backfill cap and skipping muted/unsubscribed feeds), pause toggle, quiet hours, built-in cron enrichment windows, lease-fenced atomic commit, dead-letter `status='failed'` persistence, `smartrss worker run-once --stage enrich`, and `smartrss llm health`.
5. **REST API, Sync Hydration & FTS Search**: Chunked batch hydration of `enrichment`, `tags[]`, active `interest_profile`, and expanded `app_settings` on `bootstrap` / `pull`; online-only editors (`GET/PUT /api/v1/interest-profile`, `GET/PUT/reset /api/v1/prompts/...`, `POST /api/v1/items/{id}/re-enrich`, `POST /api/v1/feeds/{id}/re-enrich-unread`); FTS indexing of TL;DR, takeaways, and tags + search filters (`tag`, `depth`, `min_tier`); extended `/api/v1/system/health` (LLM connectivity) and `/api/v1/system/metrics` (readiness lag, LLM throughput breakdown, per-feed GPU seconds).
6. **Frontend (`1d` UI Updates)**: Tag filter bar, ReadingPane "Re-enrich" & FeedManager "Re-enrich Unread" actions (online-only), Settings Curation sub-panel (LLM pause/quiet-hours/cron-window controls, Interest Profile Markdown editor with `409` diff, Prompt Templates editor with version history and "View updated default" diff), and System Health LLM connectivity + readiness-lag + throughput cards.
7. **Deterministic Contract Fixtures & `live_llm` Benchmark**: Recorded `Ollama` and `llama.cpp` fixtures in the default `make check` suite + opt-in `pytest -m live_llm` MoE smoke and steady-state throughput benchmark.

### Explicitly Out of Scope (Deferred to Later Milestones)
- **`1e`**: `item_embeddings`, embedding API calls, pre-enrichment URL/cosine clustering, `clusters` table/sync/UI, and `status='inherited'` cluster dedup. *(Note: `LLMAdapter` defines the clean module boundary where `embed()` will be added in `1e`, without adding unused embedding code in `1d`.)*
- **`1f`**: `research_sources`, PyMuPDF PDF extraction, `mini_research` stage, and `research_context` synthesis (`item_enrichments.research_context` column is created nullable in `1d` to match `data-model.md` §4.6 and remains `NULL` until `1f`).
- **`1g`**: `feedback_events`, thumbs up/down UI, offline `feed_instruction` notes, calibration example selection (prompt builder reserves the bounded calibration slot and leaves it empty in `1d`), and offline relevance evaluation harness.

---

## 2. Infrastructure, Security Boundary & Configuration

### 2.1 Environment Variables (`.env.example` & `smartrss/config.py`)

All LLM endpoint configuration is **infrastructure-level** (environment variables only — never editable via REST API or `app_settings`), preventing SSRF pivot via API compromise (`NFR-SEC-04`, ADR 0006):

| Variable | Default | Validation / Notes |
| :--- | :--- | :--- |
| `LLM_RUNTIME` | `ollama` | `Literal["ollama", "llamacpp"]` |
| `LLM_BASE_URL` | `""` *(empty = disabled)* | When non-empty: must parse as `http://` or `https://` with non-empty host, no credentials (`user:pass@`), no query/fragment; trailing slash stripped. Recommended Docker bridge value: `http://host.docker.internal:11434` (Ollama) or `http://host.docker.internal:8081` (`llama-server`). |
| `LLM_CHAT_MODEL` | `qwen3:30b-a3b` | Non-empty stripped string (max 200 chars). Overridable per prompt via `prompt_configs.model_override`. |
| `LLM_API_KEY` | `""` *(optional)* | Optional bearer key for `llama-server --api-key` or authenticated reverse proxy. Sent only in `Authorization: Bearer <key>` header; **never** logged, serialized, or placed in argv (`S3`). |
| `LLM_NUM_CTX` | `8192` | `int`, `2048..65536`. Context window passed to Ollama `options.num_ctx` and used by the prompt token budgeter. |
| `LLM_TEMPERATURE` | `0.1` | `float`, `0.0..2.0`. Low default for deterministic structured JSON output. |
| `LLM_MAX_OUTPUT_TOKENS` | `1024` | `int`, `256..8192`. Passed as Ollama `options.num_predict` / `llama.cpp` `max_tokens`. |
| `LLM_TIMEOUT_SECONDS` | `180` | `int`, `10..600`. Per-call total wall-clock timeout; strictly less than `DEFAULT_LLM_LEASE_SECONDS` (`300`s) while job heartbeat renews every `30`s. |
| `LLM_DISABLE_THINKING` | `true` | `bool`. Requests non-thinking mode (`think: false` / `chat_template_kwargs.enable_thinking: false`) for fast MoE structured JSON; `<think>` tag stripping always stays enabled as defense-in-depth. |
| `LLM_HEALTH_CACHE_SECONDS` | `30` | `int`, `5..300`. TTL for cached LLM health probe results on `GET /api/v1/system/health`. |

### 2.2 Docker Compose Networking (`NFR-SEC-04`)
- `docker-compose.yml` already configures `extra_hosts: ["host.docker.internal:host-gateway"]` on `worker`.
- Add `extra_hosts: ["host.docker.internal:host-gateway"]` to `api` as well, and forward `LLM_*` env vars to both `api` and `worker` so `GET /api/v1/system/health` and `docker compose exec api smartrss llm health` can probe the host LLM identically to `worker`.
- `api` runs with `read_only: true` and `tmpfs: [/tmp]`; `LLMAdapter` and `croniter` perform zero filesystem writes.
- Never bind host `Ollama` or `llama-server` to unrestricted `0.0.0.0` on the LAN (`NFR-SEC-04`); `README.md` documents binding strictly to the Docker bridge gateway IP (e.g., `OLLAMA_HOST=172.17.0.1:11434` or a `ufw` rule allowing only the Compose subnet).

### 2.3 Security Boundary: `LLMAdapter` vs. `SafeHttpClient` (`S2`, `S3`, `S5`, `S6`)
- `SafeHttpClient` intentionally rejects loopback, RFC1918, and Docker bridge CIDRs (`172.16.0.0/12`) so untrusted feed/article URLs can **never** reach the host LLM.
- `LLMAdapter` (`backend/src/smartrss/llm/adapter.py`) is the **sole** allow-listed exception to `S2` (alongside `security/safe_http.py`), strictly hardened for operator-configured host-LLM calls:
  1. Target base URL comes **only** from `Settings.llm_base_url` (env var, immutable at runtime).
  2. `httpx.AsyncClient(follow_redirects=False)` — redirects are **never** followed, preventing a compromised or misconfigured endpoint from pivoting requests to arbitrary LAN/WAN targets.
  3. Streaming body read with a hard **`MAX_LLM_RESPONSE_BYTES = 1_048_576` (1 MiB)** byte cap (`S5`/`S6`); exceeding it aborts the stream and raises `LLMResponseTooLargeError`.
  4. Explicit `httpx.Timeout(connect=10.0, read=timeout_s, write=30.0, pool=10.0)` wrapped in `asyncio.timeout(timeout_s)` so slow-drip response bodies cannot stall the worker indefinitely.
  5. `LLM_API_KEY` (if set) is redacted from exception messages and logs (`S3`).
  6. Add a static guard test (`1d-T01`) grepping `backend/src/smartrss/` to fail CI if any module outside `security/safe_http.py` and `llm/adapter.py` constructs `httpx.AsyncClient` / `httpx.Client` or calls `httpx.get`/`post`/`request` directly.

---

## 3. Database Schema, Migration `0004` & Strict Sync Guard

Alembic migration `backend/alembic/versions/0004_milestone_1d_llm_enrichment.py` (`revision = "0004_milestone_1d_llm_enrich"`, `down_revision = "0003_user_test_metrics_canon"`) adds the `1d` tables and columns with a clean, verified `downgrade()`.

### 3.1 `prompt_configs` (`data-model.md` §4.9)
```sql
CREATE TABLE prompt_configs (
    id               UUID PRIMARY KEY,
    name             TEXT NOT NULL CHECK (name IN ('enrichment_system', 'enrichment_user')),
    version          INT NOT NULL CHECK (version >= 1),
    origin           TEXT NOT NULL CHECK (origin IN ('default', 'user')),
    default_revision TEXT NULL,
    template_text    TEXT NOT NULL,
    model_override   TEXT NULL,
    is_active        BOOLEAN NOT NULL DEFAULT FALSE,
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_prompt_configs_name_version UNIQUE (name, version)
);
CREATE UNIQUE INDEX uq_prompt_configs_active_name
    ON prompt_configs (name) WHERE is_active = TRUE;
```

**Startup Seeding & Non-Clobbering Upgrades (`FR-LLM-04`)**:
- The `name IN ('enrichment_system', 'enrichment_user')` constraint covers all `1d` templates and will be extended in `1f` when `mini_research` prompts are introduced (`data-model.md` §4.9).
- Shipped templates live in `backend/src/smartrss/llm/prompts.py` with explicit revision identifiers (`DEFAULT_SYSTEM_REVISION = "1d-v1"`, `DEFAULT_USER_REVISION = "1d-v1"`).
- `ensure_default_prompts(session)` runs under transaction advisory lock `LOCK_PROMPT_SEED = 0x5352_0005` during API startup, worker startup, and migration `0004`:
  1. For each shipped `(name, revision, template_text)`:
     - If no row exists for `name`: insert `version=1, origin='default', default_revision=revision, is_active=True`.
     - Else if no row exists with `origin='default'` and `default_revision == revision`:
       - Compute `next_version = MAX(version) + 1`.
       - Inspect the currently active row (`is_active = True`):
         - If `active.origin == 'default'`: flip `active.is_active = False` and insert the new default row with `is_active = True` (seamless upgrade when user hasn't customized).
         - If `active.origin == 'user'`: insert the new default row with **`is_active = False`** so **user customizations are never overwritten**. The API computes `has_newer_default = (latest_default.version > active.version)` so the UI surfaces a "View updated default" diff banner.

### 3.2 `interest_profiles` (`data-model.md` §4.9)
```sql
CREATE TABLE interest_profiles (
    id               UUID PRIMARY KEY,
    version          INT NOT NULL UNIQUE CHECK (version >= 1),
    profile_markdown TEXT NOT NULL,
    is_active        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_interest_profiles_active
    ON interest_profiles ((TRUE)) WHERE is_active = TRUE;
```
- Migration `0004` seeds `version = 1, is_active = TRUE` with a concise editable starter Interest Profile Markdown template (describing high/low interest areas) and records a `sync_changes` `upsert` only on runtime updates (fresh bootstrap reads active profile directly).

### 3.3 `item_enrichments` (`data-model.md` §4.6)
```sql
CREATE TABLE item_enrichments (
    id                     UUID PRIMARY KEY,
    item_id                UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    is_current             BOOLEAN NOT NULL DEFAULT TRUE,
    status                 TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'inherited', 'failed')),
    prompt_config_id       UUID NULL REFERENCES prompt_configs(id) ON DELETE SET NULL,
    model_name             TEXT NULL,
    tldr                   TEXT NULL,
    takeaways              JSONB NOT NULL DEFAULT '[]'::jsonb,
    depth_type             TEXT NULL CHECK (
        depth_type IS NULL OR depth_type IN (
            'news-brief', 'deep-dive', 'paper-report', 'opinion', 'release-notes', 'discussion'
        )
    ),
    relevance_tier         SMALLINT NULL CHECK (
        relevance_tier IS NULL OR relevance_tier BETWEEN 1 AND 5
    ),
    relevance_rationale    TEXT NULL,
    research_context       TEXT NULL,
    feed_instructions_hash TEXT NULL,
    prompt_eval_ms         INT NULL,
    generation_ms          INT NULL,
    latency_ms             INT NULL,
    prompt_tokens          INT NULL,
    completion_tokens      INT NULL,
    parse_retries          SMALLINT NOT NULL DEFAULT 0,
    error_message          TEXT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_item_enrichments_current
    ON item_enrichments (item_id) WHERE is_current = TRUE;
CREATE INDEX ix_item_enrichments_item_created
    ON item_enrichments (item_id, created_at DESC);
```
- `prompt_config_id` references the active `enrichment_system` `prompt_configs.id` used during the call (or `NULL` on dead-letter failure before prompt resolution).
- On re-enrichment, the worker locks `items` row `FOR UPDATE`, sets any prior `is_current = TRUE` row to `is_current = FALSE` (`UPDATE item_enrichments SET is_current = FALSE WHERE item_id = :id AND is_current = TRUE`), and inserts the new `is_current = TRUE` row in the same transaction — retaining permanent enrichment history without violating `uq_item_enrichments_current`.

### 3.4 `tags` & `item_tags` (`data-model.md` §4.8)
```sql
CREATE TABLE tags (
    id           UUID PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE item_tags (
    item_id    UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id     UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX ix_item_tags_tag_item ON item_tags (tag_id, item_id);
```
- LLM-generated only in Phase 1 (`FR-LLM-01`).
- Tag upserts use `INSERT INTO tags (id, slug, display_name) VALUES ... ON CONFLICT (slug) DO NOTHING`, followed by `SELECT id, slug FROM tags WHERE slug = ANY(:slugs)` — completely immune to concurrent-insert `IntegrityError` races.
- Re-enrichment replaces an item's tags transactionally (`DELETE FROM item_tags WHERE item_id = :id` then insert new `(item_id, tag_id)` rows) and emits the parent `item` `upsert` in `sync_changes`.

### 3.5 `app_settings` Additions (`1d` Scheduling & Pause Controls)
Added to the singleton `app_settings` row in migration `0004`:

| Column | Type | Default | Constraint / Notes |
| :--- | :--- | :--- | :--- |
| `llm_paused` | `BOOLEAN NOT NULL` | `FALSE` | Hard pause: stops claiming all `enrich_item` jobs when `TRUE`. |
| `llm_quiet_hours_enabled` | `BOOLEAN NOT NULL` | `FALSE` | When `TRUE`, defers background `enrich_item` (`priority >= 50`) during quiet hours; interactive `priority < 50` (manual Re-enrich) still runs. |
| `llm_quiet_hours_start` | `SMALLINT NOT NULL` | `9` | `CHECK (0..23)`, local hour in `enrichment_timezone`. |
| `llm_quiet_hours_end` | `SMALLINT NOT NULL` | `17` | `CHECK (0..23)`, local hour in `enrichment_timezone`; supports overnight wrap-around (e.g., `22` → `7`). |
| `enrichment_schedule_mode` | `TEXT NOT NULL` | `'continuous'` | `CHECK (IN ('continuous', 'cron_window'))`. |
| `enrichment_cron_expression` | `TEXT NOT NULL` | `'0 */2 * * *'` | Validated 5-field cron expression (see §6.3). |
| `enrichment_window_minutes` | `INT NOT NULL` | `60` | `CHECK (5..1440)`. Duration after each cron fire during which daemon background enrichment runs. |
| `enrichment_timezone` | `TEXT NOT NULL` | `'UTC'` | Validated IANA timezone name via stdlib `zoneinfo.ZoneInfo` (e.g., `America/Denver`, `UTC`). |

### 3.6 `SyncLogWriter` Strict Guard Updates (`D2`)
Extend `backend/src/smartrss/db/sync_log.py`:
- Add `"interest_profiles"`, `"item_enrichments"`, and `"item_tags"` to `SYNCED_TABLES`.
- Extend `_entity_requires_sync()`:
  - `InterestProfile` → `("interest_profile", INTEREST_PROFILE_ENTITY_ID)`
  - `ItemEnrichment` → `("item", obj.item_id)`
  - `ItemTag` → `("item", obj.item_id)`
- Add the 8 new `app_settings` columns to `APP_SETTINGS_SYNCED_COLUMNS`.
- *Note*: `prompt_configs` and `tags` (dictionary table) are **not** synced via `sync_changes` (`data-model.md` §5.1); `prompt_configs` is online-only and `tags` is projected through `item_tags` on parent `item` upserts.

---

## 4. `LLMAdapter` Runtime Contract (`Ollama` & `llama.cpp`)

New package: `backend/src/smartrss/llm/`
- `profiles.py` — runtime profiles (`ollama`, `llamacpp`) and capability flags
- `schema.py` — strict Pydantic `EnrichmentOutput`, JSON Schema export, `<think>` / code-fence extraction, tag slug normalization
- `prompts.py` — default templates, `ensure_default_prompts`, token estimator, head+tail truncation, XML nonce delimitation, `feed_instructions_hash`
- `adapter.py` — `LLMAdapter`, `LLMCallMetrics`, `LLMHealthStatus`, error hierarchy, bounded parse-retry loop
- `scheduling.py` — pure scheduling evaluator (`evaluate_enrichment_gate`, quiet hours with wrap-around, cron window via `croniter` + `zoneinfo`)

### 4.1 Runtime Profiles & Request Mapping

| Capability / Field | `ollama` Profile | `llamacpp` Profile |
| :--- | :--- | :--- |
| **Chat Endpoint** | `POST /api/chat` | `POST /v1/chat/completions` |
| **Health / Models Endpoint** | `GET /api/tags` | `GET /health` + `GET /v1/models` |
| **Structured Output** | `format: <JSON Schema dict>` (`structured_output_mode="ollama_json_schema"`) | `response_format: {"type": "json_schema", "json_schema": {"name": "item_enrichment", "strict": true, "schema": <JSON Schema dict>}}` (`structured_output_mode="openai_json_schema"`) |
| **Disable Thinking Param** | `"think": false` (top-level field) | `"chat_template_kwargs": {"enable_thinking": false}` |
| **400 Capability Fallback** | If HTTP `400` occurs while `think` was sent, retry once without `think` and disable `supports_think_param` on the adapter instance (still stripping `<think>` tags). | If HTTP `400` occurs while `chat_template_kwargs` was sent, retry once without it and disable `supports_think_param` on the adapter instance. |
| **Sampling / Context** | `options: {temperature, num_ctx, num_predict}`, `stream: false` | `temperature`, `max_tokens`, `stream: false` |
| **Response Content Path** | `data["message"]["content"]` | `data["choices"][0]["message"]["content"]` |
| **Prompt Tokens** | `data.get("prompt_eval_count")` | `(data.get("usage") or {}).get("prompt_tokens")` |
| **Completion Tokens** | `data.get("eval_count")` | `(data.get("usage") or {}).get("completion_tokens")` |
| **Prompt Eval ms** | `round(data["prompt_eval_duration"] / 1_000_000)` *(ns → ms)* | `round(timings["prompt_ms"])` if present else `None` |
| **Generation ms** | `round(data["eval_duration"] / 1_000_000)` *(ns → ms)* | `round(timings["predicted_ms"])` if present else `None` |
| **Derived `tokens_per_sec`** | `round(completion_tokens / (generation_ms / 1000.0), 2)` when `completion_tokens > 0` and `generation_ms > 0`, else `None` | Same formula (with fallback to `timings.predicted_per_second` if present) |
| **Wall-clock `total_latency_ms`** | Always measured via `time.monotonic()` | Always measured via `time.monotonic()` |

### 4.2 `<think>` Stripping, JSON Extraction & Strict Schema Validation (`schema.py`)

```python
DepthType = Literal[
    "news-brief",
    "deep-dive",
    "paper-report",
    "opinion",
    "release-notes",
    "discussion",
]

class EnrichmentOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=False)

    tldr: str                       # stripped, 1..600 chars
    takeaways: list[str]            # 2..5 stripped non-empty strings, each 1..300 chars
    tags: list[str]                 # 0..6 unique normalized kebab-case slugs (<= 48 chars)
    depth_type: DepthType
    relevance_tier: int             # strict int (bools & non-integers rejected), 1..5
    relevance_rationale: str        # stripped, 1..400 chars
```

Parsing pipeline (`extract_and_validate_enrichment(raw_text, *, require_nonempty_tags: bool)`):
1. **Strip `<think>` blocks**: remove all `<think>...</think>` segments (`re.IGNORECASE | re.DOTALL`). If an unclosed `<think>` appears before a JSON object `{...}`, discard up to the first `{`.
2. **Unwrap Markdown fences**: if wrapped in ```` ```json ... ``` ```` or ```` ``` ... ``` ````, extract inner text.
3. **JSON decode**: `json.loads(cleaned)` — must yield a `dict`.
4. **Field normalization & strict validation**:
   - Reject `bool` or `float` for `relevance_tier` (`isinstance(v, bool)` explicitly rejected before int range `1..5`).
   - Strip whitespace on `tldr`, `relevance_rationale`, and every takeaway; drop empty takeaways; if `> 5` takeaways, keep first 5; if `< 2` takeaways, raise `ValidationError` (triggers parse retry).
   - Normalize `tags`: lowercase, replace spaces/underscores/non-alphanumerics with `-`, collapse consecutive `-`, strip leading/trailing `-`, truncate to 48 chars, filter by `^[a-z0-9][a-z0-9-]{0,47}$`, deduplicate preserving order, cap at 6 tags. On initial attempt (`require_nonempty_tags=True`), require `>= 1` valid tag; on final parse-retry attempt (`require_nonempty_tags=False`), accept `0..6` tags so minor tag omissions never dead-letter an otherwise valid enrichment.
5. **Bounded Parse-Retry Loop (`MAX_PARSE_RETRIES = 2`)**:
   - Attempt `0`: send `[system_msg, user_msg]`.
   - On `json.JSONDecodeError` or `pydantic.ValidationError`: increment `parse_retries` (up to `2`) and send a corrective turn appending the exact Pydantic validation errors + compact reminder of required JSON keys (never forwarding untrusted raw article instructions).
   - Accumulate `prompt_tokens`, `completion_tokens`, `prompt_eval_ms`, `generation_ms`, and `total_latency_ms` across all attempts in the call so `stage_metrics` and `item_enrichments` reflect true GPU cost!
   - If attempt `2` still fails validation, raise `EnrichmentParseError` (carrying `parse_retries=2` and compact error summary).

### 4.3 Error Taxonomy & Health Probe

| Exception | Cause | Worker Handling |
| :--- | :--- | :--- |
| `LLMUnconfiguredError` | `LLM_BASE_URL` is empty | Worker does not claim `enrich_item`; `run-once --stage enrich` exits cleanly with `status="partial"`, `notes="llm_unconfigured"`. |
| `LLMUnavailableError` | Connect error, DNS failure, timeout, HTTP `429`/`502`/`503`/`504`, `llama-server` 503 `"Loading model"` | Retryable via `fail_job` exponential backoff; daemon survives cleanly (`L1`), items stay visible in `pending`. |
| `LLMResponseTooLargeError` | Response body exceeds 1 MiB (`S5`) | Fails job via `fail_job`; dead-letters after `max_attempts`. |
| `EnrichmentParseError` | Schema validation still fails after `MAX_PARSE_RETRIES = 2` | Fails job via `fail_job`; on final attempt / dead-letter writes `ItemEnrichment(status='failed')`. |
| `LLMClientError` | HTTP `400`/`401`/`403`/`404` (e.g., unknown model name, invalid API key) after capability fallback | Fails job via `fail_job` with clear message (API key redacted, `S3`). |

**Cached Non-Blocking Health Probe (`check_llm_health`)**:
- Short timeout (`5.0`s), `follow_redirects=False`, 1 MiB cap, TTL-cached in-process (`LLM_HEALTH_CACHE_SECONDS`, default `30`s) behind an `asyncio.Lock` to coalesce concurrent requests (`L4` bounded to 1 entry).
- Returns `LLMHealthStatus(configured, runtime, model_name, status: "ok" | "unconfigured" | "model_missing" | "unreachable", available_models_count, checked_at, error)`.
- Worker logs one structured startup health check (`llm_startup_health`) without aborting startup if the LLM is offline (`NFR-LLM-01`).

---

## 5. Prompt Construction, Precedence, Token Budgets & Injection Safety

### 5.1 Instruction Precedence & Default Templates

The shipped `enrichment_system` template encodes the exact 5-level precedence hierarchy from `architecture.md` §5.2:
1. **System rubric, output JSON schema, and prompt-injection safety rules** *(immutable — untrusted article text inside the XML data fence can never override instructions, change the output schema, or force a high relevance tier)*.
2. **Per-feed curation instructions** (`feeds.curation_instructions`, plus active `feed_instruction` notes once `1g` adds them) *(authoritative for this feed; refines or overrides generic preferences for this feed within system safety rules)*.
3. **Global Interest Profile** (`interest_profiles.profile_markdown`) *(baseline priorities across all feeds)*.
4. **Calibration Examples** *(reserved empty in `1d`; populated in `1g`)*.
5. **Untrusted Article Data** *(strictly data to analyze, enclosed in per-call randomized XML tags)*.

The 5-tier calibrated relevance rubric in `enrichment_system`:
- `1` — **Skip**: off-topic, clickbait, sponsor/promo, or explicitly excluded by feed/profile instructions.
- `2` — **Low**: tangentially related or shallow news blurb with minimal new signal.
- `3` — **Worth a Look**: solid relevant update, summary, or commentary aligned with interests.
- `4` — **High**: substantive technical deep-dive, major release, or strong match to core interests / feed instructions.
- `5` — **Must Read**: rare breakthrough, landmark paper/postmortem, or direct hit on highest-priority topics (reserve for top ~5–10% of items).

### 5.2 Template Rendering & Prompt-Injection Delimitation (`NFR-SEC-03`)
- **Safe placeholder substitution**: `enrichment_user` supports explicit named placeholders:
  - Required: `{{article_content}}`, `{{article_metadata}}`
  - Optional: `{{feed_instructions}}`, `{{interest_profile}}`, `{{calibration_examples}}`
  - Render using deterministic single-pass regex replacement (never Python `str.format()`!), so literal `{` / `}` characters inside articles, code snippets, or user Markdown never raise `KeyError` or expose object attributes!
  - `PUT /api/v1/prompts/enrichment_user` validates that `{{article_content}}` and `{{article_metadata}}` are present in `template_text` (returning `422` otherwise).
- **Per-call randomized XML fence**:
  - Generate an 8-hex-char nonce (`secrets.token_hex(4)`) per prompt render:
    - `<article_metadata_{nonce}>...</article_metadata_{nonce}>`
    - `<untrusted_article_content_{nonce}>...</untrusted_article_content_{nonce}>`
  - Escape any literal occurrence of the closing tag (case-insensitive) inside the untrusted strings (and strip NUL bytes `\x00`), and tell the model the exact nonce tag name in the system/user prompt framing. Untrusted article HTML/text cannot break out of the fence even if it contains `</untrusted_article_content>`.

### 5.3 Deterministic Token Budgeter & Head+Tail Truncation (`S6`)
- **Estimator**: `estimate_tokens(text) = ceil(len(text.encode("utf-8")) / 3.5)` — fast, zero external tokenizer downloads in air-gapped Docker builds, and conservative on English, code, and CJK.
- **Per-section caps** (for default `LLM_NUM_CTX = 8192`, `LLM_MAX_OUTPUT_TOKENS = 1024`, safety margin `256` → `prompt_budget = 6912` tokens):
  1. `enrichment_system` + JSON schema overhead: measured directly (typical ~600 tokens; hard cap `1,200` tokens).
  2. `feed_instructions` (`feeds.curation_instructions`): capped at **`450` tokens** (head truncation with `[... truncated]`).
  3. `interest_profile` (`interest_profiles.profile_markdown`): capped at **`800` tokens**.
  4. `calibration_examples` (empty in `1d`, populated in `1g`): capped at **`700` tokens**.
  5. `article_metadata` (feed title, item title, author, `raw_url`, `published_at`): capped at **`150` tokens**.
  6. `article_content` (`extracted_text` when `extraction_status == 'ok'` and non-empty, else `strip_html_to_plain_text(raw_summary)`): allocated `min(4200, max(512, prompt_budget - used_tokens))` tokens using **75% head + `\n\n[... middle truncated ...]\n\n` + 25% tail** truncation so both introduction and conclusion survive!
- **`feed_instructions_hash` (`D6`)**:
  - Normalized canonical JSON: `json.dumps({"curation_instructions": feed.curation_instructions.strip(), "active_notes": []}, sort_keys=True, separators=(",", ":"))`.
  - If `curation_instructions.strip() == ""` and `active_notes == []`: store `NULL`; else store `hashlib.sha256(payload.encode("utf-8")).hexdigest()`.

---

## 6. Worker Architecture: `enrich_item`, Concurrency-1, Scheduling & `run-once`

### 6.1 Global LLM Concurrency = `1` (`ADR 0003`, `L5`)
Enforced at **two** levels so the host GPU is never oversubscribed:
1. **Separated Worker Pools**:
   - `IO_JOB_TYPES = ("poll_feed", "extract_content", "expire_items", "retention_cleanup")`
   - `LLM_JOB_TYPES = ("enrich_item",)`
   - `ALL_1D_JOB_TYPES = IO_JOB_TYPES + LLM_JOB_TYPES`
   - `STAGE_TO_JOB_TYPES`: adds `"enrich": ("enrich_item",)` and updates `"all": ALL_1D_JOB_TYPES`.
   - `run_worker_daemon` runs `WORKER_IO_CONCURRENCY` I/O tasks (default `4`) claiming only `IO_JOB_TYPES`, plus **exactly `1` dedicated LLM task** (`_llm_worker_loop`) claiming only `LLM_JOB_TYPES`.
   - `run_worker_once` likewise spawns `concurrency` I/O tasks for non-LLM types in `job_types` and **at most `1` LLM task** when `"enrich_item"` is in `job_types`.
2. **Cross-Process PostgreSQL Session Advisory Lock (`LOCK_LLM_SLOT = 0x5352_0004`)**:
   - Added to `backend/src/smartrss/jobs/locks.py`.
   - Before claiming and executing any `enrich_item` job, the LLM task acquires `advisory_session_lock(engine, LOCK_LLM_SLOT)` on a dedicated checked-out connection (released immediately after the job completes/fails/cancels, and auto-released by Postgres on crash, `L5`). If another process holds `LOCK_LLM_SLOT`, the slot sleeps briefly (daemon) or reports deferred work (`run-once`, `C2`).

### 6.2 Pipeline Chaining & Backfill / Feed-Status Rules (`FR-FEED-05`)
- **`poll_feed` → `extract_content`**:
  - Unchanged for backfill-capped older entries (inserted as `state='expired'`, never enqueued for extraction or enrichment).
  - For newly ingested `unread` entries from `active` feeds: pass `payload={"is_backfill": not feed.initial_backfill_done}` when enqueuing `extract_content` (and `priority=90` if backfill else `50`).
  - For `muted` feeds (whose items auto-mark `state='read'` on poll): `poll_feed` still runs extraction if enabled, and `extract_content` does **not** auto-enqueue `enrich_item` for `state != 'unread'` or `feed.status != 'active'` (saves GPU time on muted feeds; manual "Re-enrich" works on any item regardless of status/state).
- **`extract_content` → `enrich_item`**:
  - Extend `run_extract_content(..., is_backfill: bool = False, run_id: uuid.UUID | None = None)` and `mark_extract_content_failed(session_factory, item_id, *, is_backfill: bool = False, run_id: uuid.UUID | None = None)` (with reaper calls defaulting to `is_backfill=False, run_id=None`) so both pass `is_backfill` and `run_id` into `_set_extraction_status(...)`.
  - Inside `_set_extraction_status()` (same transaction as setting `extraction_status` to `'ok'`, `'fallback_rss'`, or `'failed'`):
    - Query `(Item.feed_id, Item.state, Feed.status)` joining `Feed`.
    - Iff `item.state == 'unread'` and `feed.status == 'active'`, call `enqueue_job(session, job_type="enrich_item", dedupe_key=f"item:{item_id}", feed_id=row.feed_id, item_id=item_id, run_id=run_id, priority=90 if is_backfill else 50, payload={"is_backfill": is_backfill})`.
  - Thus an item with `extraction_status='fallback_rss'` or `'failed'` STILL gets enriched from its RSS `title + raw_summary`!

### 6.3 Pause Toggle, Quiet Hours & Built-In Cron Windows (`smartrss/llm/scheduling.py`)
Pure, deterministic function `evaluate_enrichment_gate(settings_row, *, now_utc: datetime, is_run_once: bool) -> EnrichmentGateDecision`:
- Returns `EnrichmentGateDecision(allow_any: bool, max_priority: int | None, reason: str | None)` where `claim_next_job` adds `AND (CAST(:max_priority AS int) IS NULL OR priority <= CAST(:max_priority AS int))` (`D5`, `C5` DB clock passed in via `SELECT now()`!).
- Exact rules:
  1. **Unconfigured**: `not cfg.llm_base_url` → `allow_any=False, reason="llm_unconfigured"`.
  2. **Hard Pause**: `settings_row.llm_paused is True` → `allow_any=False, reason="llm_paused"` (applies to daemon; for explicit `worker run-once --stage enrich --ignore-pause`? Default: `run-once` also respects `llm_paused` so cron doesn't surprise a user who paused LLM for gaming, and logs `notes="llm_paused"`).
  3. **Quiet Hours** (`settings_row.llm_quiet_hours_enabled is True`, evaluated in `ZoneInfo(settings_row.enrichment_timezone)`):
     - If `start == end`: quiet hours disabled (0-length window).
     - If `start < end`: quiet when `start <= local_hour < end`.
     - If `start > end` (overnight wrap, e.g. `22..7`): quiet when `local_hour >= start or local_hour < end`.
     - When quiet is active in daemon mode → `allow_any=True, max_priority=49, reason="quiet_hours"` (blocks background `50`/`90`, allows interactive manual re-enrich at `10`!).
  4. **Built-In Cron Window** (`settings_row.enrichment_schedule_mode == "cron_window"` and `not is_run_once`):
     - Using `croniter(expr, local_now)` (pure-Python dependency added to `pyproject.toml`), find `prev_fire = croniter.get_prev(datetime)` (or `local_now` if `croniter.match(expr, local_now)`).
     - Window is active iff `(local_now - prev_fire) < timedelta(minutes=enrichment_window_minutes)`.
     - Outside window → `allow_any=True, max_priority=49, reason="outside_cron_window"` (manual `priority=10` still runs immediately; explicit `worker run-once --stage enrich` always bypasses the daemon cron window!).
  5. Otherwise → `allow_any=True, max_priority=None, reason=None`.

### 6.4 `enrich_item` Execution, Lease Fencing & Stale-Read Protection (`C1`, `C3`, `D2`)
1. **Lease & Heartbeat**: `enrich_item` jobs are claimed with `DEFAULT_LLM_LEASE_SECONDS = 300` (5 minutes > `LLM_TIMEOUT_SECONDS = 180`), renewed every `30`s by `_heartbeat_loop`.
2. **Short Read Transaction (before LLM I/O, `C1`)**:
   - Load `Item`, `Feed`, active `InterestProfile`, active `prompt_configs` (`enrichment_system` & `enrichment_user`), and DB `now()`.
   - If item missing → return `EnrichItemOutcome(metric_status="skipped")` (complete job cleanly).
   - If job is background (`priority >= 50`) and `feed.status == 'unsubscribed'` → return `EnrichItemOutcome(metric_status="skipped")` (don't waste GPU on unsubscribed feeds).
   - Commit/close read transaction immediately so no DB connection or row lock is held during 10–60s LLM HTTP inference.
3. **Prompt Build & LLM Call (no DB connection held)**:
   - Assemble prompt via `build_enrichment_prompt(...)` (computing `feed_instructions_hash`).
   - Resolve effective model: `system_prompt.model_override or user_prompt.model_override or cfg.llm_chat_model`.
   - Call `await llm_adapter.enrich(messages, model=effective_model)` (handles `<think>`, schema validation, and up to `2` parse retries).
4. **Single Atomic Lease-Fenced Write Transaction (`C1`, `C3`, `D2`)**:
   - Extend `complete_job` and `fail_job` in `jobs/queue.py` with optional LLM telemetry kwargs (`model_name`, `prompt_config_id`, `llm_prompt_ms`, `llm_gen_ms`, `prompt_tokens`, `completion_tokens`, `readiness_lag_ms`) so `StageMetric` records full LLM metrics directly with zero duplicate rows.
   - `execute_claimed_job` dispatches `enrich_item` to `run_enrich_item(...)` passing `(job, worker_id, claimed_attempt, queue_wait_ms, started_monotonic)` and skips the generic post-stage `complete_job` call when `run_enrich_item` completes atomically:
   - **Step A (Lease Fence FIRST, zero item-lock contention on lost lease)**: Read DB `now()` (`C5`), compute `readiness_lag_ms = max(0, int((db_now - item_fetched_at).total_seconds() * 1000))` (using immutable `item.fetched_at` from step 2), and execute fenced `complete_job(...)` FIRST (`UPDATE jobs SET status='done', ... WHERE id=:id AND status='running' AND locked_by=:worker_id AND attempts=:claimed_attempt`, inserting the enriched `StageMetric`).
     - If `complete_job` returns `False` (`rowcount == 0`, lease reclaimed by another worker during a slow call): **immediately rollback** without touching `items`, `item_enrichments`, `item_tags`, `stage_metrics`, or `sync_changes`!
   - **Step B (Lock Item & Persist Enrichment in Same Transaction)**:
     - Lock `Item` `FOR UPDATE` (`SELECT id, title, author, raw_summary, extracted_text FROM items WHERE id = :id FOR UPDATE`). If `Item` vanished, commit job completion cleanly.
     - Flip previous current enrichment: `UPDATE item_enrichments SET is_current = FALSE WHERE item_id = :id AND is_current = TRUE` (via `execute_synced(..., lambda _r: [SyncEvent("item", item_id, "upsert")])`).
     - Insert new `ItemEnrichment(is_current=True, status='completed', ...)` with all metadata, `feed_instructions_hash`, phase timings, token counts, and `parse_retries`.
     - Upsert `tags` (`ON CONFLICT (slug) DO NOTHING`), replace `item_tags` for `item_id`.
     - Rebuild `items.search_vector` via `rebuild_item_search_vector(...)` including `tldr`, `takeaways`, and `tags`!
     - Call `await sync_log.record(session, [SyncEvent("item", item_id, "upsert")])` and `await session.commit()`.
   - Update `_RunCounters.items_enriched += 1` and `_RunCounters.total_llm_ms += metrics.total_latency_ms` (persisted via `finalize_pipeline_run` into `pipeline_runs.items_enriched` and `pipeline_runs.total_llm_ms`).
5. **Failure & Dead-Letter Hook (`mark_enrich_item_failed`)**:
   - On `LLMUnavailableError` / `EnrichmentParseError` / `LLMResponseTooLargeError` / `LLMClientError` / unexpected `Exception`: call fenced `fail_job(...)` (recording `StageMetric(status='failed', model_name=..., prompt_config_id=..., ...)`).
   - Extend `ReapResult` in `jobs/queue.py` with `dead_lettered_enrich_items: tuple[uuid.UUID, ...]` so stale-lease reaper dead-letters invoke the same hook.
   - When `fenced_ok and dead_lettered` (or via `ReapResult.dead_lettered_enrich_items`): `mark_enrich_item_failed` opens a short transaction, locks `Item` `FOR UPDATE`, and checks whether an `is_current=True, status='completed'` enrichment already exists:
     - **If an `is_current=True, status='completed'` row already exists** (e.g., failed manual re-enrich): insert the failed attempt with `is_current=False, status='failed', error_message=...` (preserving the existing valid TL;DR/tags/rationale while logging the failure in history and the dead-letter queue; emit `item` `upsert` via `SyncLogWriter` so `sync_log` guard stays satisfied).
     - **Otherwise**: flip any prior non-completed `is_current=True` row to `FALSE`, insert `is_current=True, status='failed', error_message=...`, and emit `item` `upsert` so clients clear `pending` and render the failure state!

### 6.5 CLI Additions (`backend/src/smartrss/cli.py`)
- `smartrss worker run-once --stage enrich` (and `--stage all` now includes `enrich_item`; `RunOnceSummary` adds `items_enriched` and `total_llm_ms`, persisted to `pipeline_runs`). `run-once` acquires both `LOCK_RUN_ONCE` and `LOCK_LLM_SLOT` (shared with the daemon) so concurrent manual `run-once --stage enrich` and daemon never exceed global LLM concurrency `1`.
- `smartrss llm health [--json]` — runs `check_llm_health(force=True)`, prints runtime/model/status/available count (or JSON), exits `0` when `status == "ok"`, `1` otherwise.
- `smartrss settings get` / `set` — extended to read/write the 8 new `app_settings` columns with strict validation (`croniter.is_valid`, `ZoneInfo`, hour bounds `0..23`, window bounds `5..1440`, emitting `app_settings` `upsert` via `SyncLogWriter`).

---

## 7. REST API, Sync Engine & FTS Search Updates

### 7.1 Sync Hydration (`backend/src/smartrss/sync/service.py`)
- **`sync_bootstrap` & `sync_pull`**:
  - Add batch helper `_hydrate_items_enrichments_and_tags(session, item_ids)`:
    - Chunked at `HYDRATION_CHUNK_SIZE = 500` (`D1`, `L3`) — zero N+1 queries!
    - Query 1: `SELECT * FROM item_enrichments WHERE item_id = ANY(CAST(:ids AS uuid[])) AND is_current = TRUE` (`D5`).
    - Query 2: `SELECT it.item_id, t.slug FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_id = ANY(CAST(:ids AS uuid[])) ORDER BY t.slug ASC`.
  - Populate `SyncItemDTO.enrichment` (`ItemEnrichmentDTO`: `status`, `tldr`, `takeaways`, `depth_type`, `relevance_tier`, `relevance_rationale`, `research_context`, `model_name`, `created_at`) and `SyncItemDTO.tags` (`list[str]`).
  - Hydrate active `InterestProfile` (`SyncInterestProfileDTO(version, profile_markdown)`) on `sync_bootstrap` Page 1 and on `sync_pull` when `entity_type == "interest_profile"`.
  - Include all 8 new LLM scheduling/pause columns in `SyncAppSettingsDTO`.

### 7.2 PostgreSQL FTS Search (`backend/src/smartrss/search/fts.py`)
- Extend `SearchVectorInput` with `tldr: str | None = None`, `takeaways: Sequence[str] = ()`, and `tags: Sequence[str] = ()`.
- Updated `_UPDATE_SEARCH_VECTOR_SQL` weights (`FR-STATE-04`):
  - `A`: `title`
  - `B`: `author` + `tags` (space-joined slugs) + `tldr`
  - `C`: `takeaways` (space-joined) + `raw_summary` (plain text)
  - `D`: `extracted_text[:64KB]`
- `reindex_all_search_vectors` (`smartrss search reindex`) joins current `item_enrichments` and `item_tags`/`tags` in batches of `500` so full reindexes preserve enriched search terms; `_set_extraction_status` also loads any existing current enrichment/tags when rebuilding `search_vector` so re-extracting an already-enriched item never clobbers enriched FTS terms.
- `GET /api/v1/search` adds optional query params `tag: str | None`, `depth: DepthType | None`, `min_tier: int | None = Query(None, ge=1, le=5)`, and hydrates `enrichment` + `tags` on `SearchItemDTO` results.

### 7.3 New & Extended Online-Only REST Endpoints

| Endpoint | Behavior & Contracts |
| :--- | :--- |
| `GET /api/v1/interest-profile` | Returns `{version, profile_markdown, created_at}` for the active profile. |
| `PUT /api/v1/interest-profile` | Body: `{expected_version: int, profile_markdown: str (max 32,768 chars)}`. Locks active row `FOR UPDATE`; if `expected_version != active.version`, returns `409 Conflict` with `{detail: "Version conflict", current_version, profile_markdown}`; else flips old `is_active=False`, inserts `version = active.version + 1, is_active=True`, records `SyncEvent("interest_profile", INTEREST_PROFILE_ENTITY_ID, "upsert")`, commits, returns `200`. |
| `GET /api/v1/prompts` | Returns list of active prompts (`enrichment_system`, `enrichment_user`), each with `{id, name, version, origin, default_revision, template_text, model_override, notes, created_at, has_newer_default, latest_default_version, latest_default_template_text}`. |
| `GET /api/v1/prompts/{name}/versions` | Returns version history (`ORDER BY version DESC`, limit 50) for `enrichment_system` or `enrichment_user` (`404` on unknown name). |
| `PUT /api/v1/prompts/{name}` | Body: `{expected_version: int, template_text: str (1..32,768 chars), model_override?: str | null, notes?: str}`. Validates required placeholders for `enrichment_user` (`422` if missing `{{article_content}}` or `{{article_metadata}}`). Locks active row for `name` `FOR UPDATE`; `409 Conflict` with current server row on version mismatch; else inserts new `version = max_version + 1, origin='user', is_active=True` and commits. |
| `POST /api/v1/prompts/{name}/reset-default` | Locks active row `FOR UPDATE`; activates the latest `origin='default'` row for `name` (no-op if already active), returns active prompt. |
| `POST /api/v1/items/{item_id}/re-enrich` | `404` if item missing. Enqueues `enrich_item` (`dedupe_key=f"item:{item_id}"`, `priority=10`, `bump_existing=True`). Honors `C4`: if an active `running` job already exists, returns `{queued: false, active_job_id, active_job_status: "running"}` truthfully rather than falsely claiming queued. |
| `POST /api/v1/feeds/{feed_id}/re-enrich-unread` | Optional query/body `limit: int = 50` (`1..200`). `404` if feed missing. Enqueues `enrich_item` at `priority=90` for up to `limit` newest `unread` items in that feed (`D1` bounded). Returns `{enqueued_count, already_active_count}`. |
| `GET / PATCH /api/v1/settings` | Extended with the 8 `1d` LLM scheduling/pause fields; validates `enrichment_cron_expression` via `croniter.is_valid`, `enrichment_timezone` via `ZoneInfo`, hour/window bounds (`422` on invalid); emits `app_settings` `upsert` on change. |
| `GET /api/v1/system/health` | Adds `llm: LLMHealthStatus` (cached non-blocking probe, never delays `/system/health` > 5s even when Ollama is down). |
| `GET /api/v1/system/metrics?window=24h\|7d\|30d` | Adds `readiness_lag: {count, avg_ms, p50_ms, p95_ms, max_ms}`, `llm_breakdown: [{model_name, prompt_config_id, calls, ok_calls, failed_calls, avg_prompt_ms, avg_gen_ms, avg_tokens_per_sec, total_prompt_tokens, total_completion_tokens, parse_retry_rate}]`, and `llm_duration_ms` / `daily_gpu_seconds` on `per_feed`. |

---

## 8. Frontend (`1d` UI Updates)

`1c` already renders `item.enrichment` (tier, depth, rationale, TL;DR, takeaways, model name), `item.tags`, relevance sorting, tier/depth filters, `localSearch.ts` indexing of `tldr`/`takeaways`/`tags`, and `FeedManagerView` `curation_instructions` 409 diff. `1d` completes the user-facing experience:

1. **Reader Tag Filter & Manual Re-enrich**:
   - Clicking any LLM tag pill in `ItemList` or `ReadingPane` filters by that tag (with a dismissible active tag chip in the filter bar); works both online (passed to `GET /api/v1/search?tag=...` when searching archive) and detached (filtered locally in Dexie/`localSearch.ts`).
   - Add **"Re-enrich"** button in `ReadingPane` (calls `POST /api/v1/items/{id}/re-enrich`, surfaces toast for queued vs already running) and **"Re-enrich Unread"** button in `FeedManagerView` (calls `POST /api/v1/feeds/{id}/re-enrich-unread`). Both are clearly disabled with `"Connect to home LAN to trigger re-enrichment"` when detached (`FR-SYNC-03`).
2. **Curation & LLM Controls in `SettingsDevicesView`** (online-only when connected; read-only cached view of Interest Profile & Settings when detached):
   - **LLM Scheduling & Pause**: Toggle `Pause LLM Enrichment`, toggle `Quiet Hours` + start/end hour selectors + timezone input, toggle `Continuous` vs `Cron Window` + cron expression + window minutes.
   - **Interest Profile Editor**: Markdown `<textarea>` showing current version (`vN`), Save button with optimistic `expected_version`, and `409 Conflict` resolution panel showing server vs. local text ("Reload server version" / "Overwrite with mine").
   - **Prompt Templates Editor**: Tabs for `enrichment_system` and `enrichment_user`, version history dropdown, `"View updated default"` diff banner whenever `has_newer_default === true`, `"Reset to default"` button, and `409 Conflict` resolution panel.
   - **Safe Rendering**: All LLM outputs (`tldr`, `takeaways`, `relevance_rationale`, `tags`) continue to render as React text nodes (never `dangerouslySetInnerHTML`, `NFR-SEC-03`).
3. **System Health & Pipeline Metrics (`SystemHealthView`)**:
   - **LLM Runtime Card**: Displays `runtime`, `model_name`, status badge (`ok` green, `unconfigured` slate, `model_missing` amber, `unreachable` red), and error hint.
   - **End-to-End Readiness Lag Card**: `p50`, `p95`, `avg`, `max` from `fetched_at` → `enriched_at`.
   - **LLM Throughput Breakdown Table**: Grouped by model & prompt version (`calls`, `avg prompt ms`, `avg gen ms`, `tok/s`, total tokens, `parse_retry_rate`), plus `GPU sec/day` on the Per-Feed Cost table.
4. **Normalizers & Dexie (`api/normalize.ts`, `types/sync.ts`, `types/api.ts`)**:
   - Normalize `item.enrichment.created_at` to canonical UTC ISO (`toCanonicalIso`, `U-07`/`D6`), accept the 8 new `SyncAppSettingsDTO` fields with safe defaults for older cached rows (`DEFAULT_APP_SETTINGS`), and normalize `SystemHealthResponse.llm` + `SystemMetricsResponse.{readiness_lag, llm_breakdown}`.

---

## 9. Security & Review-Checklist Self-Check

Walked against [docs/review-checklist.md](../review-checklist.md):

| Check | How `1d` Satisfies It |
| :--- | :--- |
| **S1 / S5 / S6** (Untrusted input & caps) | LLM response streams capped at 1 MiB before JSON parse (`S5`/`S6`); prompt token budgeter uses UTF-8 byte length (`S6`); safe regex placeholder substitution avoids `str.format()` attacks; randomized XML nonce tags delimit untrusted article text (`NFR-SEC-03`). |
| **S2** (Outbound HTTP boundary) | `SafeHttpClient` remains mandatory for all feed/article fetches; `llm/adapter.py` is the sole operator-configured exception (`Settings.llm_base_url` only, `follow_redirects=False`), enforced by static guard test `1d-T01`. |
| **S3** (Secrets hygiene) | Optional `LLM_API_KEY` sent only via HTTP header, never logged, never in argv, never returned by API. |
| **C1 / C3 / C4 / C5** (Concurrency & fencing) | `enrich_item` releases DB connection during LLM I/O, fences `complete_job` `UPDATE` FIRST inside the write tx (rolling back completely on lease loss, `C1`/`C3`), returns truthful active-job status on `/re-enrich` (`C4`), and uses PostgreSQL `now()` for scheduling & `readiness_lag_ms` (`C5`). |
| **L1 / L2 / L4 / L5** (Long-running workers) | Dedicated LLM loop catches non-`CancelledError` exceptions and backs off (`L1`); SIGTERM cancels cleanly and releases both job lease and `LOCK_LLM_SLOT` (`L2`, `L5`); LLM health cache holds at most 1 entry (`L4`). |
| **D1 / D2 / D5 / D6** (DB & sync hygiene) | Sync hydration chunks at 500 IDs (`D1`); `item_enrichments`, `item_tags`, and `interest_profiles` added to strict `SyncLogWriter` guards (`D2`); raw SQL uses `CAST(:param AS uuid[])` (`D5`); `feed_instructions_hash` uses canonical sorted JSON (`D6`). |
| **T3 / T6 / T7** (Gates, concurrency & real exceptions) | 5 new critical modules added to `CRITICAL_MODULES` (`>= 90%`) in `scripts/check-critical-coverage.py` (`T3`); forced-overlap tests cover `LOCK_LLM_SLOT` concurrency-1 and lease-loss rollback (`T6`); adapter boundary fakes raise real `httpx.ConnectError` / `httpx.ReadTimeout` / `httpx.HTTPStatusError` (`T7`). |
| **B1 / B6** (Client reliability) | Enriched fields flow strictly through server-confirmed `items_base` → `display` reconciliation (`B1`); online-only editors never enqueue offline mutations and disable cleanly when detached. |

---

## 10. Verification, Coverage Gates & Test Plan

### 10.1 Critical-Module Coverage Gate (`make check`, `T3`)
Add these 5 new backend modules to `CRITICAL_MODULES` (`>= 90%` combined line+branch coverage) in `scripts/check-critical-coverage.py` in the implementation commit:
- `src/smartrss/llm/adapter.py`
- `src/smartrss/llm/schema.py`
- `src/smartrss/llm/prompts.py`
- `src/smartrss/llm/scheduling.py`
- `src/smartrss/worker/stages/enrich_item.py`

Frontend critical (`>= 90%`) and core (`>= 80%`) gates in `scripts/check-frontend-coverage.mjs` remain enforced (including `src/api/normalize.ts` and `src/search/localSearch.ts`).

### 10.2 Automated Test Matrix (`1d-T01`–`1d-T16`)

| Test ID | Layer | Invariants & Scenarios Verified |
| :--- | :--- | :--- |
| **`1d-T01`** | Unit (`pytest`) | Static `S2` boundary guard: verifies no module outside `security/safe_http.py` and `llm/adapter.py` uses `httpx` directly; verifies `Settings` rejects non-HTTP(S) / credential-bearing `LLM_BASE_URL` and redacts `LLM_API_KEY` from logs/errors (`S3`). |
| **`1d-T02`** | Unit (`pytest`) | `EnrichmentOutput` schema & `<think>` extraction: valid JSON, `<think>...</think>` stripping, unclosed `<think>`, markdown fenced JSON, rejecting `bool`/`float`/out-of-range `relevance_tier`, `<2` takeaways rejection, `>5` takeaways clamp, tag slug normalization/dedup, and final-retry empty-tag allowance. |
| **`1d-T03`** | Unit (`pytest`) | Prompt assembly, token budgeter & injection safety (`NFR-SEC-03`, `S6`, `D6`): 5-level precedence sections present, randomized XML nonce delimiter escapes embedded closing tags, `{braces}` in article/profile text never break rendering, 75%/25% head+tail truncation keeps intro + conclusion within `LLM_NUM_CTX`, and `feed_instructions_hash` is canonical (and `NULL` when empty). |
| **`1d-T04`** | Unit (`pytest`) | Scheduling gate (`llm/scheduling.py`): `llm_unconfigured`, `llm_paused`, quiet hours normal (`9..17`) and overnight wrap (`22..7`) in non-UTC `ZoneInfo` timezone (blocks `priority >= 50`, allows `priority = 10`), `cron_window` inside vs. outside window + `is_run_once=True` bypass, and invalid cron/timezone validation. |
| **`1d-T05`** | Boundary (`pytest`) | Dual-runtime `LLMAdapter` contract fixtures (`Ollama` & `llama.cpp`, `T7`): recorded fixtures for valid JSON + ns/ms timing normalization, `<think>` stripping, HTTP 400 `think`/`chat_template_kwargs` capability fallback, malformed JSON → bounded `MAX_PARSE_RETRIES=2` recovery + accumulated metrics, >1 MiB streaming response abort (`LLMResponseTooLargeError`), redirect refused (`follow_redirects=False`), real `httpx.ConnectError`/`ReadTimeout`/HTTP 503 → `LLMUnavailableError`, and health probe (`ok`, `model_missing`, `unreachable`, TTL cache). |
| **`1d-T06`** | Integration (Real PG) | Alembic `0004` upgrade → downgrade → upgrade on scratch DB; `ensure_default_prompts` idempotency, default revision auto-upgrade when `origin='default'`, and **non-clobbering** default insertion (`is_active=False`, `has_newer_default=True`) when active prompt is `origin='user'`. |
| **`1d-T07`** | Integration (Real PG) | Strict `SyncLogWriter` guard (`D2`) on `interest_profiles`, `item_enrichments`, `item_tags`, and new `app_settings` columns: unlogged mutations raise `MissingSyncEventError`; properly recorded mutations succeed. |
| **`1d-T08`** | Integration (Real PG) | End-to-end `extract_content` → `enrich_item` pipeline: active feed `unread` item chains from `ok` / `fallback_rss` / dead-lettered `failed` extraction into `enrich_item`, persists `ItemEnrichment(is_current=True)` + `tags`/`item_tags` + `stage_metrics` (with `readiness_lag_ms` & LLM timings) + `sync_changes` `item` `upsert`; backfill-capped `expired` items and `muted` feed items are NOT auto-enriched (`FR-FEED-05`). |
| **`1d-T09`** | Integration (Real PG, `T6`) | Forced-overlap concurrency & lease fencing (`C1`, `C3`): (a) two concurrent workers racing on `LOCK_LLM_SLOT` prove strictly 1 in-flight LLM call at any time (asserted via barrier + max-in-flight counter == 1); (b) lease stolen mid-LLM-call causes fenced `complete_job` to return 0 rows and roll back `item_enrichments`/`item_tags`/`sync_changes` completely. |
| **`1d-T10`** | Integration (Real PG) | Failure, dead-letter & re-enrichment lifecycle: (a) `LLMUnavailableError` backs off without crashing daemon and item stays visible `pending`; (b) exhausted retries / reaper dead-letter sets `ItemEnrichment(status='failed')` + emits `item` `upsert` when no prior completed enrichment exists, while a failed manual re-enrich preserves an existing `is_current=True, status='completed'` row; (c) successful re-enrich flips old row `is_current=False`, replaces tags, updates FTS, and emits `item` `upsert`. |
| **`1d-T11`** | Integration (Real PG) | `worker run-once --stage enrich` & `--stage all` (`FR-LLM-05`) + `smartrss llm health` + `smartrss settings set`: verifies `pipeline_runs.items_enriched` & `total_llm_ms`, `RunOnceSummary` JSON output, clean handling when `LLM_BASE_URL` is unconfigured or `llm_paused=True`, and exit codes. |
| **`1d-T12`** | Integration (Real PG) | Sync hydration & FTS search: `sync_bootstrap` and `sync_pull` hydrate `enrichment`, `tags[]`, active `interest_profile`, and 8 new `app_settings` fields in chunked batches; `GET /api/v1/search` matches terms inside `tldr`, `takeaways`, and `tags` and filters by `tag`, `depth`, `min_tier`; `smartrss search reindex` preserves enriched search vectors. |
| **`1d-T13`** | Integration (Real PG) | Management REST APIs: `GET/PUT /api/v1/interest-profile` (`409` conflict + `sync_changes`), `GET/PUT/reset /api/v1/prompts` (`409` conflict + placeholder `422`), `POST /api/v1/items/{id}/re-enrich` (`C4` running vs queued), `POST /api/v1/feeds/{id}/re-enrich-unread`, `PATCH /api/v1/settings` validation (`422` bad cron/timezone), and `/api/v1/system/{health,metrics}` LLM status, readiness lag, and model breakdown. |
| **`1d-T14`** | Frontend Unit (`vitest`) | `normalizeSyncItem` (`enrichment.created_at` canonical UTC), `normalizeSyncAppSettings` (1d fields + legacy fallback), `normalizeSystemHealth` / `normalizeSystemMetrics`, and `localSearch.ts` tag/tldr/takeaways search & tag filter behavior. |
| **`1d-T15`** | Frontend E2E (Playwright) | Enriched reader & management flow against real API/PG + stubbed local LLM: item displays tier/rationale/TL;DR/takeaways/tags, clicking tag filters list (and survives offline reload), Interest Profile & Prompt edit + `409` conflict diff works online and disables cleanly when detached, and System Health renders LLM status + readiness lag + throughput breakdown. |
| **`1d-T16`** | Opt-in `pytest -m live_llm` | Real host-native `Ollama` / `llama.cpp` MoE smoke & throughput benchmark (`10` diverse recorded articles): verifies 100% valid `EnrichmentOutput` schema compliance, records `prompt_eval_ms`, `generation_ms`, `tokens_per_sec`, `parse_retries`, and projects daily GPU time for 200 items/day against the `< 85 min/day` budget (`architecture.md` §5.1). *(Default `make check` excludes `live_llm` via `[tool.pytest.ini_options] addopts = ["-m", "not live_llm"]`, overridden when explicitly invoking `pytest -m live_llm`.)* |

---

## 11. Owner Decisions Needed Before Approval

1. **`Q-1` — Default MoE chat model & runtime in `.env.example` (resolves [Requirements §7 #3](../requirements.md#7-open-questions))**:
   - **Recommended**: Ship `LLM_RUNTIME=ollama`, `LLM_BASE_URL=` *(empty/opt-in until operator points it at host bridge)*, `LLM_CHAT_MODEL=qwen3:30b-a3b`, `LLM_DISABLE_THINKING=true`, `LLM_NUM_CTX=8192` as defaults (with documented `llama.cpp` / alternate model examples in `.env.example` and `README.md`), and benchmark `qwen3:30b-a3b` (or your preferred pulled model) in `1d-T16` (`pytest -m live_llm`).
   - *Alternative*: Specify a different default runtime (`llamacpp`) or default model tag you already run on your Ryzen 395+ host.
2. **`Q-2` — Auto-enrichment on `muted` feeds & failed re-enrichment preservation**:
   - **Recommended**: (a) Do **not** auto-enqueue `enrich_item` for `muted` feeds (whose items auto-mark `read`) or backfill-capped `expired` items, while allowing manual "Re-enrich" on any item; (b) if a manual re-enrich fails permanently after an item already has a `completed` enrichment, keep the existing `completed` enrichment as `is_current=True` and record the failed attempt with `is_current=False` so good display metadata is never wiped.
   - *Alternative*: Auto-enrich `muted` feeds as well, or overwrite `is_current` with `status='failed'` on failed re-enrich.
3. **`Q-3` — Cron schedule parser dependency**:
   - **Recommended**: Add pure-Python `croniter` to `backend/pyproject.toml` for `enrichment_cron_expression` validation and DST-safe `zoneinfo` window computation.
   - *Alternative*: Implement a minimal in-repo 5-field cron matcher with zero extra dependencies.

---

## 12. Documentation Sync Plan (Same Commit as Implementation)

When `1d` is implemented:
- `.env.example` & `docker-compose.yml`: Add `LLM_*` configuration variables and `api.extra_hosts` (`host.docker.internal:host-gateway`).
- `README.md`: Document host-native `Ollama` / `llama.cpp` Docker-bridge binding (`NFR-SEC-04`), `smartrss llm health`, `smartrss worker run-once --stage enrich`, `pytest -m live_llm`, and LLM scheduling controls.
- `docs/requirements.md` §7: Mark Open Question #3 (default candidate MoE model) resolved with the owner's `Q-1` choice.
- `docs/data-model.md` §4.9: Sync the exact `app_settings` `1d` scheduling/pause columns.
- `docs/design/1d-llm-adapter-and-enrichment.md`, `docs/design/README.md`, `docs/README.md`, and `AGENTS.md`: Advance `1d` status through `Implemented` → `In Implementation Review` → (owner) `Implementation Approved`.
