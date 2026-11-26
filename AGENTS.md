# SmartRSS — Agent Rules & Project Guide

## Project Status
> **Current Phase: Phase 1 Implementation (Milestones `1a`–`1b` implemented & approved; `1c` design approved (implementation pending); `1d`–`1g` pending design approval)**
> Do **not** write application code for a milestone until the requirements, architecture, data model, ADRs, and that milestone's component design in `docs/` are marked **`Approved`** / **`Accepted`** by the project owner.

## Overview
**SmartRSS** is a self-hosted, distributed RSS reader system for a single user. A containerized backend server on the home LAN pre-fetches, persists, clusters, and curates feeds using local LLMs (`Ollama` / `llama.cpp`) to generate display and curation metadata (including selective mini-research on primary sources/PDFs), while local-first clients sync over home Wi-Fi, operate seamlessly when detached, and feed explicit user interactions back to evolve curation over time.

## Documentation Map
Read the relevant design docs before making changes or proposing implementations:

- [Documentation Index](docs/README.md) — Status tracker for all specs and reviews
- [Requirements](docs/requirements.md) — User stories, requirement IDs, acceptance criteria, non-goals, and milestones (`1a`–`1g`, Phase 2–3)
- [Architecture](docs/architecture.md) — Container topology, `LLMAdapter`, capacity/context budgets, security, and backup/restore ops
- [Data Model](docs/data-model.md) — Entities, state machines, sync surface, per-field conflict rules, job queue, and lifecycle
- [Component Designs](docs/design/README.md) — Milestone-sequenced subsystem design specs
- [Architecture Decisions (ADRs)](docs/decisions/README.md) — Key design decisions (`0001`–`0006`)
- [Development, Testing & Documentation Process](docs/development-process.md) — Testing standards, invariant matrix, coverage, regression policy, doc sync, approval process, and review gates (§7)
- [Reviews](docs/reviews/) — Design and implementation reviews with findings, resolutions, and verification passes

## Architectural & Security Guardrails
- **Single-user, LAN-first (no third-party RSS protocols)**: No multi-tenant complexity, no public internet/VPN exposure, and Fever / Google Reader API compatibility is a **permanent non-goal** ([ADR 0002](docs/decisions/0002-native-sync-api-and-detached-client-model.md)).
- **LAN authentication & browser hardening**: Require one-time device pairing → revocable per-device bearer tokens, strict `Host` header allow-list, strict CORS allow-list, and JSON-only mutation endpoints ([ADR 0006](docs/decisions/0006-lan-security-ssrf-and-content-isolation.md)).
- **SSRF prevention on ALL server fetchers**: Every outbound HTTP fetch (feed polling, full-text extraction, mini-research) must use the shared `SafeHttpClient` with DNS resolution checks rejecting private/loopback/link-local/CGNAT/Docker IPs and non-HTTP(S) schemes on every hop plus IP pinning ([ADR 0006](docs/decisions/0006-lan-security-ssrf-and-content-isolation.md)).
- **Untrusted content & parser isolation**: Sanitize feed HTML with DOMPurify under strict SPA CSP, render LLM outputs safely, delimit untrusted text in prompts, and run HTML/PDF extraction inside a resource-bounded isolated subprocess with strict depth/size/page/timeout caps ([ADR 0006](docs/decisions/0006-lan-security-ssrf-and-content-isolation.md)).
- **Reliable detached client origin (metadata-only cache)**: Phase 1 Ubuntu laptop client runs from a fixed `localhost` secure origin (React + TS + Dexie.js + Service Worker + `navigator.storage.persist()`); clients cache curated records/metadata only and open external URLs directly via the internet ([ADR 0005](docs/decisions/0005-phase1-client-origin-and-offline-shell.md)).
- **Host-native LLM boundary**: Communicate with `Ollama` or `llama.cpp` via the internal `LLMAdapter` over the Docker bridge / firewall-restricted host port (never unrestricted `0.0.0.0`, never GPU passthrough inside containers); keep global LLM concurrency = `1` ([ADR 0001](docs/decisions/0001-tech-stack-and-container-topology.md), [ADR 0006](docs/decisions/0006-lan-security-ssrf-and-content-isolation.md)).
- **Non-blocking async pipeline**: Ingestion and UI never block on LLM inference or mini-research; raw items appear immediately in `pending` state.
- **Gap-free sync & idempotent mutations**: Downstream sync uses an append-only `sync_changes` log with serialized commit-ordered watermark + `instance_epoch` restore detection; upstream mutations carry `client_mutation_id` + clamped timestamp with per-field LWW and bounded bulk actions ([ADR 0004](docs/decisions/0004-sync-watermark-aggregates-and-restore-epoch.md)).
- **Permanent historical log + non-destructive expiration**: Aging out old unread items (anchored on `fetched_at`) transitions them to `expired` rather than deleting history; starred items never expire.
- **Untrusted input into libraries**: Never pass fetched bytes/strings to library calls that also accept a path or URL (`feedparser.parse`, `lxml`/`etree.parse`, `trafilatura.fetch_url`, …); wrap them in `io.BytesIO` (or decode explicitly) and add a static guard test (`I-01`).

## Development, Testing & Documentation Rules
Follow [docs/development-process.md](docs/development-process.md) on every change. Key non-negotiables:

- **Spec-first**: Align with `docs/` before coding; update specs first when requirements or contracts change.
- **Tests are mandatory & integral**: Write unit, real-PostgreSQL (`pgvector`) integration, boundary/contract, and E2E tests alongside every feature—never defer tests.
  - Target **>= 80%** overall coverage and **>= 90%** on critical core logic (sync watermark/reconciliation, expiration, job queue/leases, deduplication, SSRF `SafeHttpClient`, mini-research guardrails, LLM schema validation). Focus on invariants, not vanity mocks.
  - **Regression tests required**: Every bug fix must add a regression test that fails before the fix and passes after it.
  - Use deterministic recorded fixtures for HTTP/RSS/PDF/`Ollama`/`llama.cpp` boundaries in the default suite; run `pytest -m live_llm` opt-in for model/prompt evaluation.
- **Keep docs synchronized**: Update affected docs (`docs/design/*`, `docs/data-model.md`, `docs/architecture.md`, `docs/README.md`) in the **same commit** as code changes; add an ADR in `docs/decisions/` for significant architectural or trade-off decisions.
- **Verify locally before finishing**: Run `make check` (linters, type checks, unit/integration tests, coverage gates) before reporting any task complete.
- **Prove regression tests**: A fix is not done until its regression test is shown to **fail with the fix reverted** (separately for **each** fixed path) and the fixed lines appear covered in the coverage report; record that evidence in the review resolution or commit message.
- **Merging milestones**: One PR per milestone branch; merge with a **merge commit** (never squash or rebase, because review docs cite commit hashes), then delete the branch ([development-process.md §1](docs/development-process.md#1-core-development-workflow)).

## Roles & Review Gates
Details, templates, and severity scale: [development-process.md §7](docs/development-process.md#7-review-gates); shared checklist: [docs/review-checklist.md](docs/review-checklist.md).
- **Roles**: **Antigravity** is the designer/implementer (skills: `.agents/skills/prepare-for-review`, `.agents/skills/address-review-findings`). **Claude Code** is the reviewer (skills: `.claude/skills/design-review`, `implementation-review`, `verify-review-resolution`). The reviewer does not fix findings unless the project owner asks; the implementer does not mark its own fixes `Verified`.
- **Two recorded reviews per milestone** in `docs/reviews/`: a **design review** before the design is `Approved`, and an **implementation review** before the milestone is `Implementation Approved`.
- **Blockers and Majors must be resolved** (or explicitly accepted by the project owner as residual risks) before approval.
- **Separate verification pass**: After the implementer marks a review `Addressed`, the reviewer re-checks every finding in a fresh session: fix present, regression test exercises the fix and fails without it, docs synced, `make check` green. Only then is it `Verified`.
- **Only the project owner approves**: Agents may set review status to `Addressed`/`Verified` but never set a design or implementation to `Approved`/`Implementation Approved` unless the owner says so.
- **Sequencing**: Do not write code for milestone N+1 until milestone N is `Implementation Approved` (designing N+1 in parallel is fine).

## Coding & Tooling Standards
- **Backend**: Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy + Alembic, PostgreSQL 16+ (`pgvector`), Docker Compose; `ruff`, `mypy`/`pyright`, `pytest` + `pytest-cov` + `hypothesis`.
- **Frontend**: TypeScript (`strict: true`), React, Vite, Dexie.js, DOMPurify; `vitest` + `fake-indexeddb`, Playwright for offline-reload and critical E2E flows.

## Evolving Rules & Project Skills (Feedback Loop)
- **Persist process feedback**: When the user corrects a workflow, establishes a new convention, or identifies a recurring pitfall, update `AGENTS.md` (concise rule/pointer), [docs/development-process.md](docs/development-process.md) (detailed standard), [docs/review-checklist.md](docs/review-checklist.md) (new bug class), or a project skill (implementer: `.agents/skills/<name>/SKILL.md`; reviewer: `.claude/skills/<name>/SKILL.md`) so the improvement carries forward automatically.
- **Keep `AGENTS.md` lean**: Keep always-on rules crisp and well within the 24 KB limit; place detailed runbooks in `docs/` or `.agents/skills/`.
