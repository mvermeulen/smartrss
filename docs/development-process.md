# Development, Testing & Documentation Process

> **Status:** In Review

This document defines **how** SmartRSS is developed, tested, documented, and evolved. It applies to both human contributors and AI coding agents.

---

## 1. Core Development Workflow

SmartRSS follows a **spec-first, test-integrated, docs-synchronized** workflow:

1. **Align with Specs First**: Before writing code for a feature or significant change, review the relevant docs in `docs/requirements.md`, `docs/architecture.md`, `docs/data-model.md`, and `docs/design/`. If requirements or contracts are unclear or changing, update the spec first.
2. **Implement + Test Together**: Write automated tests alongside production code—never defer tests to a "later cleanup pass."
3. **Verify Locally**: Run linting, type-checking, and the full relevant test suite (including coverage checks) before considering a task done.
4. **Sync Documentation**: Update affected design docs, schemas, ADRs, and `AGENTS.md` in the **same change** as the code.
5. **Small, Focused Commits**: Keep commits cohesive (feature + its tests + its doc updates together) with clear commit messages describing *why* as well as *what*.

---

## 2. Testing Standards

Testing is an integral part of every code change, not an optional verification step.

### 2.1 Test Layers
| Layer | Scope | Tooling (Planned) | Requirements |
| :--- | :--- | :--- | :--- |
| **Unit Tests** | Pure domain logic: URL canonicalization, expiration TTL resolution, conflict resolution, mini-research guardrails, prompt rendering, Pydantic schema validation, feed backoff math | Backend: `pytest`<br>Frontend: `vitest` | Fast (milliseconds), deterministic, zero network or DB dependencies. |
| **Integration Tests** | FastAPI endpoints, PostgreSQL + `pgvector` queries, `SKIP LOCKED` job queue, FTS indexing, migrations, full sync push/pull flows | `pytest` + test PostgreSQL (`pgvector`) instance | Exercise real SQL, transactions, constraints, and `sync_seq` ordering—do not mock away Postgres behavior. |
| **External Boundary & Contract Tests** | RSS/Atom parsing, HTML/PDF extraction, OpenAI-compatible LLM adapter (`Ollama` / `llama.cpp`) | Recorded HTTP fixtures (`respx` / `pytest-httpx`) + sample HTML/PDF/RSS test fixtures | Deterministic in CI/local runs; test both happy paths and hostile inputs (malformed XML, broken PDFs, 403/429/500 responses, invalid LLM JSON, timeouts). |
| **Client Offline & Sync Tests** | IndexedDB cache, persistent outbox queueing, reconnect flush, optimistic UI updates, detached-mode transitions | `vitest` + `fake-indexeddb` (plus Playwright E2E for critical user flows) | Verify no data loss or duplicate mutations across disconnect/reconnect cycles. |
| **Optional Live-LLM Smoke / Eval Suite** | Prompt quality & structured output reliability against actual local `Ollama` / `llama.cpp` models | `pytest -m live_llm` (opt-in, excluded from default fast suite) | Used when tuning prompts, interest profiles, or upgrading local models. |

### 2.2 Coverage Expectations
- **Target Coverage**:
  - **Backend overall**: **>= 80%** line coverage (`pytest --cov`).
  - **Critical core modules** (sync reconciliation, expiration policy, job queue, URL canonicalization/dedup, mini-research guardrails, LLM output validation): **>= 90%** line/branch coverage.
  - **Frontend core logic** (local store, sync engine, outbox/conflict handling, state reducers): **>= 80%** line coverage.
- **Quality Over Vanity Metrics**:
  - Do **not** write low-value tests that merely assert mock call counts or test framework boilerplate to inflate numbers.
  - Assert observable behavior, state transitions, invariants, and error handling.
  - Coverage should never decrease silently on touched modules without an explicit reason.

### 2.3 Regression Testing Policy (Mandatory)
- **Every bug fix MUST include a regression test**:
  1. Write a test that reproduces the exact bug and verify it **fails** before the fix.
  2. Apply the fix and verify the new test **passes** along with the existing suite.
  3. Name or comment the test clearly so future maintainers understand the failure mode it guards against.
- **Real-World Feed/Document Quirks**: Whenever a real RSS feed, webpage, PDF, or LLM response triggers an unexpected edge case, add a sanitized minimal fixture to the test suite as a permanent regression case.

### 2.4 Non-Negotiable Invariant Test Matrix
The following SmartRSS invariants must always have dedicated automated test coverage:
- **Offline Sync Idempotency & Ordering**: Replaying the same `client_mutation_id` multiple times produces the exact same server state; out-of-order timestamps obey deterministic per-field last-write-wins; `sync_seq` pagination never drops or duplicates records.
- **Non-Destructive Expiration & Starred Immunity**: Unread unstarred items transition to `expired` using feed override → global default TTL; `expiration_days_override = 0` never expires; starred items **never** expire; expired items remain in the historical log.
- **Mini-Research Guardrails**: Enforces max hop depth, max response size (HTML & PDF), timeout caps, unsupported content-type rejection, and redirect limits.
- **Graceful LLM Degradation**: Ingestion and API sync continue normally when `Ollama`/`llama.cpp` is unreachable; items appear in `pending` enrichment state and backfill cleanly when LLM returns; malformed JSON triggers bounded retry + clean failure state.
- **Polite Feed Polling**: Sends `ETag`/`Last-Modified`, handles `304 Not Modified` / `301` / `410`, increments `consecutive_errors` and applies exponential backoff on failures, resets error state on recovery.

---

## 3. Code Quality & Tooling Standards

- **Backend (Python 3.12+)**:
  - Formatting & linting: `ruff format` and `ruff check`.
  - Static typing: `mypy` or `pyright` on strict/standard settings; Pydantic v2 models for all API/LLM boundaries.
  - Database migrations: Versioned migrations (`alembic`); every schema change must have a reversible migration tested against a clean database.
- **Frontend (TypeScript)**:
  - `strict: true` in `tsconfig.json`; no untyped `any` across API/sync boundaries.
  - Linting & formatting: `eslint` + `prettier` (or `biome`).
- **Automated Checks**: Provide a single repeatable command (e.g., `Makefile` or `justfile` targets like `make check`, `make test`, `make test-cov`) so both humans and agents run the exact same verification gates.

---

## 4. Documentation Maintenance Standards

Documentation is treated as a **first-class deliverable**, kept continuously synchronized with the codebase.

### 4.1 Docs-in-the-Same-Change Rule
Whenever a code change alters any of the following, the corresponding documentation **must** be updated in the same commit/PR:
- **New or changed API endpoints / Sync protocol fields** → Update `docs/design/sync-protocol-and-offline-client.md` (and OpenAPI schema).
- **Database tables, columns, indexes, or retention rules** → Update `docs/data-model.md`.
- **Service topology, external dependencies, or pipeline stages** → Update `docs/architecture.md`.
- **LLM prompts, structured output schema, mini-research rules, or feedback mechanics** → Update `docs/design/llm-pipeline-and-feedback.md` / `docs/design/ingestion-and-mini-research.md`.
- **Scope, goals, or milestone changes** → Update `docs/requirements.md`.
- **Environment variables, Docker setup, or operational commands** → Update root `README.md` / `docs/architecture.md`.

### 4.2 Architecture Decision Records (ADRs)
Create a new ADR in `docs/decisions/NNNN-short-title.md` (and update `docs/decisions/README.md`) whenever making or revising a decision that:
- Introduces or swaps a core dependency, container, or protocol.
- Changes data lifecycle, sync conflict semantics, or security/network boundaries.
- Involves meaningful trade-offs that a future contributor might otherwise question or accidentally undo.

When an older ADR is replaced, mark it `Superseded by NNNN` rather than deleting it.

### 4.3 Keeping Status & Index Accurate
- Update [docs/README.md](README.md) whenever documents are added or move between `Draft`, `In Review`, and `Approved`.
- Periodically review `docs/` during milestone transitions to retire stale notes and verify doc links remain valid.

---

## 5. Evolving `AGENTS.md` & Project Skills Based on Feedback

The development process itself should improve over time based on real experience and user feedback:

- **When to update `AGENTS.md`**:
  - Whenever the user corrects an agent behavior, clarifies a recurring convention, or establishes a new guardrail.
  - Keep `AGENTS.md` concise and high-signal (well under the 24 KB rule cap): state the **rule/guardrail directly**, and link to detailed specs in `docs/` rather than duplicating long explanations.
- **When to update `docs/development-process.md`**:
  - When expanding detailed testing strategies, coverage rules, tooling commands, or release/migration procedures.
- **When to create a Project Skill (`.agents/skills/<name>/SKILL.md`)**:
  - When a multi-step engineering workflow repeats across tasks (e.g., adding a DB migration + updating `data-model.md`, running the live-LLM prompt eval suite, or executing a spec-to-implementation checklist).
  - Skills keep detailed step-by-step runbooks out of always-on context until needed.

---

## 6. Definition of Done (Checklist)

Before marking any implementation task complete (or committing), verify:
- [ ] Behavior matches the approved specs in `docs/` (or specs were updated first).
- [ ] Unit and integration tests cover the new behavior and edge cases.
- [ ] Bug fixes include a dedicated regression test that failed before the fix.
- [ ] Linter, type checker, and test suite pass cleanly, meeting coverage targets.
- [ ] Affected docs (`docs/requirements.md`, `docs/architecture.md`, `docs/data-model.md`, `docs/design/*`, `docs/decisions/*`, `docs/README.md`) are updated in the same change.
- [ ] Any new convention, guardrail, or lesson learned from user feedback is reflected in `AGENTS.md`, `docs/development-process.md`, or `.agents/skills/`.
