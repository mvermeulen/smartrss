# 0001: Backend Tech Stack & Container Topology

- **Status**: Proposed
- **Date**: 2026-11-22

## Context
SmartRSS needs a self-hosted home-LAN backend running on Ubuntu Linux (packaged cleanly as Docker containers for a diverse homelab environment) that handles ~25 feeds (~200 items/day), full-text HTML extraction, PDF research-paper parsing, local LLM inference on an AMD Ryzen AI Max+ 395 (128 GB unified memory) host via Ollama or `llama.cpp`, full-text search, and cross-feed story clustering.

## Decision
1. **Backend language & framework**: Python 3.12+ with FastAPI for the API server and shared Python modules for background workers (`feedparser`, `httpx`, `trafilatura`, `pymupdf`, Pydantic v2).
2. **Database**: Containerized PostgreSQL 16+ with the `pgvector` extension for relational data, built-in full-text search (`tsvector`), vector similarity clustering, and durable background job queuing.
3. **LLM runtime separation**: Keep `Ollama` or `llama.cpp` running natively on the Ubuntu host (or another LAN host) rather than inside the SmartRSS Docker Compose stack. The SmartRSS worker communicates with it over an OpenAI-compatible HTTP endpoint (`host.docker.internal` or configurable LAN URL).

## Consequences
- **Positive**: Best-in-class Python libraries for HTML/PDF extraction and LLM structured outputs; single database container avoids adding Redis or a separate vector DB; host-native LLM avoids fragile ROCm/Vulkan GPU passthrough inside Docker on Ryzen unified-memory hardware.
- **Trade-offs**: Requires configuring `host.docker.internal` / host firewall allowance for the local LLM port; PostgreSQL uses slightly more RAM than SQLite (negligible on a 128 GB machine).
