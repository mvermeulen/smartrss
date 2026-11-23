# 0002: Native Sync API & Detached Client Architecture

- **Status**: Accepted
- **Date**: 2026-11-22 (Updated 2026-11-22 after design review; accepted 2026-11-23)

## Context
SmartRSS clients must support an Ubuntu laptop that leaves the LAN (Phase 1) and Android (Phase 2), operate primarily on a home LAN without VPN/public exposure, and detach cleanly from the home server while retaining general internet access to open external RSS links. Clients also need to display rich LLM metadata (TL;DR, takeaways, depth, 5-tier relevance + rationale, mini-research context, clusters) and queue offline mutations including explicit feedback, bulk mark-read, and content-related feed config edits (e.g., unsubscribing/muting).

## Decision
1. **Native Sync API only — Fever / Google Reader API is a permanent non-goal**: Define a versioned `/api/v1/sync/*` protocol (`bootstrap`, `pull`, `push`) shaped around SmartRSS aggregates (see [ADR 0004](0004-sync-watermark-aggregates-and-restore-epoch.md)). Third-party RSS protocols are permanently out of scope so the data and sync models stay clean.
2. **Metadata-only client cache**: Clients cache curated server records, LLM metadata, clusters, feeds, categories, and active settings locally, and do **not** mirror external linked web pages or PDFs—opening links hands off directly to the browser over the client's internet connection.
3. **Phase 1 & Phase 2 client delivery**:
   - **Phase 1 (Ubuntu laptop)**: Serve the React/TypeScript/Dexie SPA from a fixed `localhost` origin (local static runner or lightweight Tauri shell) so Service Workers and `navigator.storage.persist()` operate in a guaranteed secure context offline (see [ADR 0005](0005-phase1-client-origin-and-offline-shell.md)).
   - **Phase 2 (Android)**: Reuse the exact same versioned Sync API via a thin Android client (Capacitor wrapper or native Kotlin). Explicitly account for Android 9+ cleartext restrictions (`networkSecurityConfig` scoped to LAN), Capacitor mixed-content rules (using native HTTP plugin / custom scheme rather than `https://` WebView calling `http://` LAN API), and native `WorkManager` constraints for automatic home-Wi-Fi background sync.

## Consequences
- **Positive**: First-class support for SmartRSS's metadata, clusters, feedback, and offline config mutations; lightweight client storage; reliable offline origin on Ubuntu laptops without requiring LAN TLS/CA setup.
- **Trade-offs**: Existing third-party RSS apps (NetNewsWire, Reeder, etc.) cannot connect; Android plain-HTTP LAN access and background Wi-Fi sync require explicit platform configuration in Phase 2 (which favors either a Capacitor native-HTTP plugin or a lightweight native Kotlin client).
