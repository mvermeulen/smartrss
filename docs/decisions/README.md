# Architecture Decision Records (ADRs)

Record significant architectural and design choices here as numbered files (`0001-short-title.md`).

> **Approval Process**: ADRs begin as `Proposed`. Once the project owner explicitly approves them, their status is updated to `Accepted` in git. When an ADR is replaced later, mark it `Superseded by NNNN` rather than deleting it.

## Index
- [0001: Backend Tech Stack & Container Topology](0001-tech-stack-and-container-topology.md) — Proposed
- [0002: Native Sync API & Detached Client Architecture](0002-native-sync-api-and-detached-client-model.md) — Proposed
- [0003: PostgreSQL-Backed Job Queue & Async LLM Pipeline](0003-postgres-backed-job-queue-and-async-llm-pipeline.md) — Proposed
- [0004: Committed Sync Watermark, Aggregate Sync Surface & Restore Epoch](0004-sync-watermark-aggregates-and-restore-epoch.md) — Proposed
- [0005: Phase 1 Client Origin, Frontend Stack & Offline Shell](0005-phase1-client-origin-and-offline-shell.md) — Proposed
- [0006: LAN Security, SSRF Prevention & Untrusted Content Isolation](0006-lan-security-ssrf-and-content-isolation.md) — Proposed

## Template
```markdown
# NNNN: Title

- **Status**: Proposed | Accepted | Superseded
- **Date**: YYYY-MM-DD

## Context
What problem or decision are we facing?

## Decision
What did we decide?

## Consequences
What becomes easier or harder as a result?
```
