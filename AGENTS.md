# SmartRSS — Agent Rules & Project Guide

## Project Status
> **Current Phase: Requirements & Design**
> Do **not** write application code until the requirements and design documents in `docs/` are defined and approved.

## Overview
**SmartRSS** is a self-hosted, distributed RSS reader system. A backend server pre-fetches, persists, and curates feeds using local LLMs to generate display and curation metadata, while classic-style RSS clients sync over the local network, support detached/offline reading, and feed user interactions back to evolve curation over time.

## Documentation Map
Read the relevant design docs before making changes or proposing implementations:

- [Documentation Index](docs/README.md) — Status tracker for all specs
- [Requirements](docs/requirements.md) — Goals, non-goals, user stories, functional & non-functional requirements
- [Architecture](docs/architecture.md) — High-level system architecture, tech stack, and component boundaries
- [Data Model](docs/data-model.md) — Entities, schemas, storage, and data lifecycle
- [Component Designs](docs/design/README.md) — Detailed feature and subsystem design specs
- [Architecture Decisions (ADRs)](docs/decisions/README.md) — Record of key design decisions and trade-offs
- [Development, Testing & Documentation Process](docs/development-process.md) — Testing standards, coverage, regression policy, doc sync rules, and Definition of Done

## Architectural Guardrails
- **Single-user, LAN-first**: No multi-tenant complexity, no third-party RSS protocols (Fever/Google Reader), and no public internet/VPN exposure required in initial scope.
- **Metadata-only detached client cache**: Clients cache curated server records and LLM metadata locally and open external URLs directly via the internet; never mirror external target webpages/media on clients.
- **Host-native LLM boundary**: Communicate with `Ollama` or `llama.cpp` strictly through an OpenAI-compatible HTTP client interface; never require GPU passthrough inside the SmartRSS Docker containers.
- **Non-blocking async enrichment**: Ingestion and UI must never block on LLM inference or mini-research; raw items remain visible and usable while enrichment is pending or if the LLM runtime is unavailable.
- **Idempotent offline sync**: All client mutations must carry a client-generated UUID (`client_mutation_id`) and timestamp for safe retry and deterministic conflict resolution; downstream sync uses monotonic `sync_seq` cursors.
- **Permanent historical log + non-destructive expiration**: Aging out old unread items transitions them to `expired` rather than deleting history; starred/saved items never expire.
- **Safe mini-research guardrails**: Enforce strict link-depth, content-size, timeout, and content-type limits on all primary-source/PDF fetches.

## Development, Testing & Documentation Rules
Follow [docs/development-process.md](docs/development-process.md) on every change. Key non-negotiables:

- **Spec-first**: Check and align with `docs/` before writing code; update specs first when requirements or contracts change.
- **Tests are mandatory & integral**: Write unit, integration, and boundary/contract tests alongside every feature—never defer tests to later.
  - Target **>= 80%** overall coverage and **>= 90%** on critical core logic (sync reconciliation, expiration, job queue, deduplication, mini-research guardrails, LLM schema validation). Focus on behavior and invariants, not vanity mocks.
  - **Regression tests required**: Every bug fix must add a regression test that reproduces the failure before the fix and passes after it.
  - Exercise real PostgreSQL (`pgvector`) behavior in integration tests; use deterministic recorded fixtures for external HTTP/RSS/PDF/LLM boundaries in the default suite.
- **Keep docs synchronized**: Update affected docs (`docs/design/*`, `docs/data-model.md`, `docs/architecture.md`, `docs/README.md`) in the **same commit/change** as code changes; add an ADR in `docs/decisions/` for significant architectural or trade-off decisions.
- **Verify before finishing**: Run linters, type checks, and the relevant test suite before reporting a task complete.

## Coding & Tooling Standards
- **Backend**: Python 3.12+, FastAPI, Pydantic v2, PostgreSQL 16+ (`pgvector`), Alembic migrations, Docker Compose; `ruff`, `mypy`/`pyright`, `pytest` + `pytest-cov`.
- **Frontend**: TypeScript (`strict: true`) local-first SPA with IndexedDB cache and persistent offline outbox; `vitest` + `fake-indexeddb`, Playwright for critical E2E flows. *(UI framework choice finalized in `docs/design/client-ui-and-ux.md`.)*

## Evolving Rules & Project Skills (Feedback Loop)
- **Persist process feedback**: When the user corrects a workflow, establishes a new convention, or identifies a recurring pitfall, update `AGENTS.md` (concise rule/pointer), [docs/development-process.md](docs/development-process.md) (detailed standard), or add a project skill under `.agents/skills/<skill-name>/SKILL.md` so the improvement carries forward automatically.
- **Keep `AGENTS.md` lean**: Keep always-on rules crisp and well within context limits; place bulky runbooks or detailed procedures in `docs/` or `.agents/skills/`.
