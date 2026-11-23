# Requirements

> **Status:** In Review — Core requirements captured across discovery rounds; ready for review before locking architecture and component designs.

## 1. Problem Statement
Standard RSS readers fetch raw feeds chronologically and leave all triage, organization, and enrichment to the user. High-volume or overlapping feeds create noise and duplication, RSS entries often contain only short teasers or commentary pointing to deeper primary sources (such as research papers or technical reports), and client-only readers repeat fetching/processing across devices without shared intelligence.

**SmartRSS** is a self-hosted, distributed RSS system for a single user. A containerized backend server on the home LAN pre-fetches feeds, extracts full article content and selectively performs deeper "mini-research" on primary referenced sources (including PDFs and reports), uses a local LLM to generate structured display and curation metadata, clusters duplicate/overlapping stories, and persists a full historical log in a database. Clients provide a classic RSS reading experience enhanced with smart metadata, sync over home Wi-Fi, and operate seamlessly when detached from the home network.

## 2. Goals & Non-Goals
### Goals
- **Local LLM Enrichment & Customizable Curation**: Use local LLMs (`Ollama` or `llama.cpp`) to generate structured display metadata (TL;DR, key takeaways, topic/category tags, content depth) and curation metadata (relevance score + "why" rationale), with user-editable prompts, interest profiles, and per-feed curation instructions.
- **Selective "Mini-Research" on Primary Sources**: When an RSS item references or discusses an underlying primary source (e.g., a research paper PDF, technical report, or primary article), selectively fetch and synthesize context from that source—guided by per-feed configuration and user feedback.
- **Distributed Client-Server Architecture**:
  - **Server**: Pre-fetches feeds politely, extracts full content and primary references, clusters duplicate/overlapping topics, runs local LLM enrichment/curation, tracks feed health, and persists all history and state.
  - **Clients**: Provide a classic RSS reader experience backed by the server, caching enriched items and metadata locally, opening external web URLs in a browser, and syncing back read/star state, explicit feedback, and content-related configuration changes.
- **Local-Network First with Detached Operation**: Designed around a home LAN / Wi-Fi model. Clients sync when connected to the home LAN and detach cleanly when away (assuming general internet access is still available to follow external article URLs, just without access to the home backend server).
- **Proven RSS & Content Management Patterns**: Incorporate feed hygiene (polite polling, backoff, feed health), categories/folders and tags, cross-feed deduplication/clustering, read/unread and star/save management, configurable unread expiration with a retained historical log, and full-text search.
- **Feedback-Driven Evolution**: Start with simple, explicit user feedback controls (including prompt/curation customization and research hints like "fetch PDFs here") while keeping the design extensible for more sophisticated adaptation over time.

### Non-Goals
- **Multi-user / Multi-tenant support**: Designed primarily for a single user (no user accounts, sharing, or per-user tenant isolation required).
- **Third-party RSS client protocol compatibility** (e.g., Fever API, Google Reader API): Not required; a native SmartRSS API will expose LLM metadata, mini-research context, clusters, explicit feedback, and offline config mutations as first-class concepts.
- **Public internet / VPN remote access**: Initial scope assumes local LAN / home Wi-Fi sync only (no public exposure or VPN-specific workflows required).
- **Full external webpage / media mirroring on clients**: Detached clients cache server-curated records and metadata rather than mirroring external linked web pages; clicking a link opens the external URL via the client's internet connection.

## 3. Target Users, Scale & Platforms
- **Primary User**: Single personal user running a home/LAN backend server.
- **Target Scale**: ~25 feeds, ~200 items/day (~73k items/year), with headroom to scale to ~50–100 feeds without redesign.
- **Target Platforms**:
  - **Server**: Ubuntu Linux, packaged primarily as Docker containers (Docker Compose) connecting to a local LLM runtime (`Ollama` or `llama.cpp`) running on an AMD Ryzen AI Max+ 395 class machine (128 GB unified memory).
  - **Client — Phase 1**: Linux / Ubuntu desktop (browser-based UI or browser-integrated interface for effortless opening of external URLs).
  - **Client — Phase 2**: Android client supporting detached caching and offline triage/config edits away from home Wi-Fi.

## 4. Functional Requirements

### 4.1 Feed Management, Hygiene & Ingestion
- **Feed Subscriptions & Organization**: Add, edit, pause/mute, and unsubscribe feeds; organize feeds into categories/folders; import/export subscriptions via OPML.
- **Polite Polling & Resilience**: Support HTTP conditional requests (`ETag`, `Last-Modified`), per-feed polling intervals, user-agent identification, request timeouts, and exponential backoff on transient failures.
- **Feed Health Monitoring**: Track and surface per-feed health (last successful fetch, HTTP status/error message, consecutive error count, item cadence, stale/broken indicators).
- **Server-Side Full Content Extraction**: Fetch and extract clean article text from linked URLs on the backend whenever feasible so the local LLM analyzes full content rather than truncated RSS teasers.
- **Per-Feed Intelligence & Research Configuration**: Allow per-feed attributes/guidance (e.g., custom instructions, whether to perform mini-research, whether to follow and parse linked PDFs/papers, expiration overrides).

### 4.2 Selective Deeper Fetching ("Mini-Research")
- Detect when an RSS item is primarily commentary on or a pointer to an underlying primary source (e.g., research paper PDF/arXiv link, technical report, primary announcement).
- Driven by a combination of **per-feed guidance** (e.g., *"for this LLM feed, locate and summarize linked papers"*), **item heuristics**, and **user feedback** (e.g., *"fetch PDFs for this feed"* or *"dig deeper on this item"*).
- Extract text from supported primary sources (HTML articles and PDF documents) and feed relevant excerpts to the local LLM to synthesize primary-source context.
- Enforce strict, tunable server-side guardrails: max link depth (default 1 hop), max document size, fetch timeouts, allowed content types, and polite rate limiting.

### 4.3 Local LLM Metadata, Curation & Customization
- **Display Metadata** (stored per item):
  - **TL;DR Summary**: Concise high-signal summary.
  - **Key Takeaways**: Scannable bullet points of main findings or claims.
  - **Topic / Category Tags**: Normalized tags for filtering and discovery.
  - **Depth / Content Type**: Structured classification (e.g., `news-brief`, `deep-dive`, `paper-report`, `opinion`, `release-notes`, `discussion`).
  - **Primary Source / Research Context** (when triggered): Synthesized context and direct link(s) to the underlying paper/report/source.
- **Curation Metadata** (stored per item):
  - **Relevance Score**: Numeric/tiered relevance rating tailored to the user's interests.
  - **Relevance Rationale ("Why")**: Short human-readable explanation of why the item was scored as it was.
- **Deduplication & Story Clustering**:
  - Detect exact/canonical URL duplicates across feeds.
  - Cluster items across overlapping feeds that cover the same story, announcement, or paper so the user can view a single grouped story with links to all covering sources rather than reading redundant summaries.
- **Inspectable & Customizable LLM Configuration**:
  - User-editable interest profile / curation criteria, system/task prompt templates, and per-feed prompt hints.
  - Versioned or logged prompt/config changes so the user can iteratively tune output quality and optionally re-process selected items.

### 4.4 Explicit Feedback & Evolution
- Provide simple, low-friction explicit feedback controls on items and feeds:
  - Item relevance feedback (e.g., thumbs up / thumbs down, optional short reason or quick tags like *"more like this"*, *"less like this"*, *"wrong score"*).
  - Feed/enrichment feedback (e.g., *"fetch PDFs here"*, *"mute/unsubscribe"*, adjust feed priority or research guidance).
- Persist all feedback in the historical log and incorporate it into future curation/scoring (e.g., via evolving interest profile / recent feedback examples / per-feed directives).
- Keep the feedback model extensible so future phases can add automated profile refinement, embedding-based preference learning, or implicit signals.

### 4.5 Reading State, Archiving & Expiration
- **Item States**: Unread, Read, Starred/Saved, Expired (aged out of active unread queue).
- **Full Historical Log**: Retain a permanent log of ingested items, LLM metadata, clusters, and user feedback/actions for search, deduplication memory, and future model evolution.
- **Configurable Unread Expiration**:
  - Unread, unstarred items older than a configurable TTL automatically age out (`Expired`) from the active unread views so the inbox stays manageable.
  - Global default expiration policy plus **per-feed expiration overrides** (including "never expire").
  - **Starred/saved items never expire** and remain easily accessible in a dedicated Saved view.
- **Search & Filtering**: Filter by category/folder, feed, tag, depth, relevance threshold, cluster, and state (Unread / Starred / All / Expired log); full-text search across titles, summaries, takeaways, tags, and content.

### 4.6 Distributed Sync & Detached Mode
- **Downstream Sync (Server → Client)**: Clients pull incremental updates (feeds, categories, curated items, clusters, LLM metadata, curation settings) over the home LAN and persist them in a local client cache.
- **Detached Reading**: When away from the home LAN, clients operate seamlessly off locally cached data. Opening an article or primary-source link launches the external URL directly via the client's internet connection.
- **Upstream Sync (Client → Server)**: While detached, clients record and queue mutations locally and sync them idempotently back to the server upon reconnecting to the home LAN:
  - Reading actions: mark read/unread, star/unstar, mark cluster read.
  - Explicit feedback: relevance votes, notes/reasons, research hints (e.g., "fetch PDFs here").
  - Content-related configuration changes: unsubscribe/mute/pause feed, update feed category/expiration/research settings, edit curation preferences.
- **Conflict Resolution**: Deterministic, timestamped reconciliation (per-field / per-action last-write-wins with idempotent mutation IDs) so offline edits from desktop and Android never corrupt state or duplicate actions.

### 4.7 Client Reading Experience
- Classic RSS reader workflow enhanced with smart metadata views (category/feed navigation, relevance-sorted or chronological item list, detail pane showing TL;DR, takeaways, depth badge, tags, relevance + why, cluster siblings, and mini-research context).
- Browser-centric / browser-friendly UX allowing effortless opening of original article URLs and referenced primary sources in browser tabs.
- Keyboard shortcuts for fast triage on desktop (navigate, mark read, star, open in browser, give feedback).

## 5. Non-Functional Requirements
- **Network Model**: Local LAN / home Wi-Fi first; automatic non-blocking detection of server reachability (Connected vs. Detached indicator) with seamless transition between modes.
- **Local LLM Runtime & Hardware Profile**:
  - Backend talks to `Ollama` or `llama.cpp` via a clean OpenAI-compatible HTTP abstraction so models and runtimes are swappable.
  - Targeted at an AMD Ryzen AI Max+ 395 class machine with 128 GB unified memory (supporting strong local reasoning/summarization models and local embeddings).
  - Fully asynchronous background processing pipeline: ingestion and UI never block on LLM inference; raw items remain visible and usable even if the LLM runtime is temporarily busy or offline, with automatic retry/backfill.
- **Deployment & Packaging**:
  - Core server components packaged as Docker containers (Docker Compose) for clean deployment into a diverse Ubuntu homelab environment.
  - Clean connectivity to a host-native or LAN-hosted Ollama / `llama.cpp` service (avoiding brittle GPU container passthrough requirements).
- **Data Integrity & Backups**: Durable local storage with straightforward backup/export (DB backups + OPML + prompt/config export).
- **Observability**: Clear visibility into ingestion jobs, LLM processing queue, prompt versions, token/latency metrics, mini-research fetches, and feed errors.

## 6. Milestones / Phasing
- **Phase 1 (MVP — Ubuntu + Home LAN Server)**:
  - Containerized backend server (feed ingestion, polite polling, feed health, full-text + PDF/primary-source mini-research, local LLM enrichment & relevance scoring, deduplication/clustering, expiration + full history log, customizable prompts/preferences).
  - Browser-based Linux/Ubuntu client with local offline cache, detached mode, upstream mutation sync, classic RSS views, and explicit feedback controls.
- **Phase 2 (Android Detached Client)**:
  - Android client talking to the same native Sync API, with local caching, detached triage/reading, browser link handoff, and queued upstream sync for read state, feedback, and content-config edits on home Wi-Fi reconnect.
- **Phase 3 (Evolution & Intelligence Upgrades)**:
  - Automated feedback-driven interest profile suggestions, richer multi-step mini-research, semantic search, and optional implicit feedback signals.

## 7. Open Questions
*(To be resolved as we move into `docs/architecture.md` and `docs/data-model.md`)*
1. **Tech Stack Confirmation**: Python (FastAPI + background workers) + PostgreSQL (`pgvector` + full-text search) + TypeScript Web UI (local-first with IndexedDB cache) — does this stack fit your comfort level and Ubuntu/Android goals?
2. **Phase 2 Android Client Form Factor**: Prefer a dedicated lightweight Android app (e.g., Kotlin/Capacitor over plain LAN HTTP with background Wi-Fi sync) or an HTTPS-hosted PWA on the LAN?
3. **LLM Hosting Topology**: Will Ollama / `llama.cpp` run natively on the Ubuntu host (recommended for Ryzen 395+ unified-memory GPU acceleration) with the Dockerized SmartRSS backend calling it over the host/LAN network?
