# Design Review — Initial Requirements, Architecture, Data Model & ADRs

- **Date**: 2026-11-22
- **Scope**: `docs/requirements.md`, `docs/architecture.md`, `docs/data-model.md`, `docs/decisions/0001–0003`, `docs/development-process.md`, `AGENTS.md` (as of commit `2599cdb`)
- **Reviewer**: Claude Code (at the project owner's request)
- **Status**: Open. Resolve every item below before starting the detailed component designs in `docs/design/`.

## How to use this document

Each finding has an ID, a severity, the affected docs, the problem, and a recommended resolution. Use your judgment where you disagree with a recommendation, but record why.

| Severity | Meaning |
| :--- | :--- |
| **Blocker** | A correctness or feasibility flaw in a core invariant. Fix it before any component design builds on it. |
| **Major** | A significant gap or ambiguity that would force rework in the component designs. |
| **Minor** | A consistency, clarity, or hygiene issue. |

When you address an item, fill in its `Resolution:` line with what changed and where (doc and section, or a new ADR), or with the reason it was declined. Items in **Section 5 (Open Questions for the Owner)** need the project owner's answer. Don't assume an answer to them; propose a default and mark it *pending owner confirmation*.

---

## 1. Blockers

### R-01 — `sync_seq` from `nextval()` can silently drop records during pull
- **Docs**: `data-model.md` §5 (line 186), `development-process.md` §2.4, ADR 0002
- **Problem**: A PostgreSQL sequence hands out values at call time, but transactions commit in a different order. Say transaction A takes `sync_seq = 100` and commits late, while transaction B takes `101` and commits first. A client that pulls between the two commits sees `101` and advances its cursor past `100`, so it never receives row `100`. Under concurrent API and worker writes (enrichment, clustering, expiration, and push all write together) this will happen in practice. It breaks the stated invariant that pagination "never drops or duplicates records".
- **Recommendation**: Pick one fix and document it in a new ADR:
  1. **Safe watermark**: Also record `pg_current_xact_id()` per change, or use a change-log table. Have pull return rows only up to a "stable" watermark below the oldest in-flight transaction (`pg_snapshot_xmin(pg_current_snapshot())`).
  2. **Serialized counter**: Allocate `sync_seq` from a single-row counter updated under a row lock at the end of the transaction. This serializes all writers, which is acceptable at this scale.
  3. **Change-log table**: Write an append-only `sync_changes(seq, entity_type, entity_id, op)` row in the same transaction as each change, with one of the two ordering fixes above applied to it. This also solves R-02.
  
  Add an integration test that interleaves two transactions so they commit out of order and asserts that no row is skipped.
- **Resolution**:

### R-02 — Downstream sync has no complete definition of tombstones, deletions, or child-row changes
- **Docs**: `data-model.md` §3, §5
- **Problem**: Only `categories` has `deleted_at`. Several changes that clients must see have no `sync_seq` or tombstone path:
  - `item_enrichments` (new or re-processed enrichment) has no `sync_seq`. The docs imply the parent item's seq gets bumped but never say so.
  - `item_tags` (a user adds or removes a tag, or a re-enrichment changes tags) has no `sync_seq` and no deletion signal.
  - `tags` and `prompt_configs` have no `sync_seq`, while `interest_profiles` does. It's unclear which configuration clients receive at all.
  - Cluster merges and splits: when items move between clusters, or a cluster becomes empty, nothing defines how the client removes the old cluster.
  - `feedback_events`: it isn't stated whether these sync down so a second device can show that a thumbs-up was already given.
- **Recommendation**: Add a "Sync Surface" table listing every entity that is synced down, its parent or aggregate, how a change is signalled, and how a deletion is signalled. Consider syncing **aggregates**: an item record that embeds its current enrichment, tags, and research-source summaries, with any change to a child bumping the item's `sync_seq`. That keeps the protocol much simpler than syncing every table separately. A change-log table (R-01 option 3) gives you deletions for free.
- **Resolution**:

### R-03 — Phase 1 detached mode is not achievable as designed (plain-HTTP LAN origin)
- **Docs**: ADR 0002, `architecture.md` §2 and §3.4, `requirements.md` §4.6
- **Problem**: The Phase 1 client is an SPA served from the home server over plain LAN HTTP (for example `http://192.168.1.10`). When the laptop leaves the LAN:
  - **Service Workers require a secure context** (HTTPS or `localhost`). Without one, nothing caches the app shell, so the SPA cannot load at all when the server is unreachable, and IndexedDB data is useless if the page can't load.
  - `navigator.storage.persist()` also requires a secure context. Without persistent storage, the browser may **evict IndexedDB under storage pressure, including the unsynced outbox**. That means silent loss of user mutations.
  - IndexedDB is scoped to the origin. If the server's IP or hostname changes, the whole cache and outbox are orphaned.
  
  ADR 0002 argues against HTTPS to avoid certificate friction on Android, but it doesn't address what this means for the Phase 1 desktop client.
- **Recommendation**: Decide in an ADR how the Phase 1 client gets a secure, stable origin. Options:
  - **(a)** Serve the SPA from `http://localhost` on the client machine: a tiny local static server, or a desktop shell such as Tauri or Electron. `localhost` counts as a secure context, and the shell talks to the LAN API, which needs CORS configured.
  - **(b)** HTTPS on the LAN with a local CA (for example Caddy's internal CA, or a real domain with a DNS-01 certificate).
  - **(c)** A browser flag such as `unsafely-treat-insecure-origin-as-secure`. Fragile; not recommended.
  
  Option (a) also lines up with a Capacitor-based Phase 2. Whichever you choose, require `navigator.storage.persist()` and fixed server addressing (hostname or mDNS), and add a Playwright test: load the app, go offline, reload, and confirm the app works and the outbox survives.
- **Resolution**:

### R-04 — No authentication or LAN attack-surface protection
- **Docs**: `requirements.md` §2 (Non-Goals), `architecture.md` §3.1 and §6
- **Problem**: "Single-user" has been read as "no auth". Any device on the home Wi-Fi (guests, IoT devices) can read the full reading history, unsubscribe feeds, and rewrite prompts. Worse, web pages the user opens *from feeds* run in the same browser that can reach the LAN API:
  - **CSRF**: A malicious page can send simple cross-origin `POST`s, such as form posts, to `http://smartrss.lan/api/v1/...`.
  - **DNS rebinding**: A hostile domain can rebind to the LAN IP and read API responses.
- **Recommendation**: Add a minimal security section to the requirements and architecture:
  - A per-device bearer token issued at pairing (for example via a QR code or one-time code). Single user still doesn't require accounts.
  - Strict `Host` header allow-list (prevents DNS rebinding).
  - Require JSON content type plus the token header on every mutation (prevents CSRF).
  - A CORS allow-list.
  
  Record this as an ADR, since it changes the network and security boundary.
- **Resolution**:

### R-05 — Server-side fetching is an SSRF vector into the home network
- **Docs**: `requirements.md` §4.2, `architecture.md` §3.2 and §5
- **Problem**: The worker fetches URLs taken from untrusted feed content (article links, primary sources, redirects). A feed item, or a redirect, can point at `http://192.168.1.1/` (router admin), `http://db:5432`, `http://api:8000/api/v1/...`, `host.docker.internal:11434` (the LLM), or cloud metadata IPs. The current guardrails cover size, depth, and timeouts, but not **destination**. This applies to full-text extraction and feed polling as well, not only mini-research.
- **Recommendation**: Add a guardrail requirement and tests covering:
  - Resolve DNS, then reject private, loopback, link-local, and CGNAT ranges, plus Docker network ranges (IPv4 and IPv6), unless explicitly allow-listed.
  - Re-check the destination on every redirect hop, and pin the resolved IP for the connection (prevents rebinding).
  - Allow only `http` and `https` schemes.
  
  Add this to the AGENTS.md guardrail list and the §2.4 invariant matrix.
- **Resolution**:

---

## 2. Major Findings

### R-06 — Conflict resolution model is under-specified and internally inconsistent
- **Docs**: `requirements.md` §4.6, `data-model.md` §3.3 (line 96), §3.2 (line 70), `development-process.md` §2.4
- **Problem**:
  - The requirements say **per-field** last-write-wins (LWW), but `items` has a single `state_updated_at` shared by read state and star state. Starring on one device would overwrite a read/unread change made on another.
  - `feeds.config_updated_at` is also one timestamp covering many independent fields (category, expiration, research policy, prompt hint, status).
  - LWW on **client** timestamps is vulnerable to device clock skew, which is common on phones. Nothing defines a skew policy (for example, clamping client timestamps to the server's receive time, or using hybrid logical clocks).
  - The interaction between server-driven `expired` and client actions is undefined. Can a client mark an expired item read or unread? Does starring an expired item un-expire it? Does expiration itself count as a "write" in LWW?
  - LWW on large free-text fields (interest profile, prompt hints) silently discards a whole offline edit.
- **Recommendation**: In the sync design doc, write a per-field merge table. For each mutable field, give the merge rule, the timestamp source, and how it interacts with expiration. Store a timestamp per field (`read_state_updated_at`, `starred_updated_at`, ...), or keep a field-level version map. Make free-text config edits either online-only or versioned with a visible conflict. Consider property-based tests (for example with `hypothesis`) for reconciliation convergence.
- **Resolution**:

### R-07 — Item state model needs a state diagram
- **Docs**: `requirements.md` §4.5, `data-model.md` §3.3 and §5
- **Problem**: `state ∈ {unread, read, expired}` combined with `is_starred` leaves several questions open:
  - Is a read item that is later viewed in the archive still `expired`?
  - Does un-starring an old unread item expire it immediately?
  - What does "mark cluster read" do to items that join the cluster *after* the mutation was queued offline?
  - What happens to an exact duplicate (the same URL in two feeds)? Is it one unread item or two? Does reading one mark the other read?
- **Recommendation**: Add an explicit state machine (a mermaid `stateDiagram`) with every transition and who triggers it. Define cluster-level actions as expanding to an **explicit list of item IDs the client knew about** at the time of the action, so they stay idempotent and time-independent. Decide whether read state is tracked per item or per cluster.
- **Resolution**:

### R-08 — "Mark all as read" is missing
- **Docs**: `requirements.md` §4.6 and §4.7
- **Problem**: The standard RSS actions "mark feed/category/all as read" (and "mark older than X as read") are absent from the mutation list. Done naively offline, they would also mark items the client has never seen.
- **Recommendation**: Add them, with semantics bounded by what the client has seen: "all items in scope with `sync_seq <= N`", or an explicit ID list.
- **Resolution**:

### R-09 — Client bootstrap, cache window, eviction, and server restore are undefined
- **Docs**: `data-model.md` §5 (line 192), `architecture.md` §4.2
- **Problem**:
  - A new client pulling from `since_seq=0` receives the **entire history** (73k items a year, growing), even though the cache policy is "unread + starred + 14 days of read".
  - Deltas will include changes to old items outside the window (re-enrichment, cluster updates). Nothing says whether the client should ignore them or evict.
  - Nothing says how items get evicted from the client as they age out of the window.
  - **Restoring the database from a `pg_dump` backup winds `sync_seq` back.** Clients holding a higher cursor will then miss every change until the sequence catches up.
- **Recommendation**:
  - Define a bootstrap or snapshot endpoint that returns the working set plus the current watermark.
  - Define client-side eviction rules.
  - Add a server **epoch / instance ID** to every pull response. When it changes, or the cursor is ahead of the server, the client must do a full resync.
  - Add tests for each of these.
- **Resolution**:

### R-10 — Embedding dimension and model changes are not handled
- **Docs**: `data-model.md` §3.4 (line 119), §3.7, §4
- **Problem**:
  - `VECTOR(768)` is described as "configurable", but a pgvector column and its HNSW index have a fixed dimension. Changing the embedding model needs a migration and a re-embed of all history.
  - Similarity thresholds (`0.86` / `0.78`) are specific to one model.
  - The docs describe two different embedding inputs, pre-enrichment `title + excerpt` and post-enrichment `title + tldr + takeaways`, but there is only one column. It's unclear which input is stored, and mixing them in one vector space degrades clustering.
  - The HNSW index covers non-current enrichments too.
- **Recommendation**:
  - Store `embedding_model` alongside each vector, and consider a dedicated `item_embeddings` table keyed by `(item_id, model)`.
  - Pick one canonical embedding input.
  - Document the re-embed and re-cluster procedure (a project skill would suit this).
  - Make thresholds per-model settings.
  - Restrict similarity queries to current, in-window rows.
- **Resolution**:

### R-11 — Clustering design gaps
- **Docs**: `architecture.md` §3.2 (step 5) and §4.1 (step 6), `data-model.md` §4
- **Problem**:
  - **Ordering contradiction**: `data-model.md` §4.3 links exact-URL duplicates into a cluster *immediately* at ingest, but the pipeline only clusters *after* enrichment.
  - Because clustering happens after enrichment, every duplicate gets a full LLM enrichment. That is redundant work, and the user may see several near-identical summaries until clustering finishes.
  - A running centroid drifts over time, and nothing covers merging two clusters or splitting a wrong one.
  - The user can't correct a bad cluster. That's a natural explicit-feedback action and should be in scope.
- **Recommendation**:
  - Settle the stage order. Consider dedup and cheap clustering before enrichment, using a title or excerpt embedding, and then cluster-aware enrichment.
  - Define merge and split rules.
  - Add "not the same story" / "split from cluster" feedback.
  - Specify which item represents the cluster and what relevance score the cluster shows.
- **Resolution**:

### R-12 — LLM capacity, context budget, and backfill planning are missing
- **Docs**: `requirements.md` §3 and §5, `architecture.md` §3.2, ADR 0003
- **Problem**:
  - There's no throughput estimate. On a Ryzen AI Max+ 395, a dense ~70B model typically manages only a few tokens per second of generation, and prompt processing is slow too. At several thousand prompt tokens per item (article + PDF excerpts + profile + feedback examples), steady state might be fine, but an **OPML import backfill (25 feeds × ~50 items)**, a "re-process after prompt change", or a re-embed could take days.
  - Nothing defines the context-window budget or the truncation and chunking strategy for long articles and 20 MB PDFs.
  - Nothing limits concurrency to the single GPU, sets job priorities (user-triggered "dig deeper" should jump the queue), or covers sharing the LLM host with other homelab workloads.
  - The chat and embedding models are not chosen. Mixture-of-experts models are much faster on this hardware and should be considered.
- **Recommendation**: In `llm-pipeline-and-feedback.md`, add:
  - A capacity budget (tokens per item, items per hour, backlog drain time).
  - A per-stage token budget with a truncation and chunking policy.
  - A global LLM concurrency limit and job priorities.
  - A backfill policy, for example "on subscribe, enrich only the N newest; mark older ones read or expired".
  - Benchmark candidate models early with the opt-in live-LLM suite.
- **Resolution**:

### R-13 — The "OpenAI-compatible" abstraction leaks around structured output
- **Docs**: `architecture.md` §2 and §5, ADR 0001
- **Problem**: Ollama and `llama.cpp` support JSON-schema output through different mechanisms and with different completeness: Ollama uses its `format` parameter on the native API and `response_format` on its OpenAI-compatible endpoint, while `llama.cpp` uses `response_format` / `json_schema` / GBNF grammars. Behaviour differs on details such as `enum` and `minItems`. Reasoning models (for example the Qwen3 family) may emit `<think>` content or need the thinking mode disabled. Embedding endpoints also differ in how they handle batching and normalization.
- **Recommendation**: Define a small `LLMAdapter` interface with per-runtime implementations or capability flags. Record a contract-test fixture set for **both** runtimes. Specify handling of reasoning output and whether embeddings are normalized for cosine similarity.
- **Resolution**:

### R-14 — Host-native LLM networking is under-specified
- **Docs**: ADR 0001, `architecture.md` §6 (line 133)
- **Problem**: Ollama binds to `127.0.0.1` by default, and `llama.cpp`'s server also defaults to localhost. Neither is reachable from a container through `host-gateway`. The common fix, binding to `0.0.0.0`, exposes an unauthenticated LLM to the whole LAN.
- **Recommendation**: Document the supported approach: bind to the Docker bridge IP, or bind to `0.0.0.0` with a host firewall rule allowing only the Docker subnet. Add a startup connectivity check that shows up in System Health.
- **Resolution**:

### R-15 — Prompt injection and untrusted HTML rendering
- **Docs**: `requirements.md` §4.3, `architecture.md` §3.4
- **Problem**:
  - Article and PDF text goes straight into prompts, so a page can instruct the model ("rate this 100", "summarize as ..."). The blast radius is limited to scores and summaries, but it should be acknowledged and mitigated: delimit untrusted content, keep scoring instructions in the system prompt, and validate against the schema.
  - `raw_summary` is feed-provided HTML, and LLM output may contain markup. Both are rendered in the client, so there is an XSS risk. Combined with R-04, XSS lets an attacker drive the API.
- **Recommendation**: Require HTML sanitization (for example DOMPurify with an allow-list), a strict CSP on the SPA, rendering LLM output as plain text or a safe Markdown subset, and delimiting of untrusted content in prompts. Add tests with hostile fixtures.
- **Resolution**:

### R-16 — Untrusted PDF/HTML parsing needs resource isolation
- **Docs**: `architecture.md` §3.2, §5
- **Problem**: PyMuPDF (MuPDF is C code) and the HTML parsers handle hostile input. A crafted document can cause runaway CPU or memory use (decompression bombs, pathological pages) or worse, and could stall or crash the whole worker.
- **Recommendation**: Run extraction in a subprocess or a separate low-privilege container with CPU, memory, and wall-clock limits, page-count limits, and a cap on decompressed size. Keep PyMuPDF on a pinned, patched version.
- **Resolution**:

### R-17 — Job queue schema is missing operational essentials
- **Docs**: `data-model.md` §3.10 (line 173), ADR 0003
- **Problem**:
  - No uniqueness or deduplication key, so duplicate `poll_feed` or `enrich_item` jobs can be queued for the same entity.
  - No lease or heartbeat expiry. A crashed worker leaves jobs stuck in `running`, because nothing reclaims a stale `locked_at`.
  - No `finished_at` or `updated_at`, and no retention policy for `done` jobs (about 1,000 a day, forever).
  - No dead-letter or manual-retry workflow. `failed` status exists, but no process uses it.
  - No idempotency rule per stage: what happens when `enrich_item` runs twice?
  - No scheduler leader election if more than one worker replica runs.
- **Recommendation**: Add a partial unique index such as `(job_type, dedupe_key) WHERE status IN ('queued','running')`, a lease expiry and reclaim rule, a retention policy for completed jobs, and per-stage idempotency notes. Alternatively, evaluate an existing Postgres-backed queue library (such as `procrastinate`) rather than building one; if you build your own, record why in ADR 0003.
- **Resolution**:

### R-18 — Feed lifecycle edge cases
- **Docs**: `requirements.md` §4.1, `data-model.md` §3.2
- **Problem**:
  - **301**: Updating `feed_url` can collide with the `UNIQUE` constraint if that URL is already subscribed.
  - **410**: `status` has no `gone`, `error`, or `broken` value, so the health state is mixed in with user intent (`paused`/`muted`).
  - **Unsubscribe**: It isn't defined what happens to the feed's items (keep them in history, hide them from views, keep unread counts?) or to a later re-subscribe.
  - **Polling politeness**: `429` / `Retry-After`, `Cache-Control`, and jitter aren't mentioned.
  - **Article fetch politeness**: No per-domain rate limit is specified for article fetches (the extraction and research stages).
  - **Updated entries**: Nothing defines how to handle an entry whose content changes (`content_hash` differs). Re-enrich? Re-notify?
  - **GUID instability**: Some feeds change GUIDs over time, which causes duplicates.
  - **`muted`**: The meaning of muted versus paused versus unsubscribed is not defined.
- **Recommendation**: Separate user intent (`status`) from health (`health_state`). Define a feed state diagram, the item policy on unsubscribe, the update and re-enrichment policy, and politeness headers.
- **Resolution**:

### R-19 — Expiration edge cases
- **Docs**: `data-model.md` §5 (lines 189–190)
- **Problem**: Expiration keys off `published_at`. Missing, bogus, or future dates and backfilled old entries all misbehave: a newly subscribed feed's older entries expire immediately, and a future-dated entry never expires. The `effective_ttl` description in prose also reads ambiguously.
- **Recommendation**: Use `GREATEST(published_at, fetched_at)` or simply `fetched_at`, and clamp future dates. Restate the TTL resolution as a precise rule or pseudocode. Define the backfill behaviour together with R-12.
- **Resolution**:

### R-20 — The feedback loop has no way to measure whether it works
- **Docs**: `requirements.md` §4.3 and §4.4, `architecture.md` §4.3
- **Problem**:
  - The requirements say "numeric/tiered" while the data model fixes the score at 0–100. Raw 0–100 LLM scores are poorly calibrated and drift between prompt and model versions.
  - In "relevance-sorted" views, items scored under different prompt versions are mixed together with nothing to normalize them.
  - Injecting "recent feedback examples" into every prompt grows the prompt, biases toward recent topics, and has no selection rule.
  - Nothing measures whether a prompt or profile change helped.
  - A `fetch_pdfs` feedback event *also* changes `feeds.research_policy`. These are two writes of one fact, and they can conflict with a direct config edit.
- **Recommendation**:
  - Decide between tiers (for example 1–5 or must-read/maybe/skip) and 0–100.
  - Define how scores from different prompt versions compare or get re-scored.
  - Define selection rules for feedback examples (count, diversity, recency, matching feed or tag).
  - Add a small offline **evaluation set** built from the user's thumbs up/down history, so prompt changes can be compared (it ties into the `live_llm` suite).
  - Model "fetch PDFs here" as a feed config mutation that *also* records an audit event, with one source of truth.
- **Resolution**:

### R-21 — Requirements lack user stories, IDs, and acceptance criteria
- **Docs**: `requirements.md`, `AGENTS.md` (which says the requirements contain user stories)
- **Problem**: There are no user stories, no requirement IDs, and no acceptance criteria. That makes it hard to trace requirements to tests (the process doc is test-heavy) and to judge whether Phase 1 is done.
- **Recommendation**: Give each functional and non-functional requirement an ID (`FR-FEED-01`, `NFR-SYNC-02`, ...), add short user stories for the main journeys (morning triage, offline reading on a laptop, tuning the interest profile, "dig deeper"), and add measurable acceptance criteria. Examples: "an item is visible in the client within 2 minutes of the poll that fetched it, whether or not it has been enriched"; "an outbox of 500 mutations flushes in under 5 seconds".
- **Resolution**:

### R-22 — The Phase 1 MVP is too large for one milestone
- **Docs**: `requirements.md` §6
- **Problem**: Phase 1 contains essentially the whole product: ingestion, extraction, mini-research with PDFs, enrichment, clustering, feedback, prompt versioning, offline sync, and expiration. Nothing usable ships until all of it does, and design mistakes surface late.
- **Recommendation**: Split Phase 1 into vertical-slice milestones, each usable on its own. For example:
  - **1a**: Polling, full-text extraction, a basic reader, read/star sync, offline outbox, expiration.
  - **1b**: LLM enrichment (TL;DR, tags, depth, relevance) and the prompt and profile editor.
  - **1c**: Dedup and clustering.
  - **1d**: Mini-research and PDFs.
  - **1e**: The feedback loop and evaluation.
  
  Sequence the component designs to match.
- **Resolution**:

---

## 3. Minor Findings

### R-23 — Open questions are stale or inconsistent with the ADRs
`requirements.md` §7 still lists tech stack, Android form factor, and LLM topology as open, but ADRs 0001 and 0002 propose answers, and all ADRs are `Proposed`. Either close the questions by linking the ADRs (once the owner accepts them), or explicitly mark the ADRs as awaiting the owner's answers. Keep a single, current list of open questions.
- **Resolution**:

### R-24 — The architecture diagram contradicts the deployment description
In `architecture.md` (line 41), the Android client connects directly to `API`, but §6 says `web` reverse-proxies `/api/` on a single LAN port. Make the diagram show all clients going through the single entry point, or document a second exposed port.
- **Resolution**:

### R-25 — Undecided client choices block the client design
"React or Svelte" and "Dexie.js or idb" are still open, and so is Capacitor versus native Kotlin for Phase 2. The Phase 2 choice affects Phase 1: if Capacitor will wrap the web UI, the Phase 1 SPA should be built to be Capacitor-friendly from the start. Decide in the client design or an ADR, together with R-03.
- **Resolution**:

### R-26 — Android plain-HTTP note is incomplete
ADR 0002 presents plain HTTP as frictionless, but Android 9+ blocks cleartext traffic by default (it needs a `networkSecurityConfig`). Capacitor's default `https` scheme, combined with an `http` API, triggers mixed-content rules. Android background sync (WorkManager with Wi-Fi/SSID constraints) is also non-trivial from Capacitor. Record these trade-offs in the ADR.
- **Resolution**:

### R-27 — Detached search scope is unspecified
Full-text search runs on the server, so what does search do while detached? Options: a local search over the cached working set, or disabled with a clear message. Specify it.
- **Resolution**:

### R-28 — Offline feed management scope
Clarify which config mutations work offline. Unsubscribe and mute are listed; "add feed" and "OPML import" presumably need the LAN, so say so. Also define the push-response contract: per-mutation `applied` / `superseded` / `rejected`, how the client learns that a mutation was rejected, and the rollback UX for an optimistic update that failed.
- **Resolution**:

### R-29 — `search_vector` maintenance
`items.search_vector` combines fields from `item_enrichments` and `item_tags`. Specify how it's kept current: a trigger, or application code on each enrichment and tag change. Specify the text-search language configuration, and the cap on `extracted_text` length (a `tsvector` is limited to about 1 MB).
- **Resolution**:

### R-30 — Storage estimate looks optimistic
The "well under 1–2 GB" per year estimate leaves out `extracted_text` (often 10–50 KB per item), research excerpts, enrichment history, embeddings plus the HNSW index, `tsvector`/GIN, jobs, and `sync_mutations`. It's fine on this hardware, but re-estimate it, and add retention rules for `jobs` and `sync_mutations` (the latter only needs to cover the longest plausible offline period).
- **Resolution**:

### R-31 — User-editable tags are mentioned but not designed
`item_tags.source = 'user'` implies users can add and remove tags, but there is no requirement, sync mutation, or UI for it. Either add it properly or drop it.
- **Resolution**:

### R-32 — Seeding and versioning of prompts
Prompt templates live in the database (`prompt_configs`). Define how default prompts are shipped and upgraded: seeded from files in the repo, with the user's edits preserved across releases. Define how the `live_llm` eval suite references prompt versions.
- **Resolution**:

### R-33 — Development process gaps
- No CI is defined. State whether checks run in CI (GitHub Actions or similar) or only through `make check`.
- Specify how integration tests get PostgreSQL (for example `testcontainers` or a compose test profile).
- Define what "Approved" means for docs and ADRs: who approves and how it's recorded. AGENTS.md gates coding on approval, so this needs a defined mechanism.
- Add security-invariant tests (R-04, R-05, R-15) to the §2.4 matrix.
- **Resolution**:

### R-34 — Backups
Plan for and test restores, not just backups (a restore drill), and handle the `sync_seq` regression on restore (see R-09). Specify the backup schedule, how many copies to keep, and where they go (ideally off the host).
- **Resolution**:

---

## 4. Things That Are Working Well

- Clear guardrails in `AGENTS.md`, and a spec-first, docs-in-the-same-change discipline.
- Sensible stack choices for the scale (one Postgres for data, search, vectors, and the queue; no Redis).
- Keeping the LLM host-native, with no GPU passthrough, is the right call for Strix Halo hardware.
- Non-blocking enrichment, with raw items visible immediately, is the correct UX foundation.
- Idempotent `client_mutation_id`s and server-side cursors (rather than client timestamps) for downstream sync are the right primitives. They just need the fixes in R-01, R-02, and R-06.
- Keeping enrichment history linked to the prompt version and model is good for reproducibility and tuning.

---

## 5. Open Questions for the Owner

These need the project owner's decision. Propose a default for each, but don't treat it as settled until the owner confirms.

| ID | Question | Related |
| :--- | :--- | :--- |
| Q-01 | Is the Phase 1 "Ubuntu desktop" a laptop that actually leaves the LAN, or a fixed desktop? If it's fixed, detached mode for Phase 1 matters much less and R-03 could be deferred to Phase 2. | R-03, R-22 |
| Q-02 | Acceptable client form for Phase 1: a plain browser tab, a local `localhost`-served app, a desktop shell (Tauri/Electron), or LAN HTTPS with a local CA? | R-03, R-25 |
| Q-03 | How much auth friction is acceptable? (Proposed: one-time device pairing code, then a long-lived per-device token.) | R-04 |
| Q-04 | Which LLM models (chat and embedding) do you intend to use, and is the LLM host shared with other workloads (quiet hours, priority)? | R-12, R-14 |
| Q-05 | Relevance as tiers (for example must-read / maybe / skip) or a 0–100 score? How should relevance-sorted views combine relevance and recency? | R-20 |
| Q-06 | Read-state semantics for duplicates and clusters: should reading one item in a cluster mark the cluster read? Should cross-feed exact duplicates show as one item? | R-07, R-11 |
| Q-07 | On subscribing to a feed (or OPML import), how much backlog should be ingested and enriched? Should old entries be marked read automatically? | R-12, R-19 |
| Q-08 | What should search do while detached: a local search over cached items, or unavailable? | R-27 |
| Q-09 | Is it worth splitting Phase 1 into milestones 1a–1e as proposed, and is that order right for you? | R-22 |
| Q-10 | Should a later phase include a compatibility path for existing RSS apps (Fever/GReader), or is that a permanent non-goal? This affects how strictly the sync API stays aggregate-shaped. | ADR 0002 |
| Q-11 | Backup target: is there another host or NAS for off-machine backups? | R-34 |

---

## 6. Suggested Resolution Order

1. Get answers to the owner questions Q-01 to Q-03 and Q-09, which affect the blockers and how the work is split.
2. Write ADRs for the sync watermark (R-01, R-02, R-09), the client origin and offline shell (R-03), and the LAN security model (R-04, R-05, R-15).
3. Update `requirements.md`: requirement IDs, user stories, acceptance criteria, security NFRs, the milestone split, and refreshed open questions (R-21, R-22, R-23).
4. Update `data-model.md`: the sync surface table, per-field timestamps, the item and feed state machines, the job queue fields, and embedding versioning (R-02, R-06, R-07, R-10, R-17, R-18).
5. Update `architecture.md`: the diagram, LLM networking, the extraction sandbox, and capacity planning (R-12, R-14, R-16, R-24).
6. Bring ADRs 0001–0003 up to date to match the changes above, then ask the owner to move them from `Proposed` to `Accepted`.
7. Only then begin the `docs/design/*` component specs, in the order of the Phase 1 milestones.
