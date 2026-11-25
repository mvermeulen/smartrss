# Component Design 1c: Local-First Ubuntu Reader UI, Detached Offline Shell & System Health

> **Status:** **In Design Review** *(Drafted 2026-11-25 on branch `milestone/1c`; review findings `U-01`–`U-21` and owner decisions `Q-1`–`Q-4` addressed 2026-11-25 — awaiting verification)*
> **Milestone:** `1c` — Local-First Ubuntu Reader UI & Detached Mode ([Requirements §6](../requirements.md#6-milestones--phasing))
> **Requirements Covered:** `FR-FEED-01` (UI for feeds, categories, OPML), `FR-FEED-03` (Feed Health UI), `FR-STATE-01`–`02` (item state views, Starred/Archive, restore expired), `FR-STATE-03` (bounded bulk mark-read UI; cluster grouping scaffolded for `1e`), `FR-STATE-04` (connected archive FTS + detached local search), `FR-SYNC-01`–`04` (bootstrap, pull, outbox, 48h replay journal, restore recovery, rejection rollback UX, offline/online gating), `FR-UI-01`–`02` (classic 3-pane reader, sortable headers, filters, keyboard shortcuts), `NFR-SEC-01` (device pairing & revocation UI), `NFR-SEC-03` (DOMPurify single-parse sanitization, remote image blocking, strict CSP, XSS-safe snippets), `NFR-SYNC-01`–`02` (gap-free cursor rules, 500-mutation flush `< 5 s`, `navigator.storage.persist()`, Service Worker offline reload), `NFR-OPS-01` (System Health & Pipeline Metrics UI)
> **Related ADRs:** [ADR 0002](../decisions/0002-native-sync-api-and-detached-client-model.md), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md), [ADR 0005](../decisions/0005-phase1-client-origin-and-offline-shell.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md)
> **Prerequisite Specs:** [`1a-backend-ingestion-and-security.md`](1a-backend-ingestion-and-security.md), [`1b-sync-protocol-and-search.md`](1b-sync-protocol-and-search.md)

---

## 1. Scope & Boundaries

Milestone `1c` delivers the Phase 1 Ubuntu laptop local-first reader SPA, its fixed `localhost` offline-capable static runner and Service Worker shell, the Dexie.js IndexedDB cache and persistent mutation outbox, detached local search, single-parse DOMPurify + strict CSP untrusted-content rendering, device pairing/management, feed/OPML management, and the System Health & Pipeline Performance Metrics UI — consuming the `1a` and `1b` REST/sync APIs with **zero database migrations or breaking backend API changes**.

### In Scope for `1c`
1. **Frontend Application (`frontend/`)**:
   - React 19 + TypeScript (`strict: true`) + Vite 6 + Tailwind CSS v4 (static build-time CSS) + Dexie.js + DOMPurify + MiniSearch, built reproducibly from `package-lock.json` (`npm ci`).
   - **Device Pairing & Auth Lifecycle**: Initial pairing screen (`POST /api/v1/auth/pair`), paired devices list, one-time pairing code generator (`POST /api/v1/auth/pairing-codes`), device revocation (`POST /api/v1/devices/{id}/revoke`), origin-change confirmation guard (`U-08`), and graceful `401` auth-revoked recovery without destroying pending offline mutations (`U-11` fresh UUID minting on re-pair).
   - **Dexie.js Local Store & Storage Persistence**: IndexedDB schema with separated **server-base** (`items_base`, `feeds_base`) and **display** (`items`, `feeds`) tables (`U-01`), numeric `starred_num` (`0 | 1`) indexing (`U-03`), canonical 24-char UTC timestamp normalization (`U-07`), resumable two-phase streaming bootstrap tables (`U-06`), `db.on('versionchange')` handling (`U-14`), `navigator.storage.persist()` startup request + denial warning banner, and local 14-day read/expired eviction respecting active outbox references.
   - **Client Sync Engine & Persistent Outbox**: Cross-tab lock coordination (`navigator.locks` on secure origins + IndexedDB lease lock fallback on insecure LAN origins, `U-05`) + `BroadcastChannel` with no-lost-wakeup re-check (`U-09`), streaming resumable multi-page `bootstrap` pinned to Page 1 `watermark` (`U-06`), FIFO chunked `push` (`<= 500` mutations / `<= 5,000` explicit `item_ids` / `<= 1.5 MiB` body), skew-safe poison-pill bisection on `422`/`413` (`U-04`), 48-hour acknowledged replay journal (including chunked `scope='items'` preservation of scoped bulk reads `<= 5,000` IDs, `U-10`/`Q-3`), restore recovery on both `epoch_mismatch` and `cursor_ahead`, pure deterministic `display = base ⊕ outbox` reconciliation (`U-01`), `no-cors` misconfiguration probe (`U-17`), and `server_now` clock-drift detection (`> 5 min`).
   - **Service Worker Offline Shell**: Small, auditable Vite-built `sw.ts` with build-time hashed precache manifest, cache-first `/assets/*` and cache-first `/index.html` navigation (`U-14`), user-gated update activation (triggered automatically on build or `SMARTRSS_API_ORIGIN` change, `U-08`), and strict network-only bypass for `/api/*`, cross-origin LAN API calls, and `/client-config.json`.
   - **Connected & Detached Search**: Connected full-archive FTS via `GET /api/v1/search` (display-only per `1b` `P-17`, with `target_absent` mutation overlay per `U-12`) and detached local search via `MiniSearch` hydrated incrementally from Dexie, sharing one XSS-safe `SnippetFragment[][]` renderer.
   - **Classic 3-Pane Reader UX**: Left category/feed sidebar with unread & health badges, sortable middle item list/table (`Relevance`, `Date`, `Feed`, `Depth`) with filters and bounded bulk mark-read, right Reading Pane with single-parse DOMPurify-sanitized `raw_summary` (`RETURN_DOM_FRAGMENT`, remote images blocked by default, relative links resolved against `baseUrl`, `U-13`), plain-text `extracted_text` on-demand viewer, safe external browser handoff, offline feed controls, forward-compatible `1d`–`1g` metadata slots, and full keyboard navigation.
   - **Management & Observability Views**: Online-only Feed/Category CRUD + OPML import/export, diff-only `EditFeedModal` `PATCH` (`U-21`/`D3`) + optimistic-versioned `curation_instructions` editor (`409` conflict diff UX), Global `app_settings` editor, Feed Health table with manual "Poll Now", System Health overview + job queue counts + `24h`/`7d`/`30d` stage latency & per-feed cost metrics + recent `pipeline_runs` history, and Dead-Letter Queue retry/dismiss controls.
2. **Localhost Runner & Ubuntu Desktop Packaging (`client-runner/`, `deploy/`, `scripts/`)**:
   - Zero-dependency Python 3.12 stdlib static HTTP server (`client-runner/smartrss_client_runner.py`) bound to `127.0.0.1:4180` (`U-16`, avoiding Vite's `4173` preview default), canonicalizing `127.0.0.1` / `[::1]` → `http://localhost:4180`, enforcing strict `Host` allow-list, path-traversal protection, origin-tag injection into `/sw.js` (`U-08`), and strict CSP with `connect-src` pinned to the configured LAN API origin.
   - Idempotent installer/updater (`scripts/install-client.sh` + `make install-client`, `U-18`), user `systemd` service (`deploy/systemd/smartrss-client.service` with `WantedBy=default.target`), env template (`smartrss-client.env.example`), and XDG desktop launcher (`deploy/desktop/smartrss.desktop`).
3. **Docker `web` Fallback & Tooling Integration**:
   - Multi-stage `frontend/Dockerfile` (`node:22-alpine` → `nginx:1.27-alpine`) wired into `docker-compose.yml` `web` service with updated `deploy/nginx/nginx.conf` + `deploy/nginx/security-headers.conf` serving the compiled SPA as a **full connected reading & mutation LAN fallback** (`Q-2`, ADR 0005).
   - Default `CORS_ALLOW_ORIGINS` updated in `.env.example`, `docker-compose.yml`, and `backend/src/smartrss/config.py` from `http://localhost:8080` to `http://localhost:8080,http://localhost:4180`.
   - Preflight dependency checker (`scripts/check-prereqs.sh --mode dev|server|client|e2e`, `Q-4`), `Makefile` targets (`frontend-lint`, `frontend-typecheck`, `frontend-test-cov`, `runner-test`, `frontend-e2e`, `install-client`), and two-tier frontend coverage gate (`scripts/check-frontend-coverage.mjs`: `>= 90%` on 6 critical sync/security modules, `>= 80%` on 4 other core modules, `Q-1`).

### Explicitly Out of Scope for `1c`
- Backend DB schema migrations or new API endpoints (all needed endpoints already exist from `1a` and `1b`).
- Local LLM enrichment, Prompt Config / Interest Profile editors, and LLM phase telemetry (`prompt_eval_ms`/`generation_ms`) (`1d` — `1c` renders clean forward-compatible slots whenever `item.enrichment` is populated).
- Story clustering UI actions (`split_cluster`, `mark_cluster_read`) and cluster generation (`1e` — `clusters` table and `item.cluster_id` are stored forward-compatibly in Dexie).
- Mini-research extraction and "Dig Deeper" trigger (`1f` — `research_policy.enabled` and `research_policy.fetch_pdfs` offline toggles are wired in `1c`; `research_sources[]` renders if present).
- Explicit item feedback (`thumbs_up`/`thumbs_down`), inline `feed_instruction` notes, and offline eval harness (`1g`).
- Phase 2 Android Capacitor/Kotlin packaging (UI layout stays responsive and Capacitor-compatible, without Android build artifacts).

---

## 2. Key `1c` Design Trade-offs & Decisions

Decisions `1c-D01`–`1c-D04` and owner review decisions `Q-1`–`Q-4` were explicitly confirmed by the project owner on 2026-11-25 (resolving [Requirements §7](../requirements.md#7-open-questions) Open Question #1).

| # | Decision Area | Chosen Approach | Alternative(s) Considered | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **1c-D01** *(Req §7 #1, `U-16`, `Q-2`)* | **Phase 1 `localhost` client packaging & LAN fallback scope** *(Owner-confirmed)* | **Zero-dependency Python 3.12 stdlib static HTTP server (`client-runner/smartrss_client_runner.py`) on fixed `http://localhost:4180` + systemd user unit + `.desktop` launcher + `scripts/install-client.sh`**. Docker `web` Nginx serves the same bundle as a **full connected reading & mutation LAN fallback** (`Q-2`), using `crypto.getRandomValues` UUIDv4 and an IndexedDB lease lock fallback (`U-05`). | Tauri v2 desktop wrapper; port `4173` (collides with `vite preview`); read-only LAN fallback. | Zero native Rust/WebKitGTK friction, guaranteed `localhost` secure context for SW + `navigator.storage.persist()` (ADR 0005), port `4180` avoids colliding with `vite preview` (`4173`), and full connected mutations work seamlessly from any LAN desktop browser. |
| **1c-D02** *(`U-13`)* | **Remote images & untrusted feed HTML (`raw_summary`)** *(Owner-confirmed)* | **Block remote images by default** (`img-src 'self'`) using a **single-pass DOMPurify `RETURN_DOM_FRAGMENT`** pipeline with an `uponSanitizeElement` hook replacing `<img src="http(s)://...">` with an inert alt-text / external-link placeholder and resolving relative `<a href>` against `baseUrl`. | Allow remote HTTPS images; two-pass `DOMParser` + `dangerouslySetInnerHTML`. | Eliminates tracking pixels, broken detached images, CSP console noise, AND double-parse mXSS surface (`U-13`). |
| **1c-D03** *(`U-19`, `Q-4`)* | **Frontend styling, toolchain & preflight checks** *(Owner-confirmed)* | **React 19 + TypeScript (`strict: true`) + Vite 6 + Tailwind CSS v4 (static CSS) + `npm` (`package-lock.json` + `npm ci`)**. Host-native `make check` (`node >= 22` + `npm`, optional `FRONTEND_RUNTIME=docker`) guarded by **`scripts/check-prereqs.sh --mode dev|server|client|e2e`**. Zero inline `style=""` HTML attributes (strict `style-src 'self'`); ESLint forbids JSX `style={...}` except an audited virtualized list if needed. | Plain CSS Modules; Docker-only `make check` without preflight checks. | Reproducible builds (`T5`), fast host edit-test loop with clear upfront dependency verification, and strict CSP compliance without misstating how CSSOM interacts with `style-src` (`U-19`). |
| **1c-D04** *(`U-20`)* | **Detached local search engine** *(Owner-confirmed)* | **`MiniSearch` in-memory weighted inverted index** hydrated incrementally from Dexie, paired with a pure `websearch`-compatible query parser (`"phrases"`, `-negation`, negation-only rejection) and structured XSS-safe `SnippetFragment[][]` extractor (no stemming in Phase 1, documented). | Dexie `multiEntry` token table; zero-dependency scan. | Fast weighted BM25 search across the working set without bloating IndexedDB storage, and emits the exact `SnippetFragment[][]` shape returned by `GET /api/v1/search` so connected and detached search share one safe React renderer. |
| **1c-D05** *(`U-08`)* | **CSP `connect-src` pinning & post-pairing origin change** | Local runner validates `SMARTRSS_API_ORIGIN` at startup (default `http://localhost:8080`), emits `connect-src 'self' <api_origin>`, serves `GET /client-config.json` (`no-store`), and injects an `origin_tag` comment into `/sw.js`. If `config.api_origin !== auth.api_origin`, sync pauses with **zero** authenticated requests until the user confirms. Docker `web` emits `connect-src 'self'` with `api_origin: ""`. | Broad `connect-src 'self' http: https:`; silently sending bearer tokens to a changed origin. | Strict single-origin CSP pinning, prevents leaking production tokens to a test server, and automatically refreshes precached `/index.html`'s CSP header via SW update when the server IP changes. |
| **1c-D06** *(`U-14`)* | **Service Worker implementation & coherent update model** | **Small, auditable custom `sw.ts`** (~130 LOC) with Vite build-time precache manifest. **Cache-first** for both `/assets/*` and navigations (`/index.html`), with user-gated `"Update available — Reload"` (`SKIP_WAITING`). Strictly bypasses `/api/*`, cross-origin API calls, and `/client-config.json`. | Network-first navigation with user-gated `SKIP_WAITING` (contradictory); Workbox default caching. | Guarantees every tab runs code matching its active SW, eliminates mixed-version tab skew, never caches sensitive API responses, and pairs with `db.on('versionchange')` for smooth future Dexie schema upgrades. |
| **1c-D07** *(`U-01`, `U-02`, `U-05`, `U-09`)* | **Multi-tab sync safety & server-base + overlay state model** | Serialize sync via `navigator.locks` (secure context) or IndexedDB lease lock (insecure fallback) + `BroadcastChannel` with pre-release outbox re-check (`U-09`). Separate **`items_base`/`feeds_base`** from display **`items`/`feeds`**: display is always pure `base ⊕ fold(outbox, local_seq ASC)`. Drop `in_flight` status completely (`U-02`). | Local `previous_snapshot` rollback; `in_flight` outbox state; uncoordinated tabs. | Eliminates stale-snapshot rollback corruption, cross-field clobber, pull flicker, stuck mid-crash `in_flight` rows, and lost multi-tab wake-ups. |
| **1c-D08** *(`U-04`, `U-10`, `U-11`, `Q-3`)* | **Auth lifecycle, skew-safe bisection & restore journaling** | Store auth in Dexie `client_meta`. On `401`, pause sync (`auth_revoked`) preserving outbox; on re-pair, mint fresh `client_mutation_id`s (`U-11`). On batch `422`/`400`, quarantine a mutation **only** when its sibling half succeeded (`<= 3`/cycle); systematic `422` halts in `sync_error` preserving outbox (`U-04`). On `applied` of scoped bulk read (`<= 5,000` IDs), journal chunked `scope='items'` into 48h `replay_journal`; promote journal on both `epoch_mismatch` and `cursor_ahead` (`U-10`/`Q-3`). | Blindly bisecting systematic `422`s; dropping acknowledged scoped bulk reads on restore; keeping old `client_mutation_id`s after re-pair. | Prevents version skew from wiping an offline session, avoids false `mutation_id_conflict` rejection toasts on re-pair, and preserves post-backup bulk reads across restores. |

---

## 3. Repository Layout, Tooling & Build Pipeline

### 3.1 Directory & Module Structure

```text
smartrss/
├── client-runner/
│   ├── smartrss_client_runner.py          # Zero-dep Python 3.12 stdlib localhost static server (127.0.0.1:4180)
│   └── tests/
│       └── test_client_runner.py          # pytest suite: Host check, 308 localhost redirect (127.0.0.1 & [::1]), HEAD, CSP injection, path traversal (incl. %2e%2e), sw.js origin tag
├── deploy/
│   ├── nginx/
│   │   ├── nginx.conf                     # Static Vite SPA + /api/ proxy + Cache-Control + /client-config.json
│   │   ├── security-headers.conf          # Included in every location block (prevents Nginx add_header inheritance drop)
│   │   └── client-config.json             # Static `{"mode":"lan_fallback","api_origin":""}`
│   ├── systemd/
│   │   ├── smartrss-client.service        # User systemd unit (WantedBy=default.target) launching smartrss_client_runner.py
│   │   └── smartrss-client.env.example    # SMARTRSS_API_ORIGIN, SMARTRSS_CLIENT_PORT=4180, SMARTRSS_DIST_DIR
│   └── desktop/
│       └── smartrss.desktop               # XDG desktop launcher opening http://localhost:4180
├── frontend/
│   ├── Dockerfile                         # Multi-stage: node:22-alpine (npm ci + build) -> nginx:1.27-alpine
│   ├── package.json
│   ├── package-lock.json                  # Committed lockfile for reproducible builds (T5)
│   ├── tsconfig.json                      # strict: true, noUncheckedIndexedAccess: true, exactOptionalPropertyTypes: true
│   ├── vite.config.ts                     # React + Tailwind v4 + custom SW manifest emitter; server.port=5173, preview.port=4173 (distinct from 4180)
│   ├── vitest.config.ts                   # Vitest + jsdom + fake-indexeddb + v8 coverage JSON report
│   ├── playwright.config.ts               # Two E2E projects: (1) localhost:4180 runner + real API/PG; (2) Nginx web fallback on http://smartrss.test:8080
│   ├── eslint.config.js                   # ESLint flat config (TS strict, React hooks, forbid inline `style` prop except audited virtual list, forbid dangerouslySetInnerHTML)
│   ├── index.html                         # Zero inline script/style; loads /src/main.tsx & /manifest.webmanifest
│   ├── public/
│   │   ├── manifest.webmanifest
│   │   └── icons/
│   │       ├── icon-192.png
│   │       ├── icon-512.png
│   │       └── icon.svg
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── styles/app.css                 # Tailwind v4 `@import "tailwindcss";` + light/dark CSS tokens + prose rules
│       ├── types/
│       │   ├── api.ts                     # Exact TS mirrors of 1a/1b REST & system schemas
│       │   ├── sync.ts                    # Exact TS mirrors of SyncBootstrap/Pull/Push DTOs & mutation union
│       │   └── ui.ts                      # View filters, sort keys, connection/sync state enums
│       ├── security/
│       │   ├── uuid.ts                    # uuidv4() with crypto.randomUUID -> crypto.getRandomValues fallback (U-05)
│       │   ├── urlSafety.ts               # isSafeHttpUrl(), resolveSafeHttpUrl(), safeExternalHref() (S4, U-13)
│       │   └── sanitizeHtml.ts            # Single-pass DOMPurify RETURN_DOM_FRAGMENT + uponSanitizeElement img placeholder + relative link resolution (NFR-SEC-03, U-13)
│       ├── db/
│       │   ├── schema.ts                  # Dexie v1 schema (base + display + streaming bootstrap + outbox + replay_journal) + versionchange handler (U-01, U-03, U-06, U-14)
│       │   └── clientMeta.ts              # Atomic meta/auth/cursor helpers + persistence request
│       ├── api/
│       │   ├── normalize.ts               # toCanonicalIso() & recursive DTO timestamp normalizer (U-07, D6)
│       │   └── client.ts                  # Typed fetch client (Bearer auth, strict CORS-safe headers, timeouts, typed errors)
│       ├── sync/
│       │   ├── lock.ts                    # withSyncLock(): navigator.locks + IndexedDB lease lock fallback (U-05)
│       │   ├── probe.ts                   # /api/v1/health reachability + no-cors misconfiguration detector (U-17)
│       │   ├── reconciliation.ts          # Pure base ⊕ outbox recompute, pull apply, eviction, epoch_mismatch/cursor_ahead rewrite, clock drift (U-01, U-10)
│       │   ├── outbox.ts                  # Optimistic enqueue (incl. target_absent U-12), FIFO chunking, skew-safe bisection (U-04), 48h replay journal, fresh-ID re-pair (U-11)
│       │   └── engine.ts                  # Lock-serialized coordinator, BroadcastChannel + pre-release dirty re-check (U-09), streaming bootstrap (U-06), push/pull loop
│       ├── search/
│       │   ├── queryParser.ts             # Pure websearch-compatible parser ("phrase", -negation, negation-only error)
│       │   └── localSearch.ts             # MiniSearch weighted index + structured XSS-safe SnippetFragment[][] generator
│       ├── sw/
│       │   ├── sw.ts                      # Minimal auditable Service Worker (cache-first assets & navigate; bypass /api/* & /client-config.json)
│       │   └── register.ts                # Registration & user-gated update banner hook (skips gracefully on insecure origins)
│       ├── hooks/
│       │   ├── useSyncEngine.ts
│       │   ├── useReaderItems.ts
│       │   ├── useSearch.ts
│       │   ├── useKeyboardShortcuts.ts
│       │   └── useStoragePersistence.ts
│       ├── components/
│       │   ├── layout/                    # TopBar, AlertStrip, AppShell
│       │   ├── auth/                      # PairingScreen, AuthRevokedBanner, OriginChangeBanner
│       │   ├── reader/                    # Sidebar, ItemList, ItemRow, ReadingPane, SafeHtml, SnippetText, BulkReadMenu, SmartMetadataCard
│       │   ├── feeds/                     # FeedManagerView, AddFeedModal, EditFeedModal, ConflictDiffDialog, CategoryManager, OpmlPanel
│       │   ├── health/                    # SystemHealthView, StageMetricsTable, FeedCostTable, PipelineRunsTable, FeedHealthTable, DeadLetterTable
│       │   ├── settings/                  # SettingsDevicesView, AppSettingsForm, DevicesPanel, OutboxInspector
│       │   └── common/                    # StatusPill, ConfirmDialog, ToastStack, ShortcutsModal
│       └── test/
│           ├── unit/                      # Vitest + fake-indexeddb + fast-check specs
│           ├── fixtures/                  # Hostile XSS & mXSS HTML fixtures, recorded sync/search/metrics payloads
│           └── e2e/                       # Playwright E2E specs (localhost:4180 + Nginx smartrss.test:8080)
└── scripts/
    ├── check-prereqs.sh                   # Preflight dependency checker (--mode dev|server|client|e2e) with exact install hints (Q-4)
    ├── install-client.sh                  # Idempotent laptop runner installer/updater/rollback (U-18)
    └── check-frontend-coverage.mjs        # Two-tier coverage gate (>= 90% critical 5, >= 80% remaining 4 core modules; Q-1)
```

### 3.2 Environment, Preflight Checks, CORS & Build Tooling (`Q-4`)
- **Preflight Dependency Verification (`scripts/check-prereqs.sh`)**:
  - Wired into `Makefile` before build/deploy/check targets so missing or mismatched dependencies fail immediately with clear Ubuntu install commands:
    - `--mode server` (`make up`, `make migrate`): verifies `docker` and `docker compose` (no host Node/Python required; images compile inside containers with `uv sync --frozen` / `npm ci`).
    - `--mode client` (`make install-client`): verifies `python3 >= 3.12` and `systemctl --user`; if building `dist/` locally (neither `--from-dist` nor `--from-docker`), also verifies `node >= 22` and `npm`.
    - `--mode dev` (`make check`): verifies `uv`, `python3 >= 3.12`, `docker`, `node >= 22`, `npm` (and `frontend/node_modules` installed from `package-lock.json`).
    - `--mode e2e` (`make test-e2e`): all `--mode dev` checks plus Playwright Chromium binary availability.
- **Host-Native `make check` with Optional Container Escape Hatch (`Q-4`)**:
  - Default `FRONTEND_RUNTIME ?= host` runs `npm --prefix frontend ...` directly on the Ubuntu host using Node `>= 22` + `npm` (`npm ci` from committed `package-lock.json` for `T5` reproducibility).
  - Setting `FRONTEND_RUNTIME=docker` runs `frontend-lint`, `frontend-typecheck`, and `frontend-test-cov` inside a pinned `node:22-alpine` container for environments without host Node.
  - Production Docker `web` service always builds inside `frontend/Dockerfile` (`node:22-alpine` + `npm ci` → `nginx:1.27-alpine`).
- **Backend CORS Default Update (Minor `1c` Infra Touchpoint)**:
  - Update default `CORS_ALLOW_ORIGINS` in `backend/src/smartrss/config.py`, `.env.example`, and `docker-compose.yml` from `http://localhost:8080` to `http://localhost:8080,http://localhost:4180` (retaining strict no-wildcard validation).
  - Document in `README.md` / `.env.example` that LAN deployments should add their server LAN hostname/IP to `ALLOWED_HOSTS` (e.g., `smartrss.lan:8080,192.168.1.50:8080`) and any LAN fallback origin to `CORS_ALLOW_ORIGINS` (e.g., `http://smartrss.lan:8080,http://localhost:4180`).
- **CORS-Safe Request Header Invariant**:
  - Backend `CORSMiddleware` (`backend/src/smartrss/api/app.py`) explicitly allows `allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]`, `allow_headers=["Authorization", "Content-Type"]`, `allow_credentials=False`.
  - `frontend/src/api/client.ts` MUST always set `credentials: "omit"` and `cache: "no-store"` (fetch option, never a custom header), and send ONLY `Authorization`, `Accept`, and `Content-Type: application/json` (on mutation requests).
- **`Makefile` Targets**:
  - `make check-prereqs`: runs `./scripts/check-prereqs.sh --mode dev`
  - `make frontend-lint`: `npm --prefix frontend run lint` (ESLint + Prettier check)
  - `make frontend-typecheck`: `npm --prefix frontend run typecheck` (`tsc --noEmit`)
  - `make frontend-test-cov`: `npm --prefix frontend run test:cov && node scripts/check-frontend-coverage.mjs frontend/coverage/coverage-summary.json`
  - `make runner-test`: `uv run --directory backend pytest ../client-runner/tests --cov=../client-runner/smartrss_client_runner --cov-branch --cov-fail-under=90`
  - `make test-e2e`: runs `./scripts/check-prereqs.sh --mode e2e`, starts `db-test`, launches an ephemeral FastAPI instance + `smartrss_client_runner.py` (and Nginx `web` fallback project), and runs `npm --prefix frontend run test:e2e`.
  - `make install-client`: runs `./scripts/install-client.sh` (supports `INSTALL_CLIENT_ARGS="--from-docker"` or `--rollback`).
  - `make check`: Runs `check-prereqs`, backend (`lint`, `typecheck`, `test-cov`), `runner-test`, and frontend (`frontend-lint`, `frontend-typecheck`, `frontend-test-cov`).

---

## 4. Localhost Client Runner, Nginx Fallback & Strict CSP

### 4.1 `client-runner/smartrss_client_runner.py`
A single-file, zero-dependency Python 3.12 stdlib `ThreadingHTTPServer` serving the compiled static SPA bundle on the user's Ubuntu laptop:

| Behavior | Specification |
| :--- | :--- |
| **Bind Address & Port (`U-16`)** | Strictly binds `127.0.0.1` on `SMARTRSS_CLIENT_PORT` (default **`4180`**, configurable `1024..65535`; distinct from Vite dev `5173` and `vite preview` `4173`). Never binds `0.0.0.0`. |
| **Strict `Host` Validation** | Accepts ONLY `localhost:<port>`, `127.0.0.1:<port>`, or `[::1]:<port>` (case-insensitive). Any other `Host` header returns `400 Bad Request` (`text/plain`), blocking DNS rebinding against the local runner itself. |
| **Origin Canonicalization (`308`)** | Any request whose `Host` is `127.0.0.1:<port>` or `[::1]:<port>` receives `308 Permanent Redirect` to `http://localhost:<port><safe_path_and_query>` (normalizing leading `//` to `/` to prevent open redirect). Guarantees one browser origin (`http://localhost:4180`) so IndexedDB and the Service Worker are never split. |
| **`SMARTRSS_API_ORIGIN` Validation** | Parsed at startup via `urllib.parse.urlsplit`: MUST be `http` or `https`, non-empty `hostname`, no `username`/`password`, `path` in `("", "/")`, empty `query` and `fragment`, and zero ASCII control chars / whitespace / `;` / `,` (prevents CSP header injection). Normalized to `scheme://host[:port]` (default `http://localhost:8080`). Startup aborts non-zero on invalid value. |
| **`GET /client-config.json`** | Dynamically returns `{"mode":"localhost","api_origin":"<validated_origin>"}` with `Content-Type: application/json; charset=utf-8` and `Cache-Control: no-store`. |
| **`/sw.js` Origin Tag (`U-08`)** | Computes `origin_tag = sha256(api_origin.encode()).hexdigest()[:12]` at startup and appends `\n// smartrss-origin-tag:<origin_tag>\n` when serving `/sw.js` (with `Cache-Control: no-cache`). When `SMARTRSS_API_ORIGIN` changes and the runner restarts, browsers detect the byte-level `/sw.js` change, install a fresh Service Worker cache with the updated `/index.html` CSP header (`fetch('/index.html', { cache: 'reload' })`), and show the Reload banner. |
| **Path Traversal & Symlink Guard (`S1`/`S5`)** | Supports ONLY `GET` and `HEAD` (`405` otherwise). Percent-decodes URL path once, rejects null bytes, backslashes, raw or encoded `..` segments (`%2e%2e`, double-encoded `%252e%252e` rejected via normalized segment check), resolves candidate with `Path.resolve()`, and asserts `candidate.is_relative_to(dist_root)` (`dist_root = Path(dist_dir).resolve()`). Directory listing is completely disabled. |
| **Extension-Aware SPA Fallback** | Existing regular file under `dist_root` → serve with explicit safe MIME types (`.html`, `.js`, `.css`, `.json`, `.webmanifest`, `.svg`, `.png`, `.woff2`, `.ico`). Missing path → `404 Not Found` if the last path segment contains a file extension (e.g., `/assets/missing.js` never returns HTML as JS!), else serve `dist_root/index.html` (`200 OK`) for SPA routes. |
| **Cache-Control Matrix** | `/client-config.json` → `no-store`; `/index.html`, `/sw.js`, `/manifest.webmanifest` (and SPA fallback routes) → `no-cache`; `/assets/*` (Vite content-hashed) → `public, max-age=31536000, immutable`. |
| **Graceful Shutdown (`L2`)** | Handles `SIGTERM` and `SIGINT` cleanly by closing the listening socket and exiting `0`. |

### 4.2 Exact Security Headers & Content Security Policy

Both `smartrss_client_runner.py` and Docker `web` (`deploy/nginx/security-headers.conf`) emit on **every** response:

```http
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
```

- **Localhost Runner CSP (`http://localhost:4180`)**:
  ```http
  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self' <SMARTRSS_API_ORIGIN>; manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';
  ```
- **Docker `web` Fallback CSP (`deploy/nginx/security-headers.conf`)**:
  ```http
  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';
  ```
- **Nginx `add_header` Inheritance Guard**: All security headers live in `deploy/nginx/security-headers.conf`, explicitly `include`d inside **every** `location` block (`/`, `/assets/`, `= /sw.js`, `= /index.html`, `= /client-config.json`, `/api/`).
- **Inline Markup & Style Discipline (`U-19`)**:
  - `style-src 'self'` (without `'unsafe-inline'`) blocks `style="..."` attributes in parsed HTML/SVG and `setAttribute('style', ...)` (which is why `index.html` contains zero inline `<script>` or `style=` attributes and DOMPurify forbids `style` attributes in untrusted feed HTML). CSSOM writes (`el.style.prop = ...`) are not blocked by `style-src`.
  - ESLint forbids `dangerouslySetInnerHTML` across the entire codebase (zero uses — `SafeHtml.tsx` mounts a DOMPurify `DocumentFragment` directly via `replaceChildren()`, `U-13`), and forbids JSX `style={...}` props across `frontend/src/` as a clean styling convention, with one explicit audited ESLint carve-out allowed solely if a virtualized list container uses CSSOM `transform` positioning for row virtualization.
  - Playwright E2E attaches a `document.addEventListener("securitypolicyviolation", ...)` collector and **fails the test** on any unexpected CSP violation (and separately verifies in `1c-T12` that `fetch('https://other.example')` DOES trigger a `securitypolicyviolation`, proving `connect-src` pinning is enforced!).

### 4.3 Ubuntu Laptop Installation, Updates & Rollback (`U-18`)
- **`scripts/install-client.sh`** (invoked via `make install-client`):
  1. Runs `scripts/check-prereqs.sh --mode client`.
  2. Obtains `dist/` via one of three idempotent modes:
     - Default (repo with Node 22 + npm): runs `npm --prefix frontend ci && npm --prefix frontend run build` → `frontend/dist`.
     - `--from-dist <path>`: copies a pre-built static bundle (zero Node/Docker needed on the laptop).
     - `--from-docker`: extracts `/usr/share/nginx/html` from the built `web` Docker image into a staging directory.
  3. Atomically updates `~/.local/share/smartrss/`:
     - Copies bundle to `dist.new`, moves current `dist` → `dist.prev` (removing older `dist.prev`), renames `dist.new` → `dist` (enabling 1-command rollback via `./scripts/install-client.sh --rollback`).
     - Copies `client-runner/smartrss_client_runner.py` to `~/.local/share/smartrss/smartrss_client_runner.py`.
  4. Installs `~/.config/smartrss-client/config.env` from `deploy/systemd/smartrss-client.env.example` **only if absent** (never overwrites user's configured `SMARTRSS_API_ORIGIN`).
  5. Installs `~/.config/systemd/user/smartrss-client.service` (with `[Install]\nWantedBy=default.target`, `Restart=on-failure`) and `~/.local/share/applications/smartrss.desktop` (`Exec=xdg-open http://localhost:4180`).
  6. Runs `systemctl --user daemon-reload && systemctl --user enable --now smartrss-client.service` (or `restart` on update so the runner immediately serves the new `dist/` and updated `/sw.js`, triggering the client's `"Update available — Reload"` prompt per `U-14`).
- Full install, update, origin-change, and rollback commands are documented in root `README.md`.

---

## 5. Local Data Model (Dexie.js IndexedDB Schema & Storage Persistence)

### 5.1 Dexie v1 Schema (`frontend/src/db/schema.ts`)

Database name: `smartrss_v1`.
- **Canonical Timestamp Invariant (`U-07`, Checklist `D6`)**: Every timestamp stored in Dexie is normalized at the `api/client.ts` boundary via `toCanonicalIso(raw)` (`new Date(raw).toISOString()` → fixed-width 24-char UTC `YYYY-MM-DDTHH:mm:ss.sssZ`, including nested `feeds.field_updated_at` values), guaranteeing exact lexicographical ordering in IndexedDB indexes and string comparisons across all Pydantic/Python ISO formats (`...Z`, `....123456Z`, `...+00:00`).
- **Server-Base + Derived Display Separation (`U-01`)**: `items_base` and `feeds_base` hold the authoritative server-confirmed records (written ONLY by `bootstrap`, `pull`, and `push` `current_entity` merges). `items` and `feeds` hold the indexed display records, always computed inside the same Dexie `rw` transaction by the single pure function `display = base ⊕ fold(all outbox rows targeting entity, local_seq ASC)`.
- **Numeric Starred Index (`U-03`)**: Because IndexedDB cannot index booleans (booleans are invalid IDB keys and throw `DataError`), Dexie `items` stores `starred_num: 0 | 1` derived from `is_starred ? 1 : 0` at the write boundary and indexes `starred_num` / `[starred_num+effective_date]`.
- **Expected Working-Set Design Envelope (`U-06`, `U-20`)**: Steady-state target is ~2,000–6,000 items (~200 items/day × 30d default TTL + starred + 14d read); streaming bootstrap (`§6.3`) and chunked eviction handle up to 20,000+ working-set items without OOM.

| Table | Primary Key | Indexes | Stored Shape & Purpose |
| :--- | :--- | :--- | :--- |
| **`client_meta`** | `key` | *(none)* | Singleton rows: `'auth'` (`{device_id, device_name, platform, bearer_token, api_origin, paired_at}`), `'sync'` (`{instance_epoch, sync_cursor, active_gen, last_sync_at, last_server_now, clock_drift_ms}`), `'sync_lock'` (`{owner_tab_id, acquired_at, expires_at}` for insecure-context lease lock fallback, `U-05`), `'app_settings'` (`SyncAppSettingsDTO`), `'interest_profile'` (`{version, profile_markdown} \| null`), `'ui_prefs'` (`{theme, default_sort, density, auto_mark_read_on_select}`). |
| **`categories`** | `id` | `sort_order, name` | Mirrors `SyncCategoryDTO` (`id, name, sort_order, updated_at`). Removed locally on `delete` tombstone. |
| **`feeds_base`** | `id` | *(none)* | Authoritative server-confirmed `SyncFeedDTO` records (`U-01`). |
| **`feeds`** | `id` | `category_id, status, health_state, title` | Derived display `SyncFeedDTO` (`feeds_base ⊕ outbox`). |
| **`clusters`** | `id` | `max_relevance_tier, last_seen_at` | Forward-compatible `1e` table (`id, representative_item_id, title, max_relevance_tier, item_count, last_seen_at`). Empty in `1c`. |
| **`items_base`** | `id` | `bootstrap_gen` | Authoritative server-confirmed `SyncItemDTO` records + `bootstrap_gen: number` (`U-01`, `U-06`). |
| **`items`** | `id` | `feed_id, cluster_id, state, starred_num, fetched_at, published_at, effective_date, read_at, ingest_sync_seq, bootstrap_gen, [state+effective_date], [feed_id+state], [feed_id+effective_date], [starred_num+effective_date]` | Derived display `SyncItemDTO` (`items_base ⊕ outbox`) plus derived `effective_date = published_at ?? fetched_at`, numeric `starred_num: 0 \| 1` (`U-03`), and `bootstrap_gen: number`. |
| **`bootstrap_meta`** | `key` | *(none)* | Streaming bootstrap checkpoint (`key='active'`): `{phase: 'fetching' \| 'committing', target_gen, page1_epoch, page1_watermark, server_now, next_cursor, categories, feeds, clusters, interest_profile, app_settings, pages_fetched, items_staged}` (`U-06`). |
| **`bootstrap_items`** | `id` | *(none)* | Temporary page-streamed staging store for incoming bootstrap `SyncItemDTO`s (`<= 500` rows per short tx; cleared after atomic swap, `U-06`). |
| **`outbox`** | `++local_seq` | `&client_mutation_id, entity_key` | Persistent FIFO queue of unsynced mutations (**no `in_flight` status**, `U-02`): `{local_seq, client_mutation_id, created_at, payload: SyncMutationInput, entity_key: 'item:<id>' \| 'feed:<id>' \| 'bulk', target_absent: boolean, captured_item_ids: string[] \| null, display_label: string, attempts: number, last_error: string \| null}`. |
| **`replay_journal`** | `client_mutation_id` | `acknowledged_at` | Rolling **48-hour** log of server-acknowledged replayable mutations (`item_set_state`, `item_set_starred`, `feed_set_field`, `item_mark_bulk_read` with `scope='items'` — including chunked rewrites of applied scoped bulk reads `<= 5,000` IDs, `U-10`/`Q-3`): `{client_mutation_id, acknowledged_at, payload}`. Pruned on every sync cycle where `acknowledged_at < now - 48h`. |
| **`failed_mutations`** | `client_mutation_id` | `failed_at` | Quarantine table for isolated single-mutation `422`/`400`/`413` poison pills (inspectable with both **Retry** and **Dismiss** actions in Settings → Outbox Inspector, `U-04`). |
| **`sync_notifications`** | `++id` | `created_at, dismissed` | User-visible notifications for `rejected` mutations, poison-pill quarantines, and restore-recovery events (`{id, severity: 'warning' \| 'error' \| 'info', title, detail, reason, mutation_type, entity_label, created_at, dismissed}`). |

### 5.2 IndexedDB `versionchange` Handling (`U-14`) & Storage Persistence
- **`versionchange` Guard (`db/schema.ts`)**: Register `db.on("versionchange", () => { db.close(); onSchemaVersionChange(); })` from v1 so any future schema migration (`1d`/`1e`) in another tab closes stale handles cleanly and prompts reload rather than deadlocking IndexedDB upgrades.
- **`navigator.storage.persist()`**: Requested on startup when `navigator.storage?.persist` exists; records `'granted' | 'denied' | 'unsupported'` plus `navigator.storage?.estimate?.()`.
  - On `localhost` mode when `'denied'`, shows a non-blocking warning banner in `AlertStrip` advising the user to bookmark or install SmartRSS as an app so browser eviction does not drop IndexedDB/outbox data.
  - On `lan_fallback` mode (`!window.isSecureContext` or `mode === 'lan_fallback'`), displays a subtle informational badge: *"Connected LAN view — full reading & actions enabled; use `http://localhost:4180` for offline detached mode."*

### 5.3 Local Cache Eviction & Outbox Protection (`data-model.md` §6.2)
After every completed `sync/pull` cycle (`has_more === false`), run `evictStaleLocalItems(db, now)` inside a single Dexie `rw` transaction across `items_base`, `items`, `outbox`:
1. Build `protectedItemIds: Set<string>` containing every `item_id` referenced across **all** rows in `outbox` (from `item_set_state`, `item_set_starred`, `scope='items'` `item_ids`, and `captured_item_ids` of scoped bulk reads).
2. Compute `cutoffIso = new Date(now - 14 * 86_400_000).toISOString()`.
3. Delete matching rows from **both** `items_base` and `items` where (evaluating against the display row `items` so a locally un-expired or locally starred item is never evicted):
   - `item.starred_num === 0` (`!item.is_starred`), AND
   - `(item.state === 'expired' || (item.state === 'read' && (item.read_at ?? item.read_state_updated_at) < cutoffIso))`, AND
   - `!protectedItemIds.has(item.id)`.
4. Remove deleted item IDs from the in-memory `MiniSearch` index.
5. **Incoming Out-of-Window Guard**: During `pull` delta application, if an `item` `upsert` targets an ID not currently in `items_base`, not in `protectedItemIds`, AND satisfies the eviction predicate (using normalized `toCanonicalIso` timestamps, `U-07`), skip inserting it.

---

## 6. Client Sync Engine, Outbox, Conflict Reconciliation & Restore Recovery

### 6.1 Connection & Sync State Machine

```mermaid
stateDiagram-v2
    [*] --> unpaired : No auth token in client_meta
    [*] --> origin_confirm : Paired & config.api_origin != auth.api_origin (U-08)
    [*] --> bootstrapping : Paired & (sync_cursor == null or bootstrap_meta active)
    [*] --> detached : Paired & cursor exists (initial probe)

    unpaired --> bootstrapping : Pairing succeeds (POST /auth/pair)
    origin_confirm --> detached : User confirms same server (updates auth.api_origin)
    origin_confirm --> unpaired : User clicks Sign Out & Pair New Server

    bootstrapping --> online : Streaming bootstrap + trailing pull succeed
    bootstrapping --> detached : Network error / timeout (checkpoint preserved)
    bootstrapping --> misconfigured : Health CORS TypeError + no-cors resolves (U-17)
    bootstrapping --> auth_revoked : HTTP 401

    detached --> syncing : Health probe OK / online event / manual Sync
    detached --> misconfigured : Health CORS TypeError + no-cors resolves (U-17)
    misconfigured --> syncing : Manual retry / probe succeeds
    online --> syncing : Poll timer (60s) / outbox enqueued / visibility / manual Sync
    syncing --> online : Push + pull + evict succeed
    syncing --> bootstrapping : resync_required (epoch_mismatch / cursor_ahead / cursor_pruned)
    syncing --> detached : Network error / timeout
    syncing --> sync_error : Persistent 5xx or systematic 422 skew (U-04)
    syncing --> auth_revoked : HTTP 401

    sync_error --> syncing : Backoff timer / manual Sync / app reload
    sync_error --> detached : Network offline / health probe fails
    auth_revoked --> bootstrapping : Re-pair (different epoch or no cursor; fresh mutation IDs)
    auth_revoked --> syncing : Re-pair (same epoch; fresh mutation IDs preserve outbox, U-11)
    auth_revoked --> unpaired : User clicks Sign Out & Clear Cache
```

### 6.2 Multi-Tab Coordination, Reachability & Origin-Change Guard (`U-05`, `U-08`, `U-09`, `U-17`)
- **Origin-Change Guard (`U-08`)**:
  - On startup, fetch `GET /client-config.json` (`cache: "no-store"`, network-only). If reachable and `auth` exists in `client_meta`, compare `config.api_origin` against `auth.api_origin` (only when `config.mode === "localhost"`).
  - If they differ, transition immediately to **`origin_confirm`**, pause all sync requests (sending **zero** `Authorization` headers to the new origin), and display `OriginChangeBanner` (*"Server address changed from `{old}` to `{new}` — is this the same SmartRSS server?"*). Confirming updates `auth.api_origin = new` and resumes sync (where `instance_epoch` mismatch or `401` guides re-pairing cleanly). If offline (fetch fails), fall back to `auth.api_origin` cleanly.
- **Cross-Tab Sync Lock (`sync/lock.ts`, `U-05`)**:
  - Secure context (`navigator.locks` present): `navigator.locks.request("smartrss-sync-cycle", { ifAvailable: true }, ...)`.
  - Insecure HTTP fallback (`navigator.locks` absent, e.g. `http://smartrss.lan:8080`): atomic Dexie `rw` lease lock on `client_meta['sync_lock']` (`owner_tab_id = uuidv4()`, `expires_at = now + 15_000`, renewed every `5_000 ms` while held, stale lease reclaimable if `expires_at < now`, released in `finally` and on `pagehide`).
- **No-Lost-Wakeup Protocol (`U-09`)**:
  - The active lock holder tracks `dirty = false` (set `true` whenever `BroadcastChannel('smartrss-sync')` receives `outbox_enqueued` or this tab enqueues).
  - Immediately before releasing the lock after a successful cycle, the holder checks `dirty || (await db.outbox.count()) > 0`; if true (and the previous pass did not stall on error/offline, capped at `MAX_DRAIN_LOOPS = 5`), it resets `dirty = false` and runs one more push+pull pass before releasing.
  - A tab that finds the lock busy broadcasts `outbox_enqueued` on `BroadcastChannel` so the holder's `dirty` flag is guaranteed to see the newly queued work.
- **Reachability & Misconfiguration Probe (`sync/probe.ts`, `U-17`)**:
  - Probes `GET <api_origin>/api/v1/health` (`3,000 ms` timeout, `credentials: "omit"`, `cache: "no-store"`).
  - On CORS/network `TypeError`: issues one follow-up `fetch('<api_origin>/api/v1/health', { mode: 'no-cors', credentials: 'omit', cache: 'no-store' })` (`3,000 ms` timeout).
    - If `no-cors` **resolves** (opaque response), the LAN server IS reachable but rejected the browser's `Origin` or `Host` → transition to **`misconfigured`** with a prominent `"CORS / Host Misconfigured"` pill and the `.env` `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS` fix hint!
    - If `no-cors` **rejects**, the server is genuinely unreachable → transition to **`detached`** with exponential probe backoff (`5s → 10s → 20s → 30s`, reset on `online` event, window focus, or manual Sync).

### 6.3 Two-Phase Streaming Resumable Bootstrap (`U-06`, `1b` §6.2 Conformance)
To bound JS heap memory to a single page (`<= 500` items) and allow resuming interrupted multi-page bootstraps on unreliable Wi-Fi:
1. **Check Existing Checkpoint**: Read `bootstrap_meta['active']`.
   - If `phase === 'committing'`, skip directly to Step 4 (finish local swap without re-fetching).
   - If `phase === 'fetching'`, resume from `next_cursor` using saved `page1_epoch` and `page1_watermark`.
   - Otherwise, fetch Page 1 (`GET /api/v1/sync/bootstrap?limit=500`, no `cursor`), normalize timestamps (`U-07`), check clock drift, and in one short Dexie `rw` transaction write `bootstrap_meta['active'] = { phase: 'fetching', target_gen: (sync.active_gen ?? 0) + 1, page1_epoch, page1_watermark, server_now, next_cursor, categories, feeds, clusters, interest_profile, app_settings, pages_fetched: 1, items_staged: items.length }` and `bootstrap_items.bulkPut(page1.items)`.
2. **Stream Pages 2..N**: While `bootstrap_meta.next_cursor !== null` (i.e. previous page had `has_more === true`):
   - Call `GET /api/v1/sync/bootstrap?cursor=${next_cursor}&limit=500`.
   - If `response.instance_epoch !== page1_epoch`: clear `bootstrap_items` & `bootstrap_meta` and restart from Page 1 (up to `MAX_BOOTSTRAP_RESTARTS = 3`, else transition to `sync_error`).
   - In one short Dexie `rw` transaction: `bootstrap_items.bulkPut(response.items)` and update `bootstrap_meta` (`next_cursor = response.has_more ? response.next_cursor : null`, increment `pages_fetched` & `items_staged`). *(Pages 2..N have `watermark: null`; `page1_watermark` is never overwritten.)*
   - *(Notice: existing `items_base`/`items` remain 100% readable if the network drops mid-bootstrap!)*
3. **Enter Commit Phase**: Update `bootstrap_meta.phase = 'committing'`.
4. **Chunked Swap & Atomic Activation**:
   - Write `categories`, `feeds_base` (and recompute `feeds = feeds_base ⊕ outbox`), `clusters`, `app_settings`, `interest_profile` in one short Dexie `rw` transaction.
   - Stream `bootstrap_items` in chunks of `1,000` keys: for each chunk, inside a Dexie `rw` transaction, load `outbox`, write rows to `items_base` tagged `bootstrap_gen = target_gen`, compute and write `items = items_base ⊕ outbox` tagged `bootstrap_gen = target_gen`, and delete those chunk keys from `bootstrap_items`.
   - In one final short Dexie `rw` transaction: delete stale rows in `items_base` and `items` where `bootstrap_gen < target_gen`, write `client_meta['sync'] = { instance_epoch: page1_epoch, sync_cursor: page1_watermark, active_gen: target_gen, last_sync_at, last_server_now, clock_drift_ms }`, and delete `bootstrap_meta['active']`.
5. Rebuild the in-memory `MiniSearch` index from Dexie `items`.
6. Immediately run **push** (if `outbox` is non-empty, e.g. after restore recovery) followed by **trailing `pull`** (`GET /api/v1/sync/pull?since_seq=${page1_watermark}&epoch=${page1_epoch}`) looping until `has_more === false` so any writes committed during bootstrap pagination converge immediately.

### 6.4 Outbox Enqueue, Server-Base Recompute, Skew-Safe Push & Replay Journal (`U-01`, `U-02`, `U-04`, `U-10`, `U-11`, `U-12`)

#### Pure Deterministic Reconciliation Function (`sync/reconciliation.ts`, `U-01`)
All display rows are derived by one pure, `fast-check`-tested function:
- `applyOutboxToItem(base: SyncItemDTO, outboxRows: OutboxRow[]): LocalItemRecord`:
  - Starts from a shallow clone of `base`.
  - Iterates `outboxRows` in `local_seq ASC` order:
    - `item_set_state` matching `base.id`: applies `state`, `read_at`, `unexpired_at` (if transitioning from `expired` or `expired_at != null` to `unread`), `read_state_updated_at = payload.client_timestamp`.
    - `item_set_starred` matching `base.id`: applies `is_starred`, `starred_at`, `starred_updated_at = payload.client_timestamp`, updates `starred_num = is_starred ? 1 : 0`.
    - `item_mark_bulk_read`: applies ONLY if `base.id` is in `row.captured_item_ids` (for scoped modes — **never re-running the scope predicate against newly pulled items**, `U-15`) or `payload.item_ids` (for `scope === 'items'`), and ONLY when current overlaid state is `'unread'` (for scoped) or `'unread' | 'read'` (for `scope === 'items'`; `'expired'` excluded per `RR-2`).
- `applyOutboxToFeed(base: SyncFeedDTO, outboxRows: OutboxRow[]): SyncFeedDTO`:
  - Applies matching `feed_set_field` rows in `local_seq ASC` order (updating `status`, `category_id`, `expiration_days_override`, or nested `research_policy.enabled` / `research_policy.fetch_pdfs` + `field_updated_at[field]`).

#### Enqueuing an Offline/Optimistic Mutation (`sync/outbox.ts`)
Executes inside **one atomic Dexie `rw` transaction**:
1. Generate `client_mutation_id = uuidv4()` (`U-05`, lowercase) and `client_timestamp = new Date().toISOString()` (canonical 24-char UTC ISO-8601, `K-05`).
2. Target handling:
   - **Normal working-set item/feed** (`items_base.get(id)` / `feeds_base.get(id)` exists): append `outbox` row (`target_absent: false`), then recompute and write `items[id] = applyOutboxToItem(base, outboxForItem)` (or `feeds[id]`).
   - **Archive-only search result (`target_absent: true`, `U-12`)**: `items_base.get(id)` is `undefined`. Append `outbox` row with `target_absent: true` and leave Dexie `items_base` / `items` **untouched** (`P-17`). The Search view derives each displayed archive result reactively as `applyOutboxToItem(searchItem, outboxForItem)`!
   - **Scoped `item_mark_bulk_read` (`scope: 'all' | 'feed' | 'category'`)**:
     - Guard: requires `sync.instance_epoch !== null && sync.sync_cursor !== null`.
     - Stamp `up_to_sync_seq = sync.sync_cursor` and `epoch = sync.instance_epoch`.
     - Query current display `items` matching scope + `state === 'unread'` + `ingest_sync_seq <= up_to_sync_seq` + optional `older_than_ts` (`effective_date < older_than_ts`), collecting `captured_item_ids`.
     - Append `outbox` row (with `captured_item_ids` stored locally, never sent on the wire for scoped modes) and recompute display `items` for every ID in `captured_item_ids`.
   - **Explicit `scope: 'items'`**: split `item_ids` into chunks of `<= 1,000` (each with its own `client_mutation_id`), append `outbox` rows, and recompute display `items` for affected IDs.
3. Update `MiniSearch` incrementally for modified working-set items, broadcast `outbox_enqueued` via `BroadcastChannel`, and schedule a debounced (`250 ms`) sync if `online`.

#### Packing, Skew-Safe Flushing & Outcome Handling (`POST /api/v1/sync/push`)
1. Read `outbox` rows ordered by `local_seq ASC` (no `in_flight` status filter, `U-02`).
2. Pack a FIFO batch respecting all three `1b` server caps (`S6`, `P-09`):
   - `batch.length <= 500` mutations,
   - Sum of `item_ids.length` across `scope === 'items'` mutations `<= 5,000`,
   - Exact UTF-8 byte length via `TextEncoder().encode(JSON.stringify({ mutations: wirePayloads })).byteLength <= 1_572_864` bytes (`1.5 MiB`, below server `2 MiB` `413` cap).
3. Send `POST <api_origin>/api/v1/sync/push` (`15,000 ms` timeout).
4. **Transport / HTTP Error Handling**:
   - Network error / timeout → increment `attempts` and set `last_error` on batch rows, transition to `detached` (or `misconfigured` via probe), keep `outbox` intact.
   - `401 Unauthorized` → transition to `auth_revoked`, keep `outbox` intact (never drop mutations).
   - `503 Service Unavailable` (respecting `Retry-After`) or `5xx` → increment `attempts`, keep `outbox` intact, backoff retry (`sync_error`).
   - **`422` / `400` / `413` Skew-Safe Poison-Pill Bisection (`U-04`)**:
     - Track `quarantinedThisCycle = 0` (`MAX_QUARANTINES_PER_CYCLE = 3`).
     - Recursive `flushWithBisection(subBatch, siblingSucceeded)`:
       - If `subBatch.length === 1`:
         - Quarantine into `failed_mutations` **ONLY** if `siblingSucceeded === true` (proven isolated bad mutation) OR HTTP status was `413` (single oversized payload), AND `quarantinedThisCycle < MAX_QUARANTINES_PER_CYCLE`. On quarantine: delete row from `outbox`, insert into `failed_mutations`, recompute display row from `items_base`/`feeds_base` (`U-01`), insert `sync_notifications` error + toast, increment `quarantinedThisCycle`.
         - Otherwise (standalone `422`/`400` with no succeeding sibling, or cap exceeded): **halt immediately**, transition to **`sync_error`** (*"Server rejected sync payload — update or reload the app"*), keep all remaining `outbox` rows intact (zero rollbacks!).
       - If `subBatch.length > 1`: split into `[left, right]` and attempt `left` then `right`:
         - If **both** halves fail with `422`/`400` → **systematic rejection (version skew)**: halt immediately in `sync_error`, keeping the entire `outbox` intact!
         - If one half succeeds (`200`) and the other fails → process the succeeding half normally, then recurse on the failing half with `siblingSucceeded = true`!
5. **Processing HTTP `200` `SyncPushResponse`** (inside one atomic Dexie `rw` transaction per batch):
   - Normalize timestamps (`U-07`), check clock drift (`server_now`).
   - **Normative Cursor Invariant (`1b` `P-03`)**: NEVER modify `client_meta['sync'].sync_cursor` from push!
   - Collect all affected `item_id`s and `feed_id`s across results in this batch.
   - For each `{client_mutation_id, outcome, reason, affected_count, current_entity_type, current_entity}` matched to `outbox` row `M`:
     - **`applied`**:
       - Delete `M` from `outbox`.
       - **48h Replay Journal (`U-10`/`Q-3`)**:
         - If `M.payload.type` is `item_set_state`, `item_set_starred`, `feed_set_field`, or `item_mark_bulk_read(scope='items')` → upsert into `replay_journal` with `acknowledged_at = response.server_now`.
         - If `M.payload.type === 'item_mark_bulk_read'` and `M.payload.scope !== 'items'` → if `M.captured_item_ids` is non-empty and `<= 5,000` IDs, split into chunks of `<= 1,000` IDs, generate one stable `uuidv4()` per chunk, and store those `scope: 'items'` mutations (preserving `M.payload.client_timestamp`) in `replay_journal` with `acknowledged_at = response.server_now`!
     - **`superseded`**:
       - Delete `M` from `outbox` (not journaled).
       - If `current_entity` is present and the base row exists in `items_base`/`feeds_base`, merge the slim snapshot fields (`SyncPushItemSnapshot` or `SyncPushFeedSnapshot`) into `items_base`/`feeds_base` (preserving full fields like `raw_summary`, `title`, `lead_excerpt`). (If `M.target_absent`, notify Search view state with `current_entity`.)
     - **`rejected`**:
       - Delete `M` from `outbox` (not journaled).
       - If `current_entity` is present and base row exists → merge slim snapshot into `items_base`/`feeds_base`.
       - Insert a human-readable notification into `sync_notifications` (mapping `category_not_found`, `clock_skew`, `epoch_mismatch`, `entity_not_found`, `invalid_field_value`, etc.) and trigger an immediate UI toast.
   - **Unified Recompute (`U-01`)**: For every affected `item_id` and `feed_id` that exists in `items_base`/`feeds_base`, recompute `items[id] = applyOutboxToItem(items_base[id], remainingOutbox)` and `feeds[id] = applyOutboxToFeed(feeds_base[id], remainingOutbox)`! *(Rollback, supersession, and cross-field preservation all happen automatically with zero stale-snapshot bugs!)*
6. Loop until `outbox` is empty, then immediately run `sync/pull` so authoritative server state converges and `sync_cursor` advances.

### 6.5 Pull Delta Application (`U-01`, `U-07`, `1b` §6.3 Conformance)
Loop `GET /api/v1/sync/pull?since_seq=${sync_cursor}&epoch=${instance_epoch}&limit=200` until `has_more === false`:
1. Normalize timestamps (`U-07`) and check clock drift (`server_now`).
2. If `response.resync_required === true` → invoke §6.6 Restore/Resync Recovery immediately and break loop.
3. Inside **one atomic Dexie `rw` transaction**:
   - Load **all** rows in `outbox` (`local_seq ASC`, `U-02`) and build `protectedItemIds`.
   - For each `change` in `response.changes`:
     - `category`: `delete` → `categories.delete(id)`; `upsert` → `categories.put(data)`.
     - `feed`: `delete` → delete from `feeds_base` and `feeds`; `upsert` → write `feeds_base.put(data)` and recompute `feeds.put(applyOutboxToFeed(data, outboxForFeed))`.
     - `cluster`: `delete` → `clusters.delete(id)`; `upsert` → `clusters.put(data)`.
     - `item`:
       - `delete` → delete from `items_base` and `items`.
       - `upsert` → apply incoming out-of-window guard (§5.3 step 5); if kept, write `items_base.put({...data, bootstrap_gen: active_gen})` and recompute `items.put(applyOutboxToItem(data, outboxForItem, active_gen))` (`U-01`).
     - `app_settings` / `interest_profile`: update `client_meta`.
   - Advance `client_meta['sync'].sync_cursor = response.next_seq` and update `last_sync_at` / `last_server_now` / `clock_drift_ms`.
4. Update `MiniSearch` incrementally for all modified/deleted items in this page.
5. After `has_more === false`, prune `replay_journal` (`acknowledged_at < now - 48h`) and run `evictStaleLocalItems()` (§5.3).

### 6.6 Restore Recovery (`epoch_mismatch`, `cursor_ahead`, `cursor_pruned` — `U-10`, `U-21`, `Q-3`)
When `GET /api/v1/sync/pull` returns `resync_required: true`:
1. **Rewrite Pending Scoped Bulk Reads** (when `resync_reason === 'epoch_mismatch' || resync_reason === 'cursor_ahead'`):
   - Inspect `outbox` for pending `item_mark_bulk_read` with `scope !== 'items'` (whose `up_to_sync_seq` belongs to the pre-restore sequence space, `1b` `P-11-F1`).
   - If `captured_item_ids` is non-empty and `<= 5,000` IDs: replace the entry with chunked `scope: 'items'` outbox mutations (`<= 1,000` IDs per chunk, fresh `uuidv4()`, preserving original `client_timestamp`).
   - Else (empty or `> 5,000` items): remove the entry and record an informational `sync_notifications` banner (*"Server database was restored; a large offline bulk mark-read (> 5,000 items) could not be replayed safely and was cleared"*).
   - *(On `resync_reason === 'cursor_pruned'`, `instance_epoch` is unchanged so scoped bulk-read `up_to_sync_seq` bounds remain valid.)*
2. **48-Hour Replay Journal Promotion (`U-10`/`Q-3`, `U-21`)** (on **both** `resync_reason === 'epoch_mismatch'` AND `resync_reason === 'cursor_ahead'`):
   - Load all `replay_journal` entries with `acknowledged_at >= now - 48h`.
   - Append each entry whose `client_mutation_id` is not already in `outbox` into `outbox`, preserving its exact original `client_mutation_id` and `client_timestamp` (arrival order relative to newer pending outbox rows does not matter because every journaled single-field and `scope='items'` mutation commutes under `1b`'s LWW tie-breaking, `U-21`).
3. Clear any stale `bootstrap_meta` / `bootstrap_items`, execute streaming **Bootstrap** (§6.3) → **Push** (§6.4) → **Pull** (§6.5).
4. Record an informational notice in `sync_notifications` (*"Synced after server restore — recent local actions replayed cleanly"*) for `epoch_mismatch` / `cursor_ahead`.

### 6.7 Re-Pair Fresh Mutation IDs (`U-11`) & Clock-Drift Warning (`1b` §6.5 `Q-3`)
- **Re-Pair Fresh IDs (`U-11`)**: Whenever the user re-pairs after `auth_revoked` (receiving a new `device_id`), inside one Dexie `rw` transaction before resuming sync, assign a fresh `uuidv4()` `client_mutation_id` to every row in `outbox` and `replay_journal` (preserving `client_timestamp` and payload). Because the server scopes `sync_mutations` idempotency per `device_id`, minting fresh IDs prevents previously committed (timed-out or journaled) mutations from returning noisy `rejected / mutation_id_conflict` errors — LWW resolves them cleanly and silently.
- **Clock-Drift Warning**: On every `bootstrap`, `pull`, and `push` response, compute `driftMs = Date.now() - Date.parse(response.server_now)`. Whenever `Math.abs(driftMs) > 5 * 60 * 1000`, `AlertStrip` displays a persistent, non-blocking warning banner.

---

## 7. Service Worker & Offline App Shell (`frontend/src/sw/`)

### 7.1 Coherent Cache-First Precache & Fetch Routing (`U-14`, `U-08`)
A lightweight Vite build plugin (`emitSwManifestPlugin` in `vite.config.ts`) injects the exact emitted static asset manifest (`["/", "/index.html", "/manifest.webmanifest", "/icons/...", "/assets/index-<hash>.js", "/assets/index-<hash>.css", ...]`) and `BUILD_HASH` into `dist/sw.js`. When served by `smartrss_client_runner.py`, the appended `// smartrss-origin-tag:<tag>` comment ensures any `SMARTRSS_API_ORIGIN` change triggers a SW reinstall (`U-08`).

| Request Match | Strategy | Rationale |
| :--- | :--- | :--- |
| Cross-origin requests (all LAN API traffic to `SMARTRSS_API_ORIGIN`) | **Bypass (Network-only)** | Never cache authenticated API responses or interfere with offline detection / `sync/push`. |
| Same-origin `/api/*` (Nginx `web` fallback) | **Bypass (Network-only)** | Never cache API responses in Cache Storage. |
| Same-origin `/client-config.json` | **Bypass (Network-only)** | Always reflects live local-runner `SMARTRSS_API_ORIGIN`; SPA falls back to Dexie `auth.api_origin` when offline. |
| Same-origin `/assets/*`, `/manifest.webmanifest`, `/icons/*` | **Cache-first** (from active SW's precache, network fallback) | Instant offline cold launch; immutable hashed assets. |
| Same-origin navigations (`request.mode === 'navigate'`) | **Cache-first** (from active SW's precached `/index.html`, network fallback) (`U-14`) | Guarantees coherent build matching active SW across all tabs, instant offline reload, and genuine user-gated updates. |

### 7.2 Update Lifecycle & Insecure Origin Guard
- **`install`**: Opens `caches.open(CACHE_NAME)` (keyed by `BUILD_HASH` + `origin_tag` if present) and fetches each manifest URL with `{ cache: 'reload' }` (bypassing browser HTTP cache so updated `/index.html` CSP headers are guaranteed fresh, `U-08`). Calls `self.skipWaiting()` automatically ONLY on first install (`!self.registration.active`); on updates, waits for the user to click **"Update available — Reload"** (which sends `{type: 'SKIP_WAITING'}` via `postMessage` and reloads tabs on `controllerchange` cleanly, never interrupting an active sync transaction).
- **`activate`**: Deletes older `smartrss-shell-*` caches and calls `self.clients.claim()`.
- **Registration Guard (`sw/register.ts`)**: Registers `/sw.js` only when `'serviceWorker' in navigator && window.isSecureContext`. Skips silently on plain-HTTP LAN fallback (`http://smartrss.lan:8080`).

---

## 8. Search Subsystem (Connected Archive FTS & Detached Local MiniSearch)

### 8.1 Dual-Mode Search UX, `P-17` Display-Only Rule & `U-12` Target-Absent Overlay
- **Online Default**: Queries `GET /api/v1/search` (full PostgreSQL historical archive, including `expired` and `> 14d read` items), showing a `"Full Archive"` pill, with an optional `"Local Cache Only"` toggle.
- **Detached Automatic Switch**: Whenever `connectionState !== 'online'`, automatically searches the local `MiniSearch` index over Dexie's working set, displaying a prominent **`"Detached — Local Cache Only"`** badge.
- **`P-17` & `U-12` Archive Mutation Semantics**:
  - Connected search results (`SearchItemDTO`) live strictly in React component state and are **NEVER bulk-inserted into Dexie**.
  - Clicking an archive-only result displays `SearchItemDTO` metadata + `lead_excerpt` and offers an online-only **"Load full summary & extracted text"** button calling `GET /api/v1/items/{id}` (rendering `raw_summary` via `SafeHtml` and `extracted_text` as plain text, `U-13`).
  - Clicking **Star** or **Restore to Unread / Mark Read** on an archive-only result enqueues a `target_absent: true` mutation in `outbox` (§6.4); the Search view computes displayed state reactively as `applyOutboxToItem(searchResult, outboxForItem)`. On `applied`, trailing `pull` brings the item into `items_base`/`items`; on `rejected`/`superseded`, removing the outbox row cleanly reverts (or patches with `current_entity`) the Search view row and surfaces any rejection toast!

### 8.2 Detached Local Search Engine (`search/queryParser.ts` & `search/localSearch.ts`)
- **Query Parser (`queryParser.ts`)**: Pure parser matching `1b`'s `websearch_to_tsquery` syntax: quoted `"exact phrase"`, `-excluded` / `-"excluded phrase"`, positive terms, blank query → facet-only `newest` sort, and negation-only query → validation error `"Search query must include at least one positive required search term"`.
- **MiniSearch Index (`localSearch.ts`)**:
  - Hydrated once on startup from Dexie `items` and updated incrementally via `add` / `replace` / `discard` on every `bootstrap`, `pull`, or `evict`.
  - Indexed fields & boost weights (`A > B > C > D` hierarchy): `title (4.0)`, `tags_text (3.0)`, `tldr (3.0)`, `author (2.0)`, `takeaways_text (1.5)`, `lead_excerpt (1.0)`.
  - **Stemming note (`U-20`)**: Phase 1 `MiniSearch` uses prefix + exact token matching without English stemming (unlike PostgreSQL `english` FTS), so detached search may differ slightly on inflected forms (e.g., `"running"` vs `"run"`); a lightweight Porter stemmer can be added later if needed. Hydration and query latency over 5,000 synthetic items are benchmarked in `1c-T07` rather than assumed.
  - Supports exact phrase & `-negation` post-filtering, active facets (`feed_id`, `category_id`, `state`, `is_starred`, `date_from`, `date_to`), and `relevance` / `newest` sorting.
- **Structured XSS-Safe Snippet Generator**: Emits the exact `SnippetFragment[][]` contract (`{ text: string; highlight: boolean }[][]`) used by `GET /api/v1/search`, rendered via `<SnippetText>` as pure React text nodes + `<mark>`.

---

## 9. Untrusted Content Sanitization & Client Security Boundaries

### 9.1 Strict URL Validation & Relative Resolution (`frontend/src/security/urlSafety.ts`, `S4`, `U-13`)
- `isSafeHttpUrl(raw: string): boolean`: Rejects empty strings, ASCII control chars (`\x00`–`\x1f`, `\x7f`), protocol-relative URLs (`//evil.example`), and any scheme other than `http:` or `https:` with non-empty hostname.
- `resolveSafeHttpUrl(raw: string, baseUrl?: string | null): string | null`: Trims `raw`; if `raw` is already an absolute URL, validates directly via `isSafeHttpUrl`. If relative (e.g., `/2026/11/post`, `./img.png`) and `baseUrl` passes `isSafeHttpUrl`, resolves via `new URL(raw, baseUrl).href` and validates the resolved URL with `isSafeHttpUrl`. Otherwise returns `null` (fail closed).
- Applied to **every** rendered link (`raw_url`, `canonical_url`, `site_url`, `feed_url`, `research_sources[].source_url`, and every `<a href>` / image placeholder inside `raw_summary`), always with `target="_blank" rel="noopener noreferrer"`.

### 9.2 Single-Pass DOMPurify Sanitization (`frontend/src/security/sanitizeHtml.ts`, `NFR-SEC-03`, `1c-D02`, `U-13`)
`sanitizeFeedHtmlToFragment(rawHtml: string, baseUrl?: string | null): DocumentFragment` executes a **single HTML parse** (zero double-parse mXSS surface, zero `dangerouslySetInnerHTML`):
1. **Input Size Guard (`S5`/`S6`)**: Slices input to `65_536` characters.
2. **Single-Pass DOMPurify (`RETURN_DOM_FRAGMENT: true`)**:
   - **`uponSanitizeElement` hook**: When `node.tagName === 'IMG'` (or `<picture>`):
     - Reads `alt` (trimmed, capped at 120 chars) and `src`, resolving `src` via `resolveSafeHttpUrl(src, baseUrl)`.
     - Replaces `node` in-place inside DOMPurify's inert document with a `<span class="srss-blocked-img">` containing safe text `[Image blocked: <alt || hostname>] ` + an `<a href="<safe_src>" target="_blank" rel="noopener noreferrer">Open image</a>` (if `safe_src` is non-null), or plain text `[Image blocked: <alt>]` (if only `alt` is non-empty), or removes `node`.
   - **Strict Allow-List**:
     - `ALLOWED_TAGS`: `["p", "br", "a", "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "b", "i", "u", "s", "del", "sub", "sup", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "thead", "tbody", "tr", "th", "td", "dl", "dt", "dd", "figure", "figcaption", "span", "div"]`.
     - `ALLOWED_ATTR`: `["href", "title", "colspan", "rowspan", "target", "rel", "class"]`.
     - `ALLOW_DATA_ATTR: false`, `ALLOW_ARIA_ATTR: false`, `FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "input", "textarea", "select", "button", "svg", "math", "img", "picture", "video", "audio", "source", "link", "meta", "base", "template", "noscript"]`, `FORBID_ATTR: ["style", "id", "name", "src", "srcset", "onerror", "onload", "formaction"]`.
   - **`afterSanitizeAttributes` hook**:
     - Strips any `class` attribute not strictly equal to `"srss-blocked-img"`.
     - For every `<a>`, resolves `href` via `resolveSafeHttpUrl(href, baseUrl)`; if `null`, removes `href`, else sets `href = resolved`, `target = "_blank"`, `rel = "noopener noreferrer"`.
3. `SafeHtml.tsx` mounts the returned `DocumentFragment` directly via `containerRef.current.replaceChildren(fragment)` — zero re-serialization, zero `innerHTML`!
4. **Plain-Text-Only Fields (`U-13`)**: `item.title`, `item.author`, `item.lead_excerpt`, **`item.extracted_text`** (from on-demand `GET /api/v1/items/{id}`), `enrichment.tldr`, `enrichment.takeaways[]`, `enrichment.relevance_rationale`, `enrichment.research_context`, `tags[]`, `feed.title`, `feed.last_error`, and `SnippetFragment.text` are ALWAYS rendered exclusively as React text nodes (never HTML).

### 9.3 Token Hygiene & Sign Out (`S3`, `U-21`)
- `bearer_token` is stored only in Dexie `client_meta['auth']`, sent solely in `Authorization` headers, never logged, never in query strings or cookies.
- **"Sign Out & Clear Local Cache" (`U-21`)**: When online, first issues a best-effort `POST /api/v1/devices/{auth.device_id}/revoke` (`3,000 ms` timeout) so the discarded token stops being valid server-side immediately, then atomically clears all Dexie tables (`client_meta`, `items_base`, `items`, `feeds_base`, `feeds`, `categories`, `clusters`, `bootstrap_meta`, `bootstrap_items`, `outbox`, `replay_journal`, `failed_mutations`, `sync_notifications`) and resets in-memory `MiniSearch`.

---

## 10. User Interface, Navigation, Views & Keyboard Shortcuts

### 10.1 Device Pairing & Onboarding (`components/auth/PairingScreen.tsx`)
- Displayed when `client_meta['auth']` is absent.
- Shows target Server Origin (read-only pinned origin from `/client-config.json` in `localhost` mode with 1-line tip on updating `SMARTRSS_API_ORIGIN`, or `"Same-origin LAN server"` in `lan_fallback` mode) + live connectivity badge (`GET /api/v1/health`, surfacing `misconfigured` hint if `no-cors` resolves, `U-17`).
- Inputs: **Pairing Code**, **Device Name** (default `"Ubuntu Laptop"` in `localhost` mode, `"LAN Browser"` in `lan_fallback` mode), **Platform** (`"ubuntu"` or `"web"`).
- Submits `POST /api/v1/auth/pair`, persists `{device_id, device_name, platform, bearer_token, api_origin}` in `client_meta['auth']`, and triggers streaming `bootstrap` with live page/item progress.
- Clear error messages for `401`, `429` (`Retry-After`), and CORS/`ALLOWED_HOSTS` misconfiguration.

### 10.2 Classic 3-Pane Reader (`FR-UI-01`, `FR-UI-02`)
- **Top Header Bar**: SmartRSS brand + view switcher (**Reader**, **Feeds & OPML**, **Health & Metrics**, **Settings & Devices**), Search bar (`/` shortcut) with mode badge (`Full Archive` vs `Detached — Local Cache Only`), Connection & Sync pill (`Online`, `Syncing…`, `Detached`, `Misconfigured`, `Sync Error`, `Auth Revoked`), pending outbox badge (clickable → Outbox Inspector), **Sync (`r`)**, **Shortcuts (`?`)**, Theme toggle (`Light`/`Dark`/`System`).
- **Alert Strip** (conditional): `OriginChangeBanner` (`U-08`), Clock-drift warning (`> 5 min`), Storage persistence denied warning, `AuthRevokedBanner`, Service Worker `"Update available — Reload"` bar, Dexie `versionchange` reload prompt (`U-14`), and rejection rollback toast stack.
- **Pane 1 — Navigation Sidebar** (responsive drawer on `< 900px`):
  - Smart Filters with live counts: **Unread** (excludes `unsubscribed` feeds), **Starred** (queries `starred_num === 1` index, includes starred items from `unsubscribed` feeds or `expired` state), **All Active**, **Expired / Archive** (connected FTS query `state=expired`).
  - **Categories** (collapsible folders sorted `sort_order ASC, name ASC`) with unread counts and nested feeds; **Uncategorized** active/paused/muted feeds (health dot + `Paused`/`Muted` pill); collapsible **Unsubscribed** section; **"+ Add Feed"** button (online-only).
- **Pane 2 — Middle Item List / Sortable Table**:
  - **Filter & Action Toolbar**: Active scope title + count, quick state pills (`Unread`/`Starred`/`All`/`Expired`), optional `Tier`/`Depth`/`Tag` filters (shown when enriched items exist), and bounded **"Mark Read" Menu** (`Shift+A`: *"Mark all in scope read"*, *"Older than 24 hours"*, *"Older than 7 days"* with confirmation noting `up_to_sync_seq` protection).
  - **Sortable Header Bar (`FR-UI-02`)**: Clickable headers toggling asc/desc: **Relevance** *(default: `relevance_tier DESC`, unenriched after tiered, tie-broken `effective_date DESC, id DESC`)*, **Date** (`effective_date`), **Feed** (`feed.title`), **Depth** (`enrichment?.depth_type`).
  - **Item Row**: Unread indicator, Star button (`s`), Relevance tier badge (when present), Title, Feed title (graceful `"Unknown feed"` fallback if dangling mid-pull), relative timestamp, 2-line `lead_excerpt` (or `<SnippetText>` in search mode), depth/tag pills. Uses incremental/virtualized windowing for smooth scrolling across large working sets.
- **Pane 3 — Reading / Detail Pane**:
  - Header: Safe external Title link, Feed name, Author, Published & Fetched timestamps, State/Starred badges.
  - Action Bar: **Open in Browser (`o`)**, **Mark Read/Unread (`m`)**, **Star/Unstar (`s`)**, and **Feed Quick Actions** (Mute/Pause/Unsubscribe/Reactivate, Move Category, Expiration Override, Toggle `research_policy.enabled` / `fetch_pdfs` — **all offline-capable**).
  - **Smart Metadata Card**: Forward-compatible `1d`–`1f` display slot (tier + rationale, TL;DR, takeaways, depth, tags, validated `research_sources[]`); minimal subtle *"AI enrichment pending"* line when `null`.
  - **Content Body**: `SafeHtml` (`sanitizeFeedHtmlToFragment`) with `summary_truncated` notice, plus toggle for plain-text `lead_excerpt` and online-only **"Load full extracted text"** (`GET /api/v1/items/{id}`, rendered strictly as plain text, `U-13`).

### 10.3 Offline vs. Online-Only Feature Gating & `EditFeedModal` Diff-Only `PATCH` (`FR-SYNC-03`, `U-21`)

| Operation | Detached (Offline) | Connected (Online — Localhost & LAN Fallback) | Wire Mechanism |
| :--- | :--- | :--- | :--- |
| Mark item read/unread (`m`), restore `expired` → `unread` | **Enabled (Outbox)** | **Enabled (Outbox → immediate push)** | `sync/push` `item_set_state` |
| Star / unstar item (`s`) | **Enabled (Outbox)** | **Enabled (Outbox → immediate push)** | `sync/push` `item_set_starred` |
| Bounded bulk mark-read (all / feed / category / older-than-X / items) | **Enabled (Outbox)** | **Enabled (Outbox → immediate push)** | `sync/push` `item_mark_bulk_read` |
| Feed `status`, `category_id`, `expiration_days_override`, `research_policy.enabled`, `research_policy.fetch_pdfs` | **Enabled (Outbox)** | **Enabled (Outbox → immediate push)** | `sync/push` `feed_set_field` |
| Search | **Detached Local MiniSearch** | **Connected PostgreSQL FTS** (+ optional local toggle) | Local / `GET /api/v1/search` |
| Open external article/PDF URL (`o`) | **Enabled** | **Enabled** | `<a target="_blank" rel="noopener noreferrer">` |
| Add feed, OPML import/export, Edit feed URL/title/poll interval/full-text/`curation_instructions` | **Disabled** (*"Requires home LAN"*) | **Enabled** | `POST/PATCH /api/v1/feeds*` |
| Create / rename / reorder / delete Category | **Disabled** (*"Requires home LAN"*) | **Enabled** | `POST/PATCH/DELETE /api/v1/categories*` |
| Global `app_settings`, Device pairing / revocation, Manual poll, Dead-letter retry/dismiss, System Health/Metrics | **Disabled** (*"Requires home LAN"*) | **Enabled** | Online REST endpoints |

- **`EditFeedModal` Diff-Only `PATCH` (`U-21`, Checklist `D3`)**: `EditFeedModal` MUST send **only the fields actually modified by the user** in `PATCH /api/v1/feeds/{id}` (never echoing unchanged `status`, `category_id`, `expiration_days_override`, or `research_policy` fields, which would receive fresh monotonic online timestamps and silently supersede pending offline `feed_set_field` mutations).
- **Optimistic Concurrency for `curation_instructions` (`409 Conflict`)**: Sends `expected_config_version: feed.config_version` whenever `curation_instructions` changed; on `409`, preserves user draft, fetches latest `GET /api/v1/feeds/{id}`, and shows `ConflictDiffDialog` (*"Overwrite with my draft"* vs *"Reload server version"*).

### 10.4 System Health, Feed Health & Pipeline Metrics UI (`NFR-OPS-01`, `FR-FEED-03`)
When online, fetches `GET /api/v1/system/health`, `GET /api/v1/system/metrics?window=24h|7d|30d`, `GET /api/v1/feeds/health`, and `GET /api/v1/system/jobs/dead-letter` (when detached, displays local client storage/outbox diagnostics plus an online-only notice):
1. **Overview Cards**: DB health, `instance_epoch`, Server `current_seq` vs Local `sync_cursor`, Dead-letter count, Storage persistence (`granted`/`denied`/`unsupported`) & quota estimate, Local Dexie counts (items, pending outbox, 48h replay journal, quarantined), and Job Queue matrix (`job_type` × `queued`/`running`/`done`/`failed`).
2. **Pipeline Performance Metrics** (`window`: `24h` | `7d` | `30d`):
   - **Per-Stage Latency & Failure Table** (`job_type`, `total_count`, `ok`, `failed`, `skipped`, `failure_rate%`, `avg`, `p50`, `p95`, `max`, `avg_queue_wait_ms`, sortable, `<progress>` bars).
   - **Per-Feed Processing Cost Rollup Table** (`feed_title`, `total_jobs`, `failed_jobs`, `total_duration_ms`, `avg_duration_ms`, sortable).
   - **Recent Pipeline / Cron Runs (`recent_runs`)**: `trigger`, `stages`, `status`, `started_at`, `duration_ms`, `items_polled`, `items_extracted`, `items_enriched`, `jobs_failed`, `notes`.
3. **Feed Health Table** (`GET /api/v1/feeds/health`): Severity-sorted (`gone` → `broken` → `degraded` → `healthy`), separated `status` and `health_state` badges, `last_success_at`, `last_fetched_at`, `consecutive_errors`, `last_error`, row actions (**Poll Now**, **Edit URL**, **Pause/Mute/Unsubscribe**).
4. **Dead-Letter Queue**: Paginated failed jobs (`limit=25`) with **Retry** (`409` active-job collision handled cleanly) and **Dismiss** (with confirmation).

### 10.5 Settings, Devices & Outbox Inspector
- **Global App Settings (`GET`/`PATCH /api/v1/settings`)**: `default_expiration_days`, `backfill_cap`, `default_poll_interval_minutes`.
- **Paired Devices (`GET /api/v1/devices`)**: Lists devices, **"Generate Pairing Code"** (`POST /api/v1/auth/pairing-codes` + 5-min countdown), **"Revoke"** (`POST /api/v1/devices/{id}/revoke` with self-revoke warning).
- **OPML Import / Export**: File upload or paste XML + optional `default_category_id` → `POST /api/v1/feeds/import-opml` (shows `{imported, reactivated, skipped_duplicates, rejected_invalid}` + triggers `pull`). Export downloads `smartrss-feeds.opml`.
- **Outbox Inspector & Diagnostics**: Lists pending `outbox` rows (with `attempts` and `last_error`, `U-02`), quarantined `failed_mutations` with both **Retry** (re-enqueues with same `client_mutation_id`, `U-04`) and **Dismiss**, recent `sync_notifications`, **"Sync Now"**, **"Force Full Resync"**, and **"Sign Out & Clear Local Cache"** (revoking own device online first, `U-21`).

### 10.6 Keyboard Shortcuts (`FR-UI-01`)
Suppressed while focus is inside `input`, `textarea`, `select`, or `[contenteditable="true"]` (except `Escape`):

| Key | Action |
| :--- | :--- |
| `j` / `ArrowDown` | Select next item |
| `k` / `ArrowUp` | Select previous item |
| `m` | Toggle selected item `read` / `unread` |
| `s` | Toggle selected item `is_starred` |
| `o` / `Enter` | Open selected item's validated external URL in new tab (`noopener,noreferrer`) and mark `read` (if `unread`) |
| `/` | Focus global Search bar |
| `r` | Trigger manual sync (when online) |
| `Shift+A` | Open bounded "Mark current view read" confirmation dialog |
| `?` | Toggle Keyboard Shortcuts help modal |
| `Escape` | Close modal/menu or blur Search input |

---

## 11. Security, Edge Cases & Checklist Verification

Cross-checked against [docs/review-checklist.md](../review-checklist.md):

| Checklist ID | Concern | Design Mitigation in `1c` |
| :--- | :--- | :--- |
| **S1 / S5** | Untrusted HTML (incl. mXSS) & runner path handling | Single-pass DOMPurify `RETURN_DOM_FRAGMENT` (`U-13`, zero double-parse, zero `dangerouslySetInnerHTML`) on `<= 65 KB` slice; `extracted_text` rendered as plain text. `smartrss_client_runner.py` rejects raw/percent-encoded `..`, null bytes, backslashes, symlinks outside `dist_root.resolve()`, disables directory listing, and strictly parses `SMARTRSS_API_ORIGIN`. |
| **S3** | Secrets in logs/URLs/argv | `bearer_token` stored only in Dexie `client_meta`, sent only in `Authorization`, never logged/queried; Sign Out revokes own device online first (`U-21`); origin change pauses before sending any `Authorization` header (`U-08`). |
| **S4** | Rendered URL scheme validation | `resolveSafeHttpUrl()` / `isSafeHttpUrl()` enforces `http:`/`https:` on EVERY link (including relative feed links resolved against `baseUrl`) with `rel="noopener noreferrer"`. |
| **S6** | Payload size caps survive encoding | Outbox batcher checks exact UTF-8 byte length (`TextEncoder`) capping at `1.5 MiB` (< server `2 MiB` `413`), `<= 500` mutations, and `<= 5,000` explicit `item_ids`. |
| **C1 / C2 / C6** | Multi-tab races, stale rollback & stuck work | Cross-tab lock (`navigator.locks` + IndexedDB lease fallback `U-05`) with pre-release dirty re-check (`U-09`). `items_base`/`feeds_base` + pure `base ⊕ outbox` recompute eliminates stale-snapshot rollback (`U-01`). Dropping `in_flight` eliminates stuck mid-crash rows (`U-02`). |
| **C5 / D3 / D6** | Clock drift, partial edits & canonical encodings | `server_now` clock-drift banner (`> 5 min`); `toCanonicalIso()` normalizes all incoming timestamps (`U-07`); outgoing timestamps canonical UTC + lowercase `uuidv4()`; `EditFeedModal` sends diff-only `PATCH` (`U-21`/`D3`). |
| **L1 / L2 / L4** | Loop survival, shutdown & bounded memory | Skew-safe bisection halts systematic `422` in `sync_error` without wiping outbox (`U-04`); streaming 2-phase bootstrap bounds heap to 1 page (`U-06`); `smartrss_client_runner.py` handles `SIGTERM`/`SIGINT`; eviction & 48h journal pruning bound storage. |
| **E2** | Silent drops | Rejected mutations, poison-pill quarantines, `misconfigured` CORS/Host probes (`U-17`), `> 5,000`-item post-restore bulk-read drops, clock drift, and denied `navigator.storage.persist()` all surface explicit banners/toasts. |
| **T2 / T3 / T5** | Coverage gates, mapped tests & lockfiles | `package-lock.json` (`npm ci`), `scripts/check-prereqs.sh` (`Q-4`), two-tier `scripts/check-frontend-coverage.mjs` (`>= 90%` on 6 critical, `>= 80%` on 4 core, `Q-1`), `>= 90%` runner pytest coverage, and 1:1 test matrix `1c-T01`–`1c-T15`. |

### High-Level Doc Alignments
- Updated in this design-review cycle: [docs/development-process.md](../development-process.md) §2.2 (two-tier `90%`/`80%` frontend coverage gate, `Q-1`), [docs/data-model.md](../data-model.md) §6.2 (client server-base + overlay model and 48h chunked scoped bulk-read journaling, `U-01`/`U-10`/`Q-3`), [docs/review-checklist.md](../review-checklist.md) (client browser checklist section).
- To apply on implementation (`development-process.md` §4.2): [docs/requirements.md](../requirements.md) §7 Open Question #1 (resolved in `1c`), [docs/architecture.md](../architecture.md) §3.1 & §7, `.env.example`, `docker-compose.yml`, and root `README.md`.

---

## 12. Verification & Test Plan (Critical Modules & Invariant Matrix)

### 12.1 Automated Coverage Gates (Enforced by `make check`, Owner Decision `Q-1`)
- **Frontend Critical Sync & Security Modules (`>= 90%` line & branch coverage)** enforced by `scripts/check-frontend-coverage.mjs`:
  - `src/security/uuid.ts`
  - `src/security/urlSafety.ts`
  - `src/security/sanitizeHtml.ts`
  - `src/sync/reconciliation.ts`
  - `src/sync/outbox.ts`
  - `src/sync/engine.ts`
- **Frontend Core Store & Search Modules (`>= 80%` line & branch coverage)** enforced by `scripts/check-frontend-coverage.mjs`:
  - `src/db/schema.ts`
  - `src/db/clientMeta.ts`
  - `src/search/queryParser.ts`
  - `src/search/localSearch.ts`
- **Localhost Runner (`>= 90%` line & branch coverage)** enforced by `make runner-test`:
  - `client-runner/smartrss_client_runner.py`
- **Backend Critical Core Modules (`>= 90%`)**: All 16 existing `1a`/`1b` modules remain enforced in `scripts/check-critical-coverage.py`.

### 12.2 Mandatory Test Matrix (Mapped to Requirements & `development-process.md` §2.4)

| # | Test Area | Layer | Required Assertions |
| :--- | :--- | :--- | :--- |
| **1c-T01** | **Localhost runner security, canonicalization & origin tag (`1c-D01`, `1c-D05`, `U-08`, `U-15`, `U-16`)** | Unit (`pytest`) | `Host` allow-list rejects foreign hosts (`400`); `127.0.0.1:4180` AND `[::1]:4180` return `308` → `http://localhost:4180`; `HEAD` requests return headers with empty body; invalid/newline/semicolon `SMARTRSS_API_ORIGIN` rejected at startup; path traversal (`../`, `%2e%2e%2f`, `%252e%252e%252f`, symlink escape) returns `403`/`404`; missing `.js`/`.css` returns `404` while SPA route `/health` returns `index.html`; `/sw.js` appends deterministic origin tag that changes when `SMARTRSS_API_ORIGIN` changes; exact CSP `connect-src`, `Cache-Control`, and `/client-config.json` headers. |
| **1c-T02** | **Single-pass DOMPurify XSS/mXSS neutralization, remote image blocking & relative URL safety (`NFR-SEC-03`, `U-13`)** | Unit (`vitest`) | Hostile XSS fixtures (`<script>`, `<img onerror>`, `<svg onload>`, `<iframe srcdoc>`, `<a href="javascript:alert(1)">`, `//evil`, `data:text/html`, `style="background:url(...)"`, DOM-clobbering `id="location"`, `<form action>`) + DOMPurify mXSS namespace-confusion fixtures (`<math><mtext><table><mglyph><style>…`, `<noscript>`, `<form><math>`) + external `<img src="https://tracker.test/p.gif" alt="Chart">` + relative `<a href="/2026/11/post">` resolved against `baseUrl`. Verify single-pass `DocumentFragment` output contains zero scripts/handlers/`<img>`/`style`/foreign `class`, valid `https://` images become safe placeholders, relative links resolve to safe `http(s)` URLs with `target="_blank" rel="noopener noreferrer"`, and `extracted_text` renders as plain text. |
| **1c-T03** | **Server-base + overlay reconciliation, numeric `starred_num` index, timestamp normalization & eviction (`U-01`, `U-03`, `U-07`, `U-15`)** | Unit (`vitest` + `fake-indexeddb` + `fast-check`) | (a) `fast-check` property test over interleaved pulls, pushes, and outbox removals asserting `display === base ⊕ outbox` across all fields. (b) Explicit `U-01` regressions: `503` → pull (`is_starred=true`) → `422` quarantine leaves item starred; rolling back `M1=item_set_state(read)` preserves pending `M2=item_set_starred(true)`. (c) Scoped bulk-read overlay during pull marks ONLY `captured_item_ids`, never newly pulled items. (d) `U-03`: query `starred_num` & `[starred_num+effective_date]` in `fake-indexeddb` without `DataError`. (e) `U-07`: normalize `...Z`, `....500000Z`, and `...+00:00` DTOs and assert exact lexicographical sort order. (f) Eviction deletes `expired`/`>14d read` unstarred items, protects outbox-referenced items, skips incoming out-of-window unstored deltas. |
| **1c-T04** | **Outbox crash recovery, chunking, skew-safe bisection & rollback UX (`FR-SYNC-04`, `U-02`, `U-04`, `S6`)** | Unit (`vitest` + `fake-indexeddb`) | (a) `U-02`: simulate mid-push crash before response processing; start fresh `SyncEngine`; verify rows re-send with identical `client_mutation_id` and incremented `attempts`. (b) Queue 650 mutations (and > 5,000 explicit `item_ids` / > 1.5 MiB payload) → FIFO chunks respect all 3 caps, canonical UTC ISO & lowercase `uuidv4()`, and `sync_cursor` is **never** modified from push (`P-03`). (c) `U-04`: stub returning `422` for all requests halts in `sync_error` leaving entire outbox intact (zero quarantines/rollbacks); 1 bad mutation among 9 good ones isolates & quarantines only the 1 bad mutation while flushing the 9 good ones; **Retry** re-enqueues quarantined row. (d) `superseded` merges slim `current_entity` into `items_base`; `rejected` recomputes from base + creates `sync_notifications`. |
| **1c-T05** | **Streaming resumable bootstrap, restore recovery & 48h replay journal (`U-06`, `U-10`, `Q-3`, `P-11-F1`)** | Unit (`vitest` + `fake-indexeddb`) | (a) `U-06`: 3-page streaming bootstrap fails on page 3, preserves `items_base`/`items`, resumes from page 3's cursor, finishes chunked swap + trailing pull; epoch flip mid-pagination restarts cleanly. (b) `U-10`/`Q-3`: `applied` scoped bulk read (`<= 5,000` IDs) journals chunked `scope='items'` into 48h `replay_journal`; `replay_journal` prunes entries `> 48h`; BOTH `epoch_mismatch` AND `cursor_ahead` rewrite pending scoped bulk reads (`<= 5,000` IDs; drop+notify if `> 5,000`), promote 48h `replay_journal`, re-bootstrap, and flush cleanly. |
| **1c-T06** | **Origin change guard, `401` re-pair fresh IDs, multi-tab wake-up, `no-cors` probe & `versionchange` (`U-05`, `U-08`, `U-09`, `U-11`, `U-14`, `U-17`)** | Unit (`vitest` + `fake-indexeddb`) | (a) `U-08`: `config.api_origin !== auth.api_origin` transitions to `origin_confirm` and sends zero `Authorization` headers. (b) `U-11`: push times out after committing on server, device is revoked (`401` preserves outbox) and re-paired → fresh `client_mutation_id`s minted, re-flush produces zero rejection toasts. (c) `U-09`: enqueue between lock holder's last outbox read and lock release triggers immediate follow-up push. (d) `U-05`: `uuidv4()` works with `crypto.randomUUID = undefined`; IndexedDB lease lock serializes two tabs when `navigator.locks = undefined`. (e) `U-17`: CORS probe `TypeError` + resolving `no-cors` transitions to `misconfigured`. (f) `U-14`: `versionchange` event closes Dexie handle cleanly and triggers reload prompt; clock drift `> 5 min` sets banner flag. |
| **1c-T07** | **Detached local search (`MiniSearch`), benchmark & XSS-safe snippets (`FR-STATE-04`, `1c-D04`, `U-20`)** | Unit (`vitest`) | `title > author > lead_excerpt` ranking, `"exact phrase"`, `foo -bar`, negation-only `-foo` validation error, facet filters, incremental add/update/evict sync, `<script>alert(1)</script>` emitted as inert `SnippetFragment[][]`, plus 5,000-item hydration & query timing benchmark recording actual latency. |
| **1c-T08** | **Connected search `P-17` display-only & `U-12` archive-only mutation** | Unit (`vitest` + `fake-indexeddb`) | Connected search returns an `expired` archive item not in Dexie → Dexie `items_base`/`items` counts stay unchanged; starring enqueues `target_absent: true` mutation updating React display overlay; simulating `rejected` reverts React display cleanly and emits notification without writing to Dexie `items`. |
| **1c-T09** | **Playwright E2E: Pairing, Bootstrap, 3-Pane Reader, Sorting & Keyboard Shortcuts (`FR-UI-01`, `FR-UI-02`)** | E2E (Playwright + real API/PG + `localhost:4180` runner) | Pair device via `POST /api/v1/auth/pair` → bootstrap seeded feeds/items → verify sort headers (`Relevance`, `Date`, `Feed`), filters, keyboard `j`/`k`/`m`/`s`/`/`, external link `target="_blank" rel="noopener noreferrer"`, and `navigator.storage.persist()` invoked. |
| **1c-T10** | **Playwright E2E: Detached Offline Reload Survival, 500-Mutation `< 5 s` Flush & SW Update (`NFR-SYNC-02`, ADR 0005, `U-14`, `U-15`)** | E2E (Playwright + real API/PG + `localhost:4180` runner) | (a) Load paired app on `http://localhost:4180` → wait for SW active + bootstrap complete → disconnect LAN API → **hard reload offline** → verify SW serves app shell with zero errors → `"Detached"` badge & online-only controls disabled → run **detached local search** → perform offline mutations (`item_set_state`, `item_set_starred`, bounded bulk mark-read, unsubscribe/mute feed) → reload offline again (outbox survives!) → restore LAN → zero lost mutations & `0` pending outbox. (b) Timed **500-mutation flush** from offline outbox completes in `< 5.0 s` (`NFR-SYNC-02`). (c) Serving an updated `/sw.js` triggers the `"Update available — Reload"` banner and activates cleanly on click (`U-14`). |
| **1c-T11** | **Playwright E2E: Rejection Rollback UX & Clock-Drift Banner (`FR-SYNC-04`)** | E2E (Playwright + real API/PG) | While detached, move feed to Category C; delete Category C on backend; reconnect → push returns `rejected (category_not_found)` → verify feed reverts cleanly from `feeds_base` in UI and rejection toast renders. |
| **1c-T12** | **Playwright E2E: Hostile XSS/mXSS Fixtures, Positive & Negative CSP Proof (`NFR-SEC-03`, `U-15`)** | E2E (Playwright + real API/PG + `localhost:4180` runner) | Seed item with hostile `<script>`, `<img onerror>`, `<svg onload>`, mXSS `<math>`, `<a href="javascript:...">`, and `<img src="https://blocked.invalid/pixel.gif">`. Open item & search `<script>`: assert `window.__xssExecuted === undefined`, zero network requests to `blocked.invalid`, zero unexpected `securitypolicyviolation` events across the entire flow. **Negative CSP proof**: evaluate `fetch('https://other.example')` from page context and assert a `securitypolicyviolation` IS raised (`connect-src` pinning proven!). |
| **1c-T13** | **Playwright E2E: System Health, Pipeline Metrics, Feed Health & Dead-Letter UI (`NFR-OPS-01`, `FR-FEED-03`)** | E2E (Playwright + real API/PG) | Seed `stage_metrics`, `pipeline_runs`, degraded/broken feeds, and a dead-letter job → verify `24h`/`7d`/`30d` stage latency table, per-feed cost table, `pipeline_runs` history, severity-sorted Feed Health, and Dead-Letter Retry/Dismiss actions update DB cleanly. |
| **1c-T14** | **Playwright E2E: Feeds/OPML, Diff-Only `EditFeedModal`, `409` Conflict & Device Revocation/Sign Out (`FR-FEED-01`, `NFR-SEC-01`, `U-21`)** | E2E (Playwright + real API/PG) | Import & export OPML; edit `curation_instructions` (verifying `PATCH` omits unchanged LWW fields) with simulated concurrent version bump (`409` → conflict diff dialog resolves cleanly); generate pairing code for a second device; revoke a device (confirming revoked client transitions to `auth_revoked` without wiping outbox); Sign Out revokes own device server-side. |
| **1c-T15** | **Playwright E2E: Non-Localhost Deployment Topology & Insecure Nginx `web` LAN Fallback (`U-05`, `U-15`, `Q-2`)** | E2E (Playwright + Chromium `--host-resolver-rules` + real API/PG + Nginx `web`) | (a) Open Docker/Nginx `web` fallback on non-localhost plain HTTP (`http://smartrss.test:8080`, `window.isSecureContext === false`): verify pairing, bootstrap, and read/star/feed-toggle mutations succeed via `crypto.getRandomValues` UUIDv4 + IndexedDB lease lock without `TypeError`, and connected-LAN badge displays. (b) Open `localhost:4180` runner configured with non-localhost `SMARTRSS_API_ORIGIN=http://api.smartrss.test:8080` (with matching `ALLOWED_HOSTS`/`CORS_ALLOW_ORIGINS`): verify cross-origin pairing, sync, and CSP `connect-src` pinning work end-to-end. |
