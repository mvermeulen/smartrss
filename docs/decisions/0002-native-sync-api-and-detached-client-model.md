# 0002: Native Sync API & Detached Client Architecture

- **Status**: Proposed
- **Date**: 2026-11-22

## Context
SmartRSS clients must support Ubuntu desktop (Phase 1) and Android (Phase 2), operate primarily on a home LAN without VPN/public exposure, and detach cleanly from the home server while retaining general internet access to open external RSS links. Clients also need to display rich LLM metadata (TL;DR, takeaways, depth, relevance + rationale, mini-research context, clusters) and queue offline mutations including explicit feedback and content-related feed config edits (e.g., unsubscribing/muting).

## Decision
1. **Native Sync API only (no Fever / Google Reader API)**: Define a clean, versioned `/api/v1/sync/pull` + `/api/v1/sync/push` protocol using monotonic server sequence cursors (`sync_seq`) and idempotent client mutation UUIDs (`client_mutation_id`).
2. **Metadata-only client cache**: Clients cache curated items, LLM metadata, clusters, feeds, and categories locally, and do **not** mirror external linked web pages or PDFs—opening links hands off directly to the browser over the client's internet connection.
3. **Phase 1 Web UI + Phase 2 Android-friendly path**: Build Phase 1 as a local-first TypeScript SPA backed by IndexedDB + an offline mutation outbox. Keep the Sync API cleanly decoupled so Phase 2 on Android can wrap the UI in a thin native shell (e.g., Capacitor) or build a native Kotlin client—working over plain LAN HTTP without self-signed HTTPS PWA certificate friction and supporting automatic background Wi-Fi sync.

## Consequences
- **Positive**: First-class support for SmartRSS's unique metadata, feedback, and offline config mutations without shoehorning them into legacy RSS protocols; lightweight client storage; avoids LAN HTTPS/CA headaches on Android.
- **Trade-offs**: Existing third-party RSS apps (NetNewsWire, Reeder, etc.) cannot connect out of the box (acceptable per Non-Goals; a compatibility adapter could always be added later if desired).
