# Component Designs

Detailed design documents for individual subsystems and features live in this directory.

## Planned Specs
- `ingestion-and-mini-research.md` — Polite RSS/Atom polling, feed health, full-text extraction, primary-source/PDF mini-research heuristics & guardrails.
- `llm-pipeline-and-feedback.md` — Local LLM adapter (`Ollama`/`llama.cpp`), structured output schemas, prompt versioning, interest profile & explicit feedback loop, story clustering.
- `sync-protocol-and-offline-client.md` — `/api/v1/sync/pull` & `/api/v1/sync/push` contracts, `sync_seq` pagination, idempotent mutation outbox, conflict resolution, and Phase 2 Android considerations.
- `client-ui-and-ux.md` — Classic RSS layout enhanced with smart metadata, cluster views, feedback controls, prompt/feed configuration UI, and detached-mode indicators.
