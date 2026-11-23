# 0005: Phase 1 Client Origin, Frontend Stack & Offline Shell

- **Status**: Proposed *(pending owner approval)*
- **Date**: 2026-11-22

## Context
The Phase 1 Ubuntu client can run on a **laptop that leaves the home LAN**. Serving a browser SPA from a plain LAN HTTP origin (`http://192.168.1.x` or `http://smartrss.lan`) fails detached-mode requirements: browsers restrict Service Workers (needed to load the app shell offline) and `navigator.storage.persist()` (needed to protect IndexedDB and the unsynced mutation outbox from storage eviction) to **secure contexts** (`https://` or `http://localhost`), and IndexedDB data is orphaned if the server's LAN IP or hostname ever changes. Meanwhile, managing a self-signed local CA across Ubuntu and Android adds operational friction.

## Decision
1. **Stable `localhost` Secure Origin for Phase 1 (Ubuntu laptop)**:
   - Run the Phase 1 SPA from a fixed local origin on the Ubuntu client machine—either a lightweight local static server (`http://localhost:<fixed-port>`) or a thin **Tauri** desktop shell wrapping the same web bundle.
   - `localhost` (and Tauri's app origin) guarantees a secure context for Service Worker offline shell loading and `navigator.storage.persist()`, isolates client storage from server IP/hostname changes, and opens external RSS/PDF links directly in the user's default web browser.
   - The client connects to the home LAN API (configured by fixed hostname/mDNS or static IP, e.g., `http://smartrss.lan:8080`) using CORS + per-device bearer token (see [ADR 0006](0006-lan-security-ssrf-and-content-isolation.md)).
   - *(Note: The Docker Compose stack still serves the SPA via `web` as a fallback for quick connected-only LAN access or admin use, while the local origin is the primary detached-capable Phase 1 client.)*
2. **Frontend Tech Stack**:
   - **React + TypeScript (`strict: true`) + Vite** for the SPA.
   - **Dexie.js** over IndexedDB for structured local cache, local detached search indexing, and persistent offline mutation outbox.
   - **DOMPurify** with a strict allow-list + SPA Content Security Policy (CSP) for safe rendering of untrusted feed HTML.
   - Structured to remain responsive and **Capacitor-friendly** so Phase 2 Android can either wrap the same UI (using native HTTP plugin + Android `networkSecurityConfig` to avoid `https://` WebView mixed-content blocks on plain LAN HTTP) or share the exact Sync API from a native Kotlin client.
3. **Offline Verification Gate**:
   - Require `navigator.storage.persist()` on startup (surfacing a warning if denied) and enforce a Playwright E2E test that loads the app, disconnects the API, reloads the page, performs offline mutations and detached local search, reconnects, and verifies zero lost mutations.

## Consequences
- **Positive**: Reliable offline app loading and durable IndexedDB/outbox storage on Ubuntu laptops with zero local-CA/TLS certificate hassle; clean separation between client origin and server LAN address; smooth path to Phase 2 Android.
- **Trade-offs**: Requires installing/running a small local client runner (systemd user service / shell launcher or Tauri binary) on the Ubuntu laptop instead of only bookmarking a LAN URL; requires explicit CORS allow-listing on the LAN API for the local client origin.
