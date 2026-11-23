# 0001: Backend Tech Stack & Container Topology

- **Status**: Proposed *(pending owner approval)*
- **Date**: 2026-11-22 (Updated 2026-11-22 after design review)

## Context
SmartRSS needs a self-hosted home-LAN backend running on Ubuntu Linux (packaged cleanly as Docker containers for a diverse homelab environment) that handles ~25 feeds (~200 items/day), full-text HTML extraction, PDF research-paper parsing, local LLM inference on an AMD Ryzen AI Max+ 395 (128 GB unified memory) host via Ollama or `llama.cpp`, full-text search, and cross-feed story clustering.

## Decision
1. **Backend language & framework**: Python 3.12+ with FastAPI for the API server and shared Python modules for background workers (`feedparser`, `httpx`, `trafilatura`, `pymupdf`, Pydantic v2, SQLAlchemy + Alembic).
2. **Database**: Containerized PostgreSQL 16+ with the `pgvector` extension for relational data, built-in full-text search (`tsvector`), vector similarity clustering, and durable background job queuing.
3. **LLM runtime separation & adapter boundary**:
   - Keep `Ollama` or `llama.cpp` running natively on the Ubuntu host (or another dedicated LAN host) rather than inside the SmartRSS Docker Compose stack.
   - Bind the host-native LLM runtime to the Docker bridge gateway IP (or restrict via host firewall to the Docker subnet—never expose an unauthenticated LLM on unrestricted `0.0.0.0`; see [ADR 0006](0006-lan-security-ssrf-and-content-isolation.md)).
   - Abstract `Ollama` and `llama.cpp` behind an internal `LLMAdapter` interface with runtime-specific capability flags for structured JSON-schema output (`format` vs `response_format`/GBNF), reasoning-token handling (e.g., `<think>` blocks), and embedding normalization/batching rather than relying on a leaky "plain OpenAI-compatible" assumption.
4. **Frontend baseline**: TypeScript (`strict: true`), React, Dexie.js (IndexedDB), and DOMPurify (see [ADR 0005](0005-phase1-client-origin-and-offline-shell.md)).

## Consequences
- **Positive**: Best-in-class Python libraries for HTML/PDF extraction and LLM structured outputs; single database container avoids adding Redis or a separate vector DB; host-native LLM avoids fragile ROCm/Vulkan GPU passthrough inside Docker on Ryzen unified-memory hardware; explicit `LLMAdapter` avoids runtime incompatibilities between Ollama and `llama.cpp`.
- **Trade-offs**: Requires configuring Docker bridge binding or a host firewall rule for the local LLM port (verified at startup via System Health check); maintaining two small runtime capability profiles in `LLMAdapter` with contract tests for each.
