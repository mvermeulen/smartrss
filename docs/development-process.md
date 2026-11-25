# Development, Testing & Documentation Process

> **Status:** Approved *(2026-11-23, by project owner)*

This document defines **how** SmartRSS is developed, tested, documented, and evolved. It applies to both human contributors and AI coding agents.

---

## 1. Core Development Workflow

SmartRSS follows a **spec-first, test-integrated, docs-synchronized** workflow:

1. **Align with Specs First**: Before writing code for a feature or significant change, review the relevant docs in `docs/requirements.md`, `docs/architecture.md`, `docs/data-model.md`, and `docs/design/`. If requirements or contracts are unclear or changing, update the spec first.
2. **Implement + Test Together**: Write automated tests alongside production code—never defer tests to a "later cleanup pass."
3. **Verify Locally**: Run `make check` (linting, type-checking, unit + real-Postgres integration tests, coverage gates) before considering a task done. *(No GitHub Actions CI is required; local verification gates are mandatory on every change.)*
4. **Sync Documentation**: Update affected design docs, schemas, ADRs, and `AGENTS.md` in the **same change** as the code.
5. **Small, Focused Commits**: Keep commits cohesive (feature + its tests + its doc updates together) with clear commit messages describing *why* as well as *what*.
6. **Milestone Branches**: Implement each milestone on a `milestone/<id>` branch so reviews target a committed diff. Merge to `main` only after the owner sets `Implementation Approved` (§4.1, §7). Doc-only changes (designs, reviews, process) may go directly to `main`.

---

## 2. Testing Standards

Testing is an integral part of every code change, not an optional verification step.

### 2.1 Test Layers & Infrastructure
| Layer | Scope | Tooling | Requirements |
| :--- | :--- | :--- | :--- |
| **Unit Tests** | Pure domain logic: URL canonicalization, expiration TTL resolution, per-field LWW conflict merge, clock-skew clamping, SSRF IP range validation, mini-research guardrails, prompt rendering/delimitation, Pydantic schemas, feed backoff math | Backend: `pytest` + `hypothesis` (property-based tests for sync convergence)<br>Frontend: `vitest` | Fast (milliseconds), deterministic, zero network or DB dependencies. |
| **Integration Tests (Real PostgreSQL)** | FastAPI endpoints, `sync_state`/`sync_changes` concurrent-commit ordering, `bootstrap`/`pull`/`push`, `instance_epoch` restore drill, PostgreSQL + `pgvector` clustering, `SKIP LOCKED` job queue + lease reclaim, FTS indexing, Alembic migrations | `pytest` against a real `pgvector/pgvector:pg16` test database started via `docker compose --profile test` (or `testcontainers`) | Exercise real SQL, transactions, locks, constraints, and `sync_seq` ordering—never mock away PostgreSQL behavior. |
| **External Boundary & Contract Tests** | RSS/Atom parsing, HTML/PDF extraction in bounded subprocess, `SafeHttpClient` SSRF/redirect/IP-pinning, `LLMAdapter` for **both** `Ollama` and `llama.cpp` | Recorded HTTP fixtures (`respx` / `pytest-httpx`) + sanitized sample & hostile HTML/PDF/RSS/LLM fixtures | Deterministic in default runs; cover both runtimes (`Ollama` & `llama.cpp`) and hostile inputs (malformed XML, crafted PDF bombs, redirect loops, private-IP SSRF, 403/410/429, invalid LLM JSON, `<think>` blocks, timeouts). |
| **Client Offline, Security & E2E Tests** | Dexie.js IndexedDB cache, persistent outbox queueing, optimistic rollback on `rejected`, detached local search, Service Worker offline reload, DOMPurify XSS neutralization, strict CSP | `vitest` + `fake-indexeddb` + Playwright E2E | Must include an offline-reload E2E test: load app → disconnect LAN API → reload → search locally + perform offline mutations → reconnect → verify zero lost mutations. |
| **Opt-In Live-LLM Smoke & Eval Suite** | Real MoE structured-output reliability, throughput benchmarking, and offline relevance evaluation against historical thumbs up/down feedback | `pytest -m live_llm` (opt-in, excluded from default fast suite) | Run during `1d` model benchmarking, `1g` eval setup, and whenever upgrading models or editing prompts/Interest Profile. |

### 2.2 Coverage Expectations
- **Target Coverage**:
  - **Backend overall**: **>= 80%** line coverage (`pytest --cov`).
  - **Critical core modules** (sync watermark & reconciliation, per-field LWW merge, expiration policy, job queue & lease reclaim, URL canonicalization/dedup, SSRF `SafeHttpClient`, mini-research guardrails, LLM schema validation): **>= 90%** line/branch coverage.
- **Enforcement** (`make test-cov`, part of `make check`):
  - The overall gate is `pytest --cov-fail-under=80`.
  - The per-module critical gate is [`scripts/check-critical-coverage.py`](../scripts/check-critical-coverage.py). It reads the `coverage.json` written by `pytest --cov-report=json` and fails (exit `1`) if any module in its `CRITICAL_MODULES` list is below `MIN_CRITICAL_PCT` (`90`), or is missing from the report.
  - The percentage is `coverage.py`'s `percent_covered`, which combines lines and branches under `--cov-branch`.
  - Run it manually with `python3 scripts/check-critical-coverage.py backend/coverage.json` after `pytest --cov=smartrss --cov-branch --cov-report=json:coverage.json` in `backend/`.
  - **Maintaining the list**: when a milestone adds or renames a critical module (as listed in that milestone's component design §"Verification & Test Plan"), update `CRITICAL_MODULES` in the **same commit**. Never lower `MIN_CRITICAL_PCT` or drop a module to make the gate pass; add meaningful tests instead.
  - **Frontend core logic** (Dexie local store, sync engine, outbox/rollback handling, detached search, HTML sanitization): **>= 80%** line coverage.
- **Quality Over Vanity Metrics**:
  - Do **not** write low-value tests that merely assert mock call counts or test framework boilerplate to inflate numbers.
  - Assert observable behavior, state transitions, invariants, and error handling.
  - Coverage must not silently regress on touched modules.

### 2.3 Regression Testing Policy (Mandatory)
- **Every bug fix MUST include a regression test**:
  1. Write a test that reproduces the exact bug and verify it **fails** before the fix.
  2. Apply the fix and verify the new test **passes** along with the existing suite.
  3. Name or comment the test clearly so future maintainers understand the failure mode it guards against.
  4. **Prove it** (required when the fix was written before the test, which is the usual case for review findings): temporarily revert or disable the fix, run the test, confirm it **fails for the expected reason**, restore the fix, and confirm it passes. Then check that the coverage report shows the fixed lines as covered. A test that passes with and without the fix does not count. *(`1a` lesson: the first `I-02` test failed a different code path and passed either way.)*
  5. **Record the evidence** in the review resolution or commit message, e.g. ``I-02: test_i02_… — reverted slot `except Exception`, test failed (slot died); restored, passed; runner.py:659-667 covered``.
- **Real-World Feed/Document Quirks**: Whenever a real RSS feed, webpage, PDF, or LLM response triggers an unexpected edge case, add a sanitized minimal fixture to the test suite as a permanent regression case.

### 2.4 Non-Negotiable Invariant Test Matrix
The following SmartRSS invariants must always have dedicated automated test coverage:
- **Gap-Free Committed Sync Watermark**: Real-Postgres concurrency test interleaving two transactions so the earlier-starting transaction commits later, proving `sync/pull` never skips or duplicates records.
- **Backup Restore & `instance_epoch` Recovery**: Test simulating a DB restore that rewinds `current_seq` and rotates `instance_epoch`, proving clients preserve unsynced outboxes, cleanly re-bootstrap, and flush mutations idempotently.
- **Offline Sync Idempotency & Conflict Convergence**: Replay tests for duplicate `client_mutation_id`s, clock-skew clamping tests, `hypothesis` property-based tests proving per-field LWW convergence regardless of mutation arrival order, and bounded bulk mark-read tests proving unseen items are untouched.
- **Detached Client Survival (Playwright)**: Load app on `localhost` origin → disconnect API → reload page offline → verify app loads via Service Worker, detached local search works, and outbox survives reload and flushes on reconnect (including rejection rollback UX).
- **LAN Auth, CSRF & DNS Rebinding Security**: Verify missing/invalid/revoked bearer tokens return `401`, disallowed `Host` headers are rejected, disallowed CORS origins are blocked, non-JSON mutation posts are rejected, and expired/reused pairing codes fail.
- **SSRF Protection Across All Fetchers**: Verify `SafeHttpClient` blocks loopback, RFC1918, link-local, CGNAT, IPv6 ULA, Docker subnets, non-HTTP(S) schemes, and multi-hop redirects pointing to private IPs (plus IP pinning against DNS rebinding) in polling, extraction, and mini-research.
- **XSS, CSP & Parser Isolation**: Verify hostile feed HTML/LLM output is sanitized by DOMPurify under strict CSP, untrusted prompt text is delimited and schema-validated, and oversized/pathological PDFs/HTML hit subprocess CPU/RAM/page/byte limits without crashing the worker.
- **Non-Destructive Expiration & Starred Immunity**: Verify `fetched_at`-based TTL (and `unexpired_at` reset), feed override → global default, `0` = never expire, starred immunity, backfill cap (`N = 10`), and permanent history retention.
- **Graceful LLM Degradation, Dual-Runtime Contract & Direct Feed Guidance**: Verify ingestion/UI work normally when LLM is offline, `Ollama` and `llama.cpp` fixtures both pass (including malformed JSON recovery, `<think>` stripping, and timing telemetry parsing), per-feed `curation_instructions` + active `feed_instruction` notes render in the dedicated prompt section with untrusted article text strictly delimited, offline `feed_instruction` add/archive syncs idempotently, and concurrency stays bounded to `1`.
- **Polite Feed Polling, Cron Batch Idempotency & Performance Metrics**: Verify `ETag`/`Last-Modified`, `304`/`301` (including URL collision)/`410`/`429` + `Retry-After`, exponential backoff, separate `status` vs `health_state`, job dedupe uniqueness, stale-lease reclaim after worker crash, overlapping `worker run-once` batch calls never double-process, and `stage_metrics` / `pipeline_runs` accurately capture per-stage latency and counts.

---

## 3. Code Quality & Tooling Standards

- **Backend (Python 3.12+)**:
  - Formatting & linting: `ruff format` and `ruff check`.
  - Static typing: `mypy` or `pyright`; Pydantic v2 models for all API and LLM boundaries.
  - Database migrations: Versioned `alembic` migrations; every schema change must have a reversible migration tested against a clean PostgreSQL database.
- **Frontend (TypeScript)**:
  - `strict: true` in `tsconfig.json`; React + Vite + Dexie.js + DOMPurify; no untyped `any` across API/sync boundaries.
  - Linting & formatting: `eslint` + `prettier` (or `biome`).
- **Repeatable Local Verification (`Makefile`)**:
  - Provide standard targets (`make check`, `make test`, `make test-unit`, `make test-int`, `make test-e2e`, `make test-cov`, `make test-live-llm`, `make backup`, `make restore-drill`) so humans and agents run the exact same local verification gates.

---

## 4. Documentation Maintenance & Approval Standards

Documentation is a **first-class deliverable**, kept continuously synchronized with the codebase.

### 4.1 Approval Mechanism (Coding Gate)
- `AGENTS.md` gates application coding on approved specs.
- **Who approves**: The project owner explicitly reviews and approves specs and ADRs.
- **How approval is recorded**: Updating the document status to **`Approved`** (in the doc header and [docs/README.md](README.md)) and ADR status from `Proposed` to **`Accepted`** in a git commit.
- Component designs in `docs/design/` are approved milestone-by-milestone (`1a`, `1b`, …) before implementing each milestone.
- **Milestone lifecycle** (recorded in the component design header and its [docs/README.md](README.md) / [design/README.md](design/README.md) rows):

  | Status | Meaning | Set by |
  | :--- | :--- | :--- |
  | `Draft` | Design being written | Author |
  | `In Design Review` | Design review open in `docs/reviews/` | Author / reviewer |
  | `Approved` | Design review `Verified`; coding may start | **Project owner only** |
  | `Implemented` | Code, tests, and docs complete; `make check` green | Implementer |
  | `In Implementation Review` | Implementation review open | Implementer / reviewer |
  | `Implementation Approved` | Implementation review `Verified`; milestone done; next milestone's coding may start | **Project owner only** |

- Agents never set `Approved` or `Implementation Approved` on their own initiative; they do so only when the owner explicitly says to, in the same commit that records it.

### 4.2 Docs-in-the-Same-Change Rule
Whenever a code change alters any of the following, update the corresponding docs in the **same commit**:
- API endpoints, sync protocol, or push/pull schemas → `docs/design/1b-sync-protocol-and-search.md` (and OpenAPI).
- Database tables, columns, indexes, state machines, or retention rules → `docs/data-model.md`.
- Service topology, networking, security boundaries, or pipeline stages → `docs/architecture.md`.
- LLM prompts, adapter contracts, clustering, or mini-research rules → `docs/design/` specs.
- Scope, acceptance criteria, or milestones → `docs/requirements.md`.
- Environment variables, Docker setup, or backup/restore commands → root `README.md` / `docs/architecture.md`.

### 4.3 Architecture Decision Records (ADRs)
Create a new ADR in `docs/decisions/NNNN-short-title.md` (and update `docs/decisions/README.md`) whenever introducing/swapping a core dependency, protocol, security/network boundary, or data/sync invariant. Mark replaced ADRs `Superseded by NNNN` rather than deleting them.

---

## 5. Evolving `AGENTS.md` & Project Skills Based on Feedback

- **When to update `AGENTS.md`**: Whenever the user corrects an agent behavior, clarifies a recurring convention, or establishes a new guardrail. Keep `AGENTS.md` lean (well under the 24 KB limit) and link to `docs/` for bulky details.
- **When to update `docs/development-process.md`**: When refining testing standards, invariant matrices, tooling commands, or release/backup drills.
- **When to create a Project Skill (`.agents/skills/<name>/SKILL.md`)**: When a multi-step engineering procedure repeats across sessions (e.g., `re-embed-and-recluster`, `add-alembic-migration`, `run-live-llm-eval`, `restore-drill`).

---

## 6. Definition of Done (Checklist)

Before marking any implementation task complete (or committing), verify:
- [ ] Behavior matches the approved specs in `docs/` (or specs were updated first).
- [ ] Unit, real-Postgres integration, and boundary/E2E tests cover new behavior and edge cases.
- [ ] Bug fixes include a dedicated regression test that failed before the fix.
- [ ] Local verification (`make check` / coverage gates) passes cleanly.
- [ ] Affected docs (`docs/requirements.md`, `docs/architecture.md`, `docs/data-model.md`, `docs/design/*`, `docs/decisions/*`, `docs/README.md`) are updated in the same commit.
- [ ] Any new convention, guardrail, or lesson learned from user feedback is reflected in `AGENTS.md`, `docs/development-process.md`, or `.agents/skills/`.
- [ ] Each regression test has recorded "fails with the fix reverted" evidence (§2.3).
- [ ] Status changes appear in the doc header **and** the `docs/README.md` index, with no stale or duplicate rows.
- [ ] For milestone completion: implementation review exists, is `Verified`, and residual risks are owner-accepted (§7).

---

## 7. Review Gates

Reviews are a required, recorded part of every milestone, not an optional extra. In `1a`, the design review found 6 blockers that `Approved`-ready prose hid. The implementation review found a security blocker (`I-01`) after `make check` passed. Each verification pass then found that some "Addressed" claims were incomplete (`N-01`–`N-11`; the `I-02`/`I-04`/`I-18`/`I-09` follow-ups).

### 7.1 When reviews happen
| Gate | Input | Must reach `Verified` before |
| :--- | :--- | :--- |
| **Design review** | Component design `Draft` (plus touched requirements, data model, ADRs) | Owner sets design `Approved` |
| **Implementation review** | The milestone's code, tests, migrations, scripts, and doc diff (ideally a committed branch diff, not an uncommitted tree) | Owner sets `Implementation Approved` |
| **Ad-hoc review** *(optional)* | Large refactor, security-boundary change, new ADR | Merge of that change |

### 7.2 Review document format
- **File**: `docs/reviews/YYYY-MM-DD-<scope>-<design|implementation>-review.md`. Add a row to the [docs/README.md](README.md) index with status `Open`, and update that same row afterwards (never add a second row).
- **Header**: date, scope (paths plus base commit), specs checked against, reviewer (agent/model or person), and status.
- **Finding IDs**: one letter prefix per review, unique across the repo (used so far: `R`, `Q`, `B`, `M`, `S`, `A`, `N`, `T`, `I`). Number findings sequentially and never renumber them.
- **Severity**:
  | Severity | Meaning | Before approval |
  | :--- | :--- | :--- |
  | **Blocker** | Security hole, data loss/corruption, or broken core invariant | Must be fixed |
  | **Major** | Incorrect behavior under realistic conditions, missing required test/gate, spec drift | Must be fixed or owner-accepted |
  | **Minor** | Edge case, hardening, or maintainability issue with limited impact | Fix or track |
  | **Nit** | Style or cleanup | Optional |
- **Each finding**: location (`path:line`), the problem, a concrete failure scenario or impact, and a suggested fix, including the regression test to add.
- **Required sections**: outcome summary (what is solid plus the overall recommendation), findings by severity, verification performed (`make check` result, reproductions), and, once resolved, **Resolutions** (per finding: fix, test name, §2.3 evidence) and **Accepted residual risks** (owner-acknowledged).

### 7.3 Review status lifecycle
`Open` → `Addressed` (implementer claims every finding is resolved, with evidence) → `Verified` (a separate verification pass confirms it) → the owner approves the design or implementation.
- **Who does what**: the reviewer (**Claude Code**) writes the review (`Open`). The implementer (**Antigravity**) fixes the findings, records resolutions and evidence, and sets `Addressed`. The reviewer runs the verification pass and sets `Verified`. The project owner approves.
- **Separate verification pass**: run by the reviewer in a fresh session, never by the agent that wrote the fixes. Check every finding, not a sample:
  - Re-read the fix.
  - Re-run the revert-the-fix check for Blocker/Major regression tests.
  - Confirm that the fix's lines are covered.
  - Confirm that the docs and index rows are updated.
  - Run `make check`.
- Anything the verification pass finds is appended to the same review as a new "Verification pass" section, using a new prefix (as `N-` was for `A-`) or as follow-ups. Resolve those before moving to `Verified`.

### 7.4 Reviewer focus
Beyond spec conformance, reviewers walk the recurring failure-class checklist in [`docs/review-checklist.md`](review-checklist.md). It covers untrusted input into libraries, stale reads across I/O, loop survival, lease ownership, DB limits, secrets in argv, event-loop blocking, encoding, and doc drift. Implementers self-check against the same list before requesting review. When a new class of bug is found, add it to that checklist.

### 7.5 Roles & skills
| Role | Agent | Skills |
| :--- | :--- | :--- |
| Designer / implementer | **Antigravity** | [`.agents/skills/prepare-for-review`](../.agents/skills/prepare-for-review/SKILL.md), [`.agents/skills/address-review-findings`](../.agents/skills/address-review-findings/SKILL.md) |
| Reviewer / verifier | **Claude Code** | [`.claude/skills/design-review`](../.claude/skills/design-review/SKILL.md), [`.claude/skills/implementation-review`](../.claude/skills/implementation-review/SKILL.md), [`.claude/skills/verify-review-resolution`](../.claude/skills/verify-review-resolution/SKILL.md) |
| Approver | Project owner | — |

**Hand-off**: the implementer runs `prepare-for-review`, then the owner asks Claude Code to review. The implementer runs `address-review-findings`, then the owner asks Claude Code to verify. The owner approves.
