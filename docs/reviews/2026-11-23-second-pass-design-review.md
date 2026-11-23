# Design Review — Second Pass (Post-Resolution Verification)

- **Date**: 2026-11-23
- **Scope**: `docs/requirements.md`, `docs/architecture.md`, `docs/data-model.md`, `docs/development-process.md`, `AGENTS.md`, ADRs `0001`–`0006`, `docs/design/README.md` (as of commit `633f877`)
- **Reviewer**: Claude Code (at the project owner's request)
- **Purpose**: Verify that the resolutions recorded in [the initial design review](2026-11-22-initial-design-review.md) and [the high-level design review](2026-11-22-high-level-design-review.md) actually hold up in the current docs, and surface anything the fixes themselves introduced, before the owner signs off on `Approved`/`Accepted` status.
- **Status**: Complete. No blockers. A short list of Major/Minor residual items below; none should hold up approval of the documents as a package, but two are worth an explicit owner call before `Accepted`.

## Review outcome

Every Blocker (`B-01`–`B-05` / `R-01`–`R-05`) and Major finding from both prior reviews is substantively addressed in the current docs — I checked each resolution claim against the actual text in `requirements.md`, `architecture.md`, `data-model.md`, and ADRs `0001`–`0006` rather than taking the resolution trackers at their word, and the cross-references line up. The docs are also internally consistent with each other (diagram, status table, open-questions list, and ADR index all agree).

What's left is smaller: two residual design decisions worth the owner's explicit attention before `Accepted` (not blockers — defensible either way, but the docs should say which way was chosen), and five minor clarifications that are fine to fold into the relevant milestone component design (`1a`/`1b`/`1d`/`1e`) rather than re-opening the high-level docs.

---

## 1. Verified: Prior Blockers Hold Up

Spot-checked against current text (not just the resolution trackers):

| ID | Claim | Verified against |
| :--- | :--- | :--- |
| R-01/B-01 | Commit-ordered watermark replaces `nextval()` | `data-model.md` §4.2 (`sync_state` `FOR UPDATE` + `sync_changes`), [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md) — logic is sound: holding the row lock until commit genuinely prevents a lower `seq` from becoming visible after a higher one. |
| R-02/B-02 | Sync surface + tombstones defined | `data-model.md` §5.1 table covers all six entity types including `delete` semantics. |
| R-03/B-03 | Phase 1 origin fixed | [ADR 0005](../decisions/0005-phase1-client-origin-and-offline-shell.md), `requirements.md` §3 — `localhost` origin decision is consistent everywhere it's referenced. |
| R-04/B-04 | LAN auth added | `NFR-SEC-01`, [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md) §1 — pairing + bearer token + Host/CORS allow-lists all specified (see §3 below for one residual gap). |
| R-05/B-05 | SSRF guardrails on all fetchers | `NFR-SEC-02`, [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md) §2 — `SafeHttpClient` is explicitly shared across polling, extraction, and mini-research, not just one of them. |

The Major findings (`R-06`–`R-22` / `M-01`–`M-14`) check out the same way: per-field timestamps actually exist on `items` and `feeds`, the item/feed state diagrams actually cover the transitions called out as missing, the job queue actually has `dedupe_key`/`lease_expires_at`/`heartbeat_at`, embeddings actually moved to a model-versioned table, etc. I won't re-list all 14 — the resolution trackers in both prior review docs are accurate.

---

## 2. Major — Residual Items Worth an Explicit Owner Decision

### S-01 — Bearer tokens travel in cleartext over the LAN hop, re-opening part of the threat model ADR 0006 was written to close

- **Docs**: `NFR-SEC-01`, [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md) §1, [ADR 0002](../decisions/0002-native-sync-api-and-detached-client-model.md)
- **Problem**: ADR 0006's own threat model (context, point 1) is "any LAN/IoT device... could hit the plain-HTTP LAN API." The fix adds a per-device bearer token — but the client-to-API hop is still plain HTTP (ADR 0002 deliberately rejected LAN TLS to avoid cert friction; ADR 0005 only makes the *client's own origin* secure, which is a different hop). A bearer token sent over plain HTTP on shared Wi-Fi is sniffable by anything that can observe LAN traffic: a rogue/compromised device on the same SSID, or an attacker who has captured the WPA handshake. Once sniffed, the token grants full read/write access indefinitely until revoked — which is close to the original "any LAN device can read/write" problem the auth requirement was meant to solve, just requiring one extra passive step.
- **Why it wasn't caught as a blocker the first time**: R-04/B-04 were specifically about CSRF and DNS rebinding (a hostile *webpage*, not a hostile *device*), which the bearer token does fix. Passive sniffing of the token itself is a distinct vector that neither prior review's recommendation explicitly covered.
- **Recommendation**: This is a legitimate trade-off, not a missing feature — pick one explicitly and record it:
  1. Accept the residual risk given the stated threat model is "trusted home Wi-Fi," and say so in ADR 0006's Consequences (e.g., "assumes WPA2/3 with no hostile co-tenants on the LAN; token sniffing by an on-path LAN attacker is an accepted residual risk").
  2. Add LAN TLS via a local CA (ADR 0002's rejected option (b)) — cert friction was the reason it was rejected for the *client origin* problem (R-03), but that problem is now solved by `localhost`; the remaining question is only whether it's worth it just to encrypt the LAN hop's token traffic.
  3. Short-rotation tokens (e.g., re-issue on each `bootstrap`) to shrink the exposure window — weaker than TLS but cheaper than running a CA.
- **Suggested default if no strong preference**: Option 1 (accept and document) — matches the project's existing risk tolerance (ADR 0002 already traded LAN TLS away once for similar friction reasons), but it should be a stated decision, not a silent gap.

### S-02 — The sync-watermark row lock must not be held across the LLM call, or `enrich_item` throughput collapses to `concurrency = 1` for *all* writers, not just LLM calls

- **Docs**: [ADR 0004](../decisions/0004-sync-watermark-aggregates-and-restore-epoch.md) §1, `architecture.md` §3.3 step 5
- **Problem**: ADR 0004 says the `sync_state` row lock is acquired "at the end of any write transaction that mutates synced state... before committing," which is the right design — *if* "the write transaction" is a short transaction that wraps only the final persist step. But `architecture.md` §3.3 describes `enrich_item` as a single stage that "builds a prompt... calls `LLMAdapter`... validates... updates `search_vector`, and emits an aggregate `item` upsert in `sync_changes`" without explicitly saying the ~10–25 second LLM call happens *outside* the DB transaction that takes the lock. If an implementer reads this literally and wraps the whole stage (including the `LLMAdapter` call) in one transaction, every enrichment would serialize all other synced writes (item ingestion, read/star mutations, feed config changes) behind the LLM call's latency — which would violate `NFR-SYNC-02`'s 2-minute visibility and 5-second outbox-flush targets under load, not because the watermark design is wrong, but because of where the transaction boundary is drawn.
- **Recommendation**: Add one sentence to ADR 0004 or `architecture.md` §3.3 making the transaction boundary explicit: the `LLMAdapter` call (and any other slow I/O) happens *before* opening the short, final write transaction that takes `sync_state FOR UPDATE`, writes the result, and commits. This is almost certainly the intended design given the "sub-millisecond" framing in the ADR's Consequences — it just isn't stated precisely enough to stop a future implementation from getting it backwards. Worth a one-line addition now since it's cheap to fix and expensive to debug later; otherwise fine to leave as an explicit note picked up in the `1b`/`1d` component designs.

---

## 3. Minor — Fold into Milestone Component Designs (Not Blocking)

### S-03 — Canonical embedding input has two slightly different descriptions
`data-model.md` §4.7 defines it precisely as `title + first 1,500 chars of extracted_text (or raw_summary)`. `architecture.md` §3.3 (step 3) and §4.1 (step 4) both describe the same stage as computing `title + lead_excerpt`, and `lead_excerpt` isn't a column anywhere in the schema. These are presumably meant to be the same value. **Resolution for `1e`**: pick one term and use it in both docs, or define `lead_excerpt` explicitly as the derived value data-model.md already specifies.

### S-04 — `feeds.config_version` scope is ambiguous
`data-model.md` §4.4 describes `config_version` as "optimistic concurrency token for free-text fields" (i.e., `curation_instructions`), while `field_updated_at` (JSONB) handles per-field LWW for the offline-mutable structured fields (`status`, `category_id`, `research_policy.fetch_pdfs`, etc.) on the *same row*. It's not stated whether an offline LWW update to, say, `feeds.status` also bumps `config_version`. If it does, a legitimate online edit to `curation_instructions` could spuriously 409 against an unrelated concurrent field change made from another device. **Resolution for `1b`**: state explicitly that `config_version` increments only on free-text-field writes and is independent of `field_updated_at` LWW fields.

### S-05 — "Muted" feed item-state transition isn't in the state diagram
`data-model.md` §3.2 says muted-feed items are "auto-marked `read` (or hidden from active unread inbox)" — two different mechanisms presented as interchangeable, but they have different sync/UI consequences (one mutates `state`/`read_state_updated_at` and emits a sync change; the other is a pure client-side filter with no server write). The item state diagram (§3.1) doesn't list a mute-triggered transition either way. **Resolution for `1a`/`1e`**: pick one mechanism and add the transition to §3.1 if it's the state-mutating option.

### S-06 — "Optional LLM confirmation on borderline [cluster] matches" (`FR-LLM-03`) has no home in the pipeline
Clustering (`cluster_item`) runs *before* enrichment specifically to avoid redundant LLM calls (R-11's fix), which is the right call — but `FR-LLM-03` also promises optional LLM confirmation for borderline embedding-similarity matches, and neither `architecture.md` §3.3 step 3 nor the capacity budget in §5.1 accounts for where that confirmation call happens or its token/latency cost. **Resolution for `1e`**: either specify it as a small follow-up call inside `cluster_item` (and add it to the capacity budget) or drop "optional LLM confirmation" from `FR-LLM-03` if borderline cases will just use the threshold as-is for Phase 1.

### S-07 — `bootstrap` response size isn't bounded
`GET /api/v1/sync/bootstrap` returns "all unread + starred + last 14 days read items" with no stated page size or streaming behavior. At steady state (~200 items/day, backfill capped at `N=10`/feed) this stays small, but a multi-day LLM or worker outage, or a `resync_required` after 90+ days detached, could make the unread set large enough that an unpaginated single-response bootstrap becomes the slow/fragile part of recovery — precisely the scenario `instance_epoch` recovery is meant to handle gracefully. **Resolution for `1b`**: state a page size / cursor for `bootstrap` (or an explicit upper bound with graceful degradation) rather than assuming it's always small.

---

## 4. Things That Are Working Well (Since Last Review)

- The resolution trackers in both prior review docs are **accurate** — every spot-check against the actual doc text matched the claimed resolution. That's not a given after a large simultaneous edit across five documents and three new ADRs; it reflects real discipline in keeping the docs-in-the-same-change rule from `development-process.md` §4.2.
- The aggregate-item sync design (R-02/B-02's fix) is simpler to reason about than the original per-table sync surface would have been, and the tombstone rules are now unambiguous.
- Severity separation of `status` vs. `health_state` on feeds, and `is_starred` vs. `state` on items, cleanly resolved two different "one timestamp, two meanings" bugs from the first review with the same pattern — good sign of a consistent design instinct, not just patching each symptom separately.
- The milestone split (`1a`–`1g`) is genuinely vertical — each slice has its own exit criteria and none of them silently depend on a later slice's output to be testable.

---

## 5. Approval Recommendation

| Area | Recommendation | Notes |
| :--- | :--- | :--- |
| `docs/requirements.md` | **Ready to approve** | No open findings against this doc specifically. |
| `docs/data-model.md` | **Ready to approve** | S-03/S-04/S-05 are clarifications, not defects; fine to resolve in `1a`/`1b`/`1e` component designs without re-opening this doc. |
| `docs/architecture.md` | **Ready to approve, with S-02 addressed first** | S-02 is a one-sentence fix (transaction-boundary clarification) — cheap enough to land before `Accepted` rather than deferring. |
| `docs/development-process.md` | **Ready to approve** | No open findings. |
| ADR 0001 (tech stack) | **Ready to accept** | No open findings. |
| ADR 0002 (sync API & detached client) | **Ready to accept** | No open findings beyond its interaction with S-01. |
| ADR 0003 (job queue) | **Ready to accept** | No open findings. |
| ADR 0004 (sync watermark) | **Ready to accept, with S-02's clarification folded in** | Design is correct; the ADR text just needs the transaction-boundary sentence. |
| ADR 0005 (Phase 1 client origin) | **Ready to accept** | No open findings. |
| ADR 0006 (LAN security/SSRF) | **Needs one owner decision (S-01) before `Accepted`** | Not a design flaw — a genuine trade-off (accept residual sniffing risk vs. add LAN TLS vs. short-rotation tokens) that should be a recorded decision, not a silent assumption, given this ADR exists specifically to close LAN attack surface. |
| `AGENTS.md` / `docs/README.md` / `docs/decisions/README.md` | **Ready to approve** | Cross-references and status table are internally consistent. |

**Bottom line**: the package is in materially better shape than at the first review and I did not find anything that should block moving `requirements.md`, `data-model.md`, `development-process.md`, and ADRs `0001`/`0002`/`0003`/`0005` to `Approved`/`Accepted` now. `architecture.md` and ADR `0004` need one clarifying sentence each (S-02) — trivial to land in the same sign-off commit. ADR `0006` needs an explicit owner call on S-01 (token-sniffing risk acceptance vs. mitigation) before it's marked `Accepted`, since that ADR's whole purpose is closing the LAN attack surface and this is a gap in that closure, not a documentation nit. None of S-03–S-07 should hold up approval — they're correctly sized for the milestone component designs that come next.

## 6. Suggested Next Step

1. Owner decides S-01 (token transport risk) and S-02 gets a one-line fix to ADR 0004 / `architecture.md` §3.3 — both cheap, do them together.
2. Move `requirements.md`, `architecture.md`, `data-model.md`, `development-process.md` to `Approved` and ADRs `0001`–`0006` to `Accepted` in one commit, per the mechanism defined in `development-process.md` §4.1.
3. Begin the `docs/design/1a-backend-ingestion-and-security.md` component design, carrying S-03–S-07 forward as explicit items to resolve inside the relevant milestone specs (`1a`, `1b`, `1e` as noted above) rather than leaving them implicit.
