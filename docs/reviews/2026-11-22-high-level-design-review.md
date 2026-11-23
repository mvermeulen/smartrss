# High-Level Design Review

- **Date:** 2026-11-22
- **Scope:** Requirements, architecture, data model, development process, ADR 0001–0003, and documentation index
- **Status:** Open — resolve blockers and owner decisions before writing component-level designs

## Review outcome

The package has a credible single-user LAN architecture and good overall separation of ingestion, LLM enrichment, storage, and sync. However, it is not yet safe to turn into component specifications. The most important unresolved issues are sync correctness, detached client security/origin, server-side URL SSRF protection, conflict semantics, and the operational behavior of jobs and state transitions.

## Blockers

### B-01: Downstream sync can skip an already-seen transaction

`sync_seq` is obtained with a PostgreSQL sequence, but rows that receive different sequence numbers can commit out of order. A pull cursor can move past an uncommitted or later-committing row. The data model and invariant tests do not currently prevent that failure.

**Required resolution:** Select and record a committed-watermark strategy: a serialized counter, stable snapshot watermarks, or an append-only change log. Define pull-page visibility and cursor advancement. Add a real-PostgreSQL concurrent-commit test that proves no rows are skipped or duplicated.

### B-02: Sync is not defined for child entities and tombstones

Enrichments, tags, research sources, feedback, prompt changes, cluster membership changes, and deletions do not each have a complete synchronization contract. It is unclear what an aggregate item pull contains or how a client learns that a child changed or disappeared.

**Required resolution:** Add a sync surface table for every synchronized entity and operation. Prefer aggregate-shaped item payloads for one entity, or specify a change-log payload. Define tombstones and cluster merge/split removal semantics.

### B-03: Phase 1 cannot reliably serve an offline SPA over plain LAN HTTP

A browser loaded over `http://smartrss.lan` cannot use service-worker secure contexts or persistent IndexedDB reliably. Leaving the LAN may leave the UI unable to load, and origin or IP changes orphan the cache and mutation outbox.

**Required resolution:** Decide the Phase 1 client origin in an ADR. Use a stable localhost desktop shell, LAN HTTPS, or another explicit solution. Document persistent-storage requirements, fixed hostname/instance identity, and an offline reload/outbox test.

### B-04: The LAN is unauthenticated and mutation endpoints are exposed to CSRF/DNS rebinding

The single-user decision has been interpreted as no authentication. A bearerless mutation API is readable and writable by any LAN device, while malicious pages can also target a plain-HTTP same-network service.

**Required resolution:** Define per-device pairing and bearer tokens, a strict Host allow-list, JSON-only mutation requests, and CORS policy. Record the threat model and add security tests.

### B-05: Server fetchers expose an SSRF path

Feed, article, and primary-source URLs originate in untrusted external content. Feed redirects and DNS rebinding can reach router admin interfaces, Docker services, the local LLM, or link-local/cloud metadata destinations.

**Required resolution:** Apply destination validation to all server-side fetch stages. Resolve DNS before connecting, reject loopback/private/link-local/CGNAT/Docker ranges and non-HTTP schemes, revalidate every redirect, and pin or revalidate the resolved address for the connection. Add hostile fixture tests.

## Major findings

### M-01: Per-field conflict resolution is under-specified

Read state and star state share `state_updated_at`; feed settings share `config_updated_at`. These timestamps cannot represent independent field changes. Device clock skew and server-side expiration need explicit merge rules. Free-text prompts/profile edits need a conflict policy rather than silent whole-field last-write-wins.

**Resolution required:** Add a per-field merge table including timestamp/version source, server or client authority, expiration interaction, and rejection/conflict behavior.

### M-02: Item and feed state machines are missing

The unread/read/starred/expired model leaves open transitions for read items, un-starring, cluster actions, exact duplicates, feed unsubscribe, mute versus pause, 410 feeds, and re-subscription.

**Resolution required:** Define state diagrams and explicit transition triggers. Mark `expired` as an item state and specify whether starring an expired item restores it. Define cluster and bulk actions as explicit item-ID or snapshot-cursor scopes.

### M-03: Standard bulk actions are missing

Mark category/feed/all read and mark older than a threshold read need offline semantics. They must not silently operate on unseen items.

**Resolution required:** Define mutation forms bounded by a server snapshot watermark or explicit item-ID lists, and specify idempotency and out-of-window behavior.

### M-04: Bootstrap and restore behavior can lose or over-pull data

A `since_seq=0` pull can return a growing full history. Restoring PostgreSQL can rewind `sync_seq`, while clients can retain a newer cursor. Cache eviction and old-delta handling are not defined.

**Resolution required:** Add snapshot/bootstrap endpoints, a server instance or epoch ID, cache eviction rules, and full-resync-on-restoration behavior. Test backup restore and cursor regression.

### M-05: Embedding and clustering contracts are unstable

The fixed `VECTOR(768)` column conflicts with model changes, thresholds are model-specific, the canonical embedding input is ambiguous, and the HNSW index can include obsolete enrichments. Merge/split behavior and cluster representation are also unspecified.

**Resolution required:** Version embedding model and dimensions, define one canonical embedding input, isolate or re-embed history, scope queries to current in-window vectors, and specify merge/split and cluster representative rules.

### M-06: LLM throughput and context budgets are absent

Prompt size, article/PDF truncation, concurrency, priority, backlog, reprocessing, and sharing the host LLM with other workloads have not been designed. A large backfill or prompt change could run for days.

**Resolution required:** Produce capacity estimates, stage token budgets, truncation/chunking rules, one-host concurrency limits, priorities, and a small backfill policy.

### M-07: The OpenAI-compatible abstraction is not a sufficient implementation contract

Ollama and llama.cpp differ in structured-output fields and reasoning behavior. Embedding normalization and batching are unspecified.

**Resolution required:** Define an internal adapter/capability contract and fixture-based tests for both runtimes, including malformed JSON, reasoning mode, batches, normalization, and capability failures.

### M-08: Host-native LLM networking is insecure or unusable as written

Binding Ollama/llama.cpp to localhost from the Docker stack is commonly unreachable. Binding it to `0.0.0.0` exposes it on the LAN.

**Resolution required:** Choose Docker bridge/firewall/private LAN binding, restrict allowed source networks, and expose startup health checks plus a diagnostics view.

### M-09: Prompt and extraction boundaries need hostile-input controls

Article/PDF text can prompt-inject the LLM, raw feed HTML can contain active markup, and LLM output is not guaranteed safe to render. Parser code also processes untrusted binary input.

**Resolution required:** Add prompt delimitation, HTML sanitization, strict CSP, plain-text/safe-Markdown rendering, parser process isolation, limits, and hostile fixtures.

### M-10: The PostgreSQL queue needs lease and idempotency semantics

The queue schema lacks dedupe keys, lease expiry, heartbeats, terminal timestamps, retention, stale-job reclaim, scheduler leadership, and stage idempotency rules.

**Resolution required:** Add operational fields and indexes, stale lease reclaim, retention/dead-letter/manual retry behavior, a scheduler leadership rule, and per-stage duplicate-run semantics.

### M-11: Feed lifecycle and polling policy remain incomplete

301 URL migration, 410, unsubscribe/re-subscribe behavior, 429 and `Retry-After`, article-domain politeness, changed entry content, unstable GUIDs, and the meaning of `muted` are unresolved.

**Resolution required:** Separate user intent from feed health, define a feed state machine, 301/410 and unsubscribe policies, conditional-header and Retry-After handling, domain rate limits, and changed-content reprocessing behavior.

### M-12: Expiration and backlog rules can silently age items incorrectly

Expiration uses `published_at`, so missing/future/backfilled dates produce incorrect outcomes. The subscription backlog policy is undefined.

**Resolution required:** Choose fetched or clamped publication time, state the effective TTL precisely, and define the initial OPML/subscription backlog and enrichment behavior together.

### M-13: Relevance feedback and model versions lack an evaluation model

Scores across prompt/model versions are not comparable; feedback-example selection is undefined; and `fetch_pdfs` creates two potentially conflicting writes.

**Resolution required:** Choose tiers or 0–100, normalize or reprocess on version changes, define feedback-example selection, use feed config mutations as the source of truth with audit events, and add an offline evaluation set.

### M-14: MVP scope is too broad and not testable as a milestone

Phase 1 bundles core ingestion, every enrichment feature, clusters, feedback, sync, and UI. It also lacks traceable requirements and acceptance criteria.

**Resolution required:** Split Phase 1 into usable vertical slices, add requirement IDs and user stories, and define measurable acceptance criteria for each slice.

## Minor findings

- ADRs 0001–0003 are `Proposed` but requirements still present their decisions as open questions.
- The architecture diagram routes Android directly to API while deployment defines a single reverse-proxy entry point.
- React versus Svelte and Dexie versus idb remain undecided; Phase 2 form (Capacitor or native) must align with the Phase 1 shell decision.
- Android cleartext traffic and background Wi-Fi sync need explicit network/security configuration.
- Detached search scope is unspecified.
- Offline feed management and push response/rollback behavior need a contract.
- `search_vector` maintenance, language, and text-size limits are missing.
- Storage estimates omit extracted text, research history, embeddings, indexes, jobs, and mutation logs; add retention estimates.
- User-editable tags lack a requirement and sync/UI design.
- Prompt seeding/version upgrade behavior is missing.
- CI/PostgreSQL integration setup, approval criteria, and security invariant tests are missing from the development process.
- Restore drills, backup schedule, copies, and off-host storage are not specified.

## Owner decisions needed

1. Is Phase 1 a laptop that leaves the LAN, and which secure/stable client origin should it use?
2. What auth friction is acceptable for LAN devices?
3. Which chat and embedding models will be used, and is the LLM host shared with other workloads?
4. Should relevance use tiers or 0–100, and how should recency combine with relevance?
5. How should reading, duplicates, and clusters interact?
6. How much backlog should be ingested and enriched after a subscription or OPML import?
7. Should detached search work across the local cache, or be unavailable?
8. Should Phase 1 be split into vertical milestones in the proposed 1a–1e order?
9. Is compatibility with Fever/GReader permanently out of scope?
10. Is off-host backup storage available?

## Recommended pre-component-design sequence

1. Decide the Phase 1 origin, authentication model, relevance model, cluster/read semantics, and MVP milestone split.
2. Resolve sync watermark, aggregate payload/tombstone, conflict, bootstrap, epoch, and restore semantics in ADRs.
3. Update requirements with IDs, stories, acceptance criteria, security requirements, and revised open questions.
4. Update architecture and data model for state machines, SSRF controls, queue leases, embedding versioning, and LLM capacity.
5. Move accepted ADRs to `Accepted`, and update the documentation index/statuses.
6. Only then begin the lower-level component designs in vertical-slice order.
