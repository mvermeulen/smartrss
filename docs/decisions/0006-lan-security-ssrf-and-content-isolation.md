# 0006: LAN Security, SSRF Prevention & Untrusted Content Isolation

- **Status**: Proposed *(pending owner approval)*
- **Date**: 2026-11-22

## Context
Although SmartRSS is single-user and LAN-only, leaving the API unauthenticated and server fetchers unrestricted creates four real attack vectors:
1. **LAN / Browser Threats**: Any LAN/IoT device—or a malicious webpage opened from an RSS item in the user's own browser—could hit the plain-HTTP LAN API via CSRF or DNS rebinding to read history, unsubscribe feeds, or rewrite LLM prompts.
2. **SSRF via Untrusted Feed URLs**: Feed polling, full-text extraction, and mini-research follow URLs and redirects from untrusted external feeds, which could target router admin panels (`192.168.x.x`), internal Docker services (`db:5432`, `api:8000`), the host LLM (`11434`), or link-local/metadata IPs.
3. **Host-Native LLM Exposure**: Binding Ollama/`llama.cpp` to `0.0.0.0` so containers can reach it exposes an unauthenticated LLM server to the entire LAN.
4. **Hostile Content & Parsers**: Feed HTML can contain XSS; article/PDF text can attempt prompt injection; crafted PDFs/HTML can trigger decompression bombs, runaway CPU/memory, or C-library crashes in PyMuPDF.

## Decision
1. **Per-Device Pairing Authentication & Browser Attack Hardening**:
   - **Device Pairing**: Bootstrap the first device via server CLI (`smartrss pair --name ...`) and subsequent devices via CLI or an already-paired device's UI/QR code. One-time short-lived pairing codes (5-minute TTL, rate-limited) exchange for long-lived, individually revocable **per-device bearer tokens** (stored hashed in PostgreSQL `devices` table).
   - **CSRF & DNS Rebinding Controls**:
     - Enforce a strict `Host` header allow-list (configured LAN hostname/IP + `localhost`).
     - Enforce a strict CORS allow-list (fixed Phase 1 local client origin(s) + LAN web origin).
     - Require `Authorization: Bearer <token>` and `Content-Type: application/json` on all mutation endpoints (rejecting simple cross-origin form posts).
2. **Shared `SafeHttpClient` for ALL Server-Side Fetching (SSRF Guardrails)**:
   - Every outbound fetch—feed polling, full-text extraction, and mini-research—must go through one shared `SafeHttpClient`:
     - Allow only `http` and `https` schemes.
     - Resolve DNS before connecting and reject loopback (`127.0.0.0/8`, `::1`), RFC1918 private ranges (`10/8`, `172.16/12`, `192.168/16`), link-local (`169.254/16`, `fe80::/10`), CGNAT (`100.64/10`), IPv6 ULA (`fc00::/7`), IPv4-mapped IPv6, multicast/unspecified, and Docker bridge subnets unless explicitly allow-listed in server config.
     - Re-validate every redirect hop (max 5 redirects) and **pin the validated resolved IP** for the TCP connection (preserving `Host` / TLS SNI) to prevent DNS rebinding.
     - Enforce connect/read timeouts, per-domain rate limiting, and response byte caps (e.g., 5 MB HTML/XML, 20 MB PDF).
3. **Host-Native LLM Network Restriction**:
   - Do not expose Ollama/`llama.cpp` on unrestricted `0.0.0.0`. Document and support either binding the LLM server strictly to the Docker bridge gateway IP or restricting port access via host firewall (`ufw`/`nftables`) to the Docker compose subnet only. Verify reachability at worker startup and surface status in System Health.
4. **Untrusted Content Rendering, Prompt Delimitation & Parser Isolation**:
   - **Client XSS/CSP**: Sanitize all feed-provided HTML (`raw_summary`) with DOMPurify using a strict allow-list; enforce a strict SPA CSP (`script-src 'self'`, no inline scripts); render LLM-generated fields (`tldr`, `takeaways`, `relevance_rationale`, `research_context`) as plain text or a strictly sanitized Markdown subset.
   - **Prompt Injection Mitigation**: Keep scoring rubrics and system rules in the system prompt; wrap untrusted title/article/PDF text in explicit XML/fence delimiters with instructions treating enclosed text strictly as untrusted data; enforce strict Pydantic schema validation on all LLM outputs.
   - **Parser Resource Isolation**: Execute HTML (`trafilatura`) and PDF (`pymupdf`) extraction inside a dedicated bounded subprocess (or low-privilege extractor container) with hard CPU time, memory (`RLIMIT_AS` / container mem limit), wall-clock timeout, max page count (e.g., 50 pages), and decompressed-size caps; keep `pymupdf` pinned and regularly patched.

## Consequences
- **Positive**: Closes LAN CSRF/DNS-rebinding/unauthorized-access vectors with near-zero user friction (pair once per device); eliminates SSRF into the home network and host LLM; protects both the browser client and worker host against hostile feeds and crafted PDFs.
- **Trade-offs**: Adds a small `devices`/`pairing_codes` table, a one-time pairing step per new device, slightly more complex HTTP transport code (IP pinning), and subprocess overhead for extraction (negligible at ~200 items/day).
