/**
 * Tests for 1c-T07 (MiniSearch detached search, queryParser, XSS-safe snippets, 5,000-item benchmark)
 * and 1c-T08 (P-17 display-only connected search & U-12 target_absent archive mutation rollback).
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SmartRSSApiClient } from "../../api/client";
import { SmartRSSDB } from "../../db/schema";
import { buildSafeSnippets, LocalSearchIndex } from "../../search/localSearch";
import { NegationOnlyQueryError, parseSearchQuery } from "../../search/queryParser";
import type { LockHandle } from "../../sync/lock";
import { enqueueItemStarred, flushOutbox } from "../../sync/outbox";
import { applyOutboxToSearchItem } from "../../sync/reconciliation";
import type { SyncPushResponse } from "../../types/sync";
import { makeLocalItem, makeSearchItem } from "../fixtures/syncFactories";

const NOOP_LOCK: LockHandle = {
  mode: "web_locks",
  ownerTabId: "tab-search",
  fencingToken: null,
  async assertAndRenewInTx() {},
  async renewBeforeIo() {},
};

describe("1c-T07 queryParser & MiniSearch detached local search", () => {
  it("parses phrases, negations, blanks, and rejects negation-only queries matching 1b 422", () => {
    const q1 = parseSearchQuery(`pgvector "sync watermark" -redis -"old cache"`);
    expect(q1.positiveTerms).toEqual(["pgvector"]);
    expect(q1.positivePhrases).toEqual(["sync watermark"]);
    expect(q1.excludedTerms).toEqual(["redis"]);
    expect(q1.excludedPhrases).toEqual(["old cache"]);
    expect(q1.isEmpty).toBe(false);

    expect(parseSearchQuery("   ").isEmpty).toBe(true);
    expect(parseSearchQuery("!!!").isEmpty).toBe(true);
    expect(() => parseSearchQuery("-foo")).toThrow(NegationOnlyQueryError);
    expect(() => parseSearchQuery(`-"hello world"`)).toThrow(NegationOnlyQueryError);
  });

  it("ranks title > author > lead_excerpt, applies phrases, negations, facets & incremental updates", () => {
    const idx = new LocalSearchIndex();
    const titleHit = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333301",
      feed_id: "f1",
      title: "pgvector HNSW tuning guide",
      lead_excerpt: "General database article.",
      state: "unread",
      is_starred: true,
    });
    const bodyHit = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333302",
      feed_id: "f2",
      title: "Weekly Systems Digest",
      lead_excerpt: "Includes a brief note about pgvector and redis caching.",
      state: "read",
      is_starred: false,
    });
    const xssItem = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333303",
      feed_id: "f1",
      title: "Escaping <script>alert(1)</script> safely",
      lead_excerpt: "Never render <script>alert(1)</script> as raw HTML in snippets.",
      state: "unread",
      is_starred: false,
    });

    idx.rebuild([bodyHit, titleHit, xssItem]);
    expect(idx.size).toBe(3);

    // Title hit outranks lead_excerpt hit
    const r1 = idx.search({ q: "pgvector" });
    expect(r1.results.map((r) => r.item.id)).toEqual([titleHit.id, bodyHit.id]);

    // Negation excludes bodyHit
    const r2 = idx.search({ q: "pgvector -redis" });
    expect(r2.results.map((r) => r.item.id)).toEqual([titleHit.id]);

    // Exact phrase match
    const r3 = idx.search({ q: `"HNSW tuning"` });
    expect(r3.results.map((r) => r.item.id)).toEqual([titleHit.id]);

    // Facet filter by category + starred
    const feedCatMap = new Map<string, string | null>([
      ["f1", "cat-a"],
      ["f2", "cat-b"],
    ]);
    const r4 = idx.search({
      q: "pgvector",
      category_id: "cat-a",
      feedCategoryMap: feedCatMap,
      is_starred: true,
    });
    expect(r4.total).toBe(1);
    expect(r4.results[0]!.item.id).toBe(titleHit.id);

    // XSS-safe structured snippets preserve literal <script> as inert text
    const rXss = idx.search({ q: "script" });
    expect(rXss.total).toBe(1);
    const frags = rXss.results[0]!.snippet.flat();
    expect(frags.some((f) => f.highlight && f.text.toLowerCase() === "script")).toBe(true);
    expect(frags.map((f) => f.text).join("")).toContain("<script>alert(1)</script>");

    // Incremental upsert & discard
    idx.upsert({ ...bodyHit, title: "Updated pgvector notes" });
    idx.discard(titleHit.id);
    idx.discard("non-existent-id");
    expect(idx.size).toBe(2);
    expect(idx.search({ q: "pgvector" }).results.map((r) => r.item.id)).toEqual([bodyHit.id]);

    // Blank query -> facet-filtered newest; punctuation-only -> 0; excluded phrase & date/feed/state facets
    expect(idx.search({ q: "", sort: "newest", feed_id: "f2", state: "read" }).total).toBe(1);
    expect(idx.search({ q: "!!!" }).total).toBe(0);
    expect(
      idx.search({
        q: `pgvector -"redis caching"`,
      }).total,
    ).toBe(0);
    expect(
      idx.search({
        q: "",
        date_from: "2027-01-01T00:00:00.000Z",
      }).total,
    ).toBe(0);
    expect(
      idx.search({
        q: "",
        date_to: "2020-01-01T00:00:00.000Z",
      }).total,
    ).toBe(0);

    // Enrichment fields (tldr/takeaways/tags) & missing author/lead_excerpt branches
    const enriched = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333304",
      feed_id: "f1",
      author: null,
      lead_excerpt: null,
      title: "Unrelated headline",
      tags: ["rust", "wasm"],
      enrichment: {
        status: "completed",
        tldr: "Concise webassembly memory overview",
        takeaways: ["Linear memory growth is bounded"],
        depth_type: "deep-dive",
        relevance_tier: 5,
        relevance_rationale: "Matches systems interest",
        research_context: null,
      },
    });
    idx.upsert(enriched);
    const rEnr = idx.search({ q: "webassembly", sort: "relevance" });
    expect(rEnr.total).toBe(1);
    expect(rEnr.results[0]!.item.id).toBe(enriched.id);

    // Date & tie-breaking sort when scores or dates match
    const tieA = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333305",
      feed_id: "f1",
      published_at: "2026-11-25T10:00:00.000Z",
    });
    const tieB = makeLocalItem({
      id: "33333333-3333-4333-8333-333333333306",
      feed_id: "f1",
      published_at: "2026-11-25T10:00:00.000Z",
    });
    idx.upsert(tieA);
    idx.upsert(tieB);
    const newestRes = idx.search({ q: "", sort: "newest", limit: 2, offset: 0 });
    expect(newestRes.results).toHaveLength(2);

    // Multi-window snippet extraction (> 160 chars apart)
    const longText =
      "Intro pgvector note " + "x ".repeat(150) + "second pgvector tuning paragraph at end.";
    const multiFrags = buildSafeSnippets(longText, ["pgvector"]);
    expect(multiFrags.length).toBe(2);
    expect(buildSafeSnippets("", ["foo"])).toEqual([]);
    expect(buildSafeSnippets("no match here", ["zzz"])).toEqual([]);
  });

  it("benchmarks 5,000-item hydration and query latency cleanly (U-20)", () => {
    const items = Array.from({ length: 5_000 }, (_, i) =>
      makeLocalItem({
        id: `33333333-3333-4333-8333-${String(i).padStart(12, "0")}`,
        title: i === 2500 ? "Quantum pgvector benchmark needle" : `Engineering update #${i}`,
        lead_excerpt: `Summary paragraph number ${i} discussing distributed systems and local-first sync.`,
        published_at: new Date(Date.parse("2026-11-01T00:00:00.000Z") + i * 60_000).toISOString(),
      }),
    );

    const idx = new LocalSearchIndex();
    const t0 = performance.now();
    idx.rebuild(items);
    const hydrateMs = performance.now() - t0;

    const t1 = performance.now();
    const res = idx.search({ q: "pgvector needle" });
    const queryMs = performance.now() - t1;

    expect(res.total).toBe(1);
    expect(hydrateMs).toBeLessThan(10_000);
    expect(queryMs).toBeLessThan(1_000);
  });
});

describe("1c-T08 connected archive search P-17 display-only & U-12 target_absent mutation", () => {
  let db: SmartRSSDB;

  beforeEach(async () => {
    db = new SmartRSSDB(`test_t08_${Math.random().toString(36).slice(2)}`);
    await db.open();
  });

  afterEach(async () => {
    await db.delete();
  });

  it("mutating an archive-only search result keeps Dexie items untouched and rolls back cleanly on rejected", async () => {
    const archiveItem = makeSearchItem({
      id: "55555555-5555-4555-8555-555555555555",
      state: "expired",
      is_starred: false,
    });

    // Dexie does NOT contain archiveItem (P-17)
    expect(await db.items.count()).toBe(0);
    expect(await db.items_base.count()).toBe(0);

    // User clicks Star on archive-only search result -> target_absent: true
    const row = await enqueueItemStarred(db, archiveItem.id, true, archiveItem.title);
    expect(row.target_absent).toBe(true);
    expect(await db.items.count()).toBe(0);
    expect(await db.items_base.count()).toBe(0);

    // Search view overlays pending outbox reactively
    const pendingOutbox = await db.outbox.toArray();
    const displayed = applyOutboxToSearchItem(archiveItem, pendingOutbox);
    expect(displayed.is_starred).toBe(true);
    expect(displayed.state).toBe("expired");

    // Server rejects the mutation -> outbox cleared, callback fires, Dexie still untouched
    let callbackOutcome: string | null = null;
    const rejectFetch: typeof fetch = async () =>
      new Response(
        JSON.stringify({
          instance_epoch: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          server_now: "2026-11-25T12:00:00Z",
          results: [
            {
              client_mutation_id: row.client_mutation_id,
              outcome: "rejected",
              reason: "entity_not_found",
              affected_count: null,
              current_entity_type: null,
              current_entity: null,
            },
          ],
        } satisfies SyncPushResponse),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );

    const api = new SmartRSSApiClient("http://localhost:8080", "tok", rejectFetch);
    await flushOutbox(db, api, NOOP_LOCK, {
      onArchiveMutationResolved: (_id, outcome) => {
        callbackOutcome = outcome;
      },
    });

    expect(callbackOutcome).toBe("rejected");
    expect(await db.items.count()).toBe(0);
    expect(await db.outbox.count()).toBe(0);
    // Recomputing overlay with empty outbox reverts search result cleanly!
    expect(applyOutboxToSearchItem(archiveItem, await db.outbox.toArray()).is_starred).toBe(false);
    expect(await db.sync_notifications.count()).toBe(1);
  });
});
