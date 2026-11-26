/**
 * Detached local search engine backed by MiniSearch (1c §8.2, 1c-D04, U-20).
 *
 * Hydrates from Dexie `items` and updates incrementally on bootstrap/pull/eviction.
 * Produces structured XSS-safe `SnippetFragmentDTO[][]` matching `GET /api/v1/search`.
 *
 * Note (U-20): Phase 1 MiniSearch uses prefix + exact token matching without English
 * stemming, so inflected forms (e.g. "running" vs "run") may differ slightly from
 * PostgreSQL's `english` FTS dictionary.
 */

import MiniSearch from "minisearch";
import type { LocalItemRecord } from "../db/schema";
import type {
  SearchItemDTO,
  SearchResponse,
  SearchResultDTO,
  SnippetFragmentDTO,
} from "../types/api";
import { parseSearchQuery } from "./queryParser";

interface IndexedDoc {
  id: string;
  title: string;
  tags_text: string;
  tldr: string;
  author: string;
  takeaways_text: string;
  lead_excerpt: string;
}

export interface LocalSearchFilters {
  q?: string | null;
  feed_id?: string | null;
  category_id?: string | null;
  feedCategoryMap?: Map<string, string | null>;
  state?: "unread" | "read" | "expired" | null;
  is_starred?: boolean | null;
  date_from?: string | null;
  date_to?: string | null;
  sort?: "relevance" | "newest";
  limit?: number;
  offset?: number;
}

function toIndexedDoc(item: LocalItemRecord): IndexedDoc {
  return {
    id: item.id,
    title: item.title,
    tags_text: (item.tags ?? []).join(" "),
    tldr: item.enrichment?.tldr ?? "",
    author: item.author ?? "",
    takeaways_text: (item.enrichment?.takeaways ?? []).join(" "),
    lead_excerpt: item.lead_excerpt ?? "",
  };
}

function toSearchItemDTO(item: LocalItemRecord): SearchItemDTO {
  return {
    id: item.id,
    feed_id: item.feed_id,
    cluster_id: item.cluster_id,
    raw_url: item.raw_url,
    canonical_url: item.canonical_url,
    title: item.title,
    author: item.author,
    published_at: item.published_at,
    fetched_at: item.fetched_at,
    lead_excerpt: item.lead_excerpt,
    extraction_status: item.extraction_status,
    state: item.state,
    is_starred: item.is_starred,
    read_at: item.read_at,
    starred_at: item.starred_at,
    expired_at: item.expired_at,
    unexpired_at: item.unexpired_at,
    read_state_updated_at: item.read_state_updated_at,
    starred_updated_at: item.starred_updated_at,
    ingest_sync_seq: item.ingest_sync_seq,
    enrichment: item.enrichment,
    tags: item.tags,
    research_sources: item.research_sources,
    user_feedback: item.user_feedback,
    updated_at: item.updated_at,
  };
}

function escapeRegExp(literal: string): string {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Extract up to 2 context windows (~160 chars) around matched terms/phrases as pure
 * structured `SnippetFragmentDTO[][]` — zero HTML emitted, safe for `<script>` payloads.
 */
export function buildSafeSnippets(
  text: string,
  highlights: readonly string[],
  maxFragments = 2,
  windowChars = 160,
): SnippetFragmentDTO[][] {
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean || highlights.length === 0) {
    return [];
  }

  const ordered = Array.from(new Set(highlights.filter((h) => h.length > 0))).sort(
    (a, b) => b.length - a.length,
  );
  if (ordered.length === 0) {
    return [];
  }

  const pattern = new RegExp(`(${ordered.map(escapeRegExp).join("|")})`, "gi");
  const matches: Array<{ start: number; end: number }> = [];
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(clean)) !== null) {
    matches.push({ start: m.index, end: m.index + m[0].length });
    if (matches.length >= 20) break;
  }

  if (matches.length === 0) {
    return [];
  }

  const windows: Array<{ start: number; end: number }> = [];
  for (const hit of matches) {
    if (windows.length >= maxFragments) break;
    const half = Math.floor(windowChars / 2);
    const start = Math.max(0, hit.start - half);
    const end = Math.min(clean.length, Math.max(hit.end + half, start + windowChars));
    const overlaps = windows.some((w) => start <= w.end && end >= w.start);
    if (!overlaps) {
      windows.push({ start, end });
    }
  }

  return windows.map((win) => {
    const slice = clean.slice(win.start, win.end);
    const frags: SnippetFragmentDTO[] = [];
    const subPattern = new RegExp(`(${ordered.map(escapeRegExp).join("|")})`, "gi");
    let cursor = 0;
    let sub: RegExpExecArray | null;
    while ((sub = subPattern.exec(slice)) !== null) {
      if (sub.index > cursor) {
        frags.push({ text: slice.slice(cursor, sub.index), highlight: false });
      }
      frags.push({ text: sub[0], highlight: true });
      cursor = sub.index + sub[0].length;
    }
    if (cursor < slice.length) {
      frags.push({ text: slice.slice(cursor), highlight: false });
    }
    return frags;
  });
}

function itemMatchesFacets(item: LocalItemRecord, filters: LocalSearchFilters): boolean {
  if (filters.feed_id && item.feed_id !== filters.feed_id.toLowerCase()) {
    return false;
  }
  if (filters.category_id && filters.feedCategoryMap) {
    const itemCat = filters.feedCategoryMap.get(item.feed_id) ?? null;
    if (itemCat !== filters.category_id.toLowerCase()) {
      return false;
    }
  }
  if (filters.state && item.state !== filters.state) {
    return false;
  }
  if (filters.is_starred !== undefined && filters.is_starred !== null) {
    if (item.is_starred !== filters.is_starred) {
      return false;
    }
  }
  if (filters.date_from && item.effective_date < filters.date_from) {
    return false;
  }
  if (filters.date_to && item.effective_date > filters.date_to) {
    return false;
  }
  return true;
}

function combinedSearchableLower(item: LocalItemRecord): string {
  return [
    item.title,
    item.author ?? "",
    (item.tags ?? []).join(" "),
    item.enrichment?.tldr ?? "",
    (item.enrichment?.takeaways ?? []).join(" "),
    item.lead_excerpt ?? "",
  ]
    .join(" ")
    .replace(/\s+/g, " ")
    .toLowerCase();
}

export class LocalSearchIndex {
  private mini: MiniSearch<IndexedDoc>;
  private itemsById = new Map<string, LocalItemRecord>();

  constructor() {
    this.mini = this.createMiniSearch();
  }

  private createMiniSearch(): MiniSearch<IndexedDoc> {
    return new MiniSearch<IndexedDoc>({
      fields: ["title", "tags_text", "tldr", "author", "takeaways_text", "lead_excerpt"],
      storeFields: [],
      searchOptions: {
        boost: {
          title: 4.0,
          tags_text: 3.0,
          tldr: 3.0,
          author: 2.0,
          takeaways_text: 1.5,
          lead_excerpt: 1.0,
        },
        prefix: true,
        combineWith: "AND",
      },
    });
  }

  get size(): number {
    return this.itemsById.size;
  }

  rebuild(items: readonly LocalItemRecord[]): void {
    this.mini = this.createMiniSearch();
    this.itemsById.clear();
    const docs: IndexedDoc[] = [];
    for (const item of items) {
      this.itemsById.set(item.id, item);
      docs.push(toIndexedDoc(item));
    }
    if (docs.length > 0) {
      this.mini.addAll(docs);
    }
  }

  upsert(item: LocalItemRecord): void {
    if (this.itemsById.has(item.id)) {
      this.mini.replace(toIndexedDoc(item));
    } else {
      this.mini.add(toIndexedDoc(item));
    }
    this.itemsById.set(item.id, item);
  }

  discard(itemId: string): void {
    if (this.itemsById.has(itemId)) {
      this.mini.discard(itemId);
      this.itemsById.delete(itemId);
    }
  }

  search(filters: LocalSearchFilters): SearchResponse {
    const parsed = parseSearchQuery(filters.q);
    const limit = Math.min(100, Math.max(1, filters.limit ?? 20));
    const offset = Math.max(0, filters.offset ?? 0);
    const requestedSort = filters.sort ?? "relevance";
    const effectiveSort: "relevance" | "newest" = parsed.isEmpty ? "newest" : requestedSort;

    const candidates: Array<{ item: LocalItemRecord; score: number | null }> = [];

    if (parsed.isEmpty) {
      // Check if raw query had only punctuation/stop noise vs blank
      if (parsed.raw.length > 0) {
        return {
          query: parsed.raw,
          sort: effectiveSort,
          total: 0,
          limit,
          offset,
          results: [],
        };
      }
      for (const item of this.itemsById.values()) {
        if (itemMatchesFacets(item, filters)) {
          candidates.push({ item, score: null });
        }
      }
    } else {
      const miniQuery = [...parsed.positiveTerms, ...parsed.positivePhrases].join(" ");
      const hits = this.mini.search(miniQuery);
      for (const hit of hits) {
        const item = this.itemsById.get(String(hit.id));
        if (!item || !itemMatchesFacets(item, filters)) {
          continue;
        }
        const hay = combinedSearchableLower(item);
        if (parsed.positivePhrases.some((p) => !hay.includes(p))) {
          continue;
        }
        if (parsed.excludedPhrases.some((p) => hay.includes(p))) {
          continue;
        }
        if (parsed.excludedTerms.some((t) => hay.includes(t))) {
          continue;
        }
        candidates.push({ item, score: hit.score });
      }
    }

    candidates.sort((a, b) => {
      if (effectiveSort === "relevance" && a.score !== null && b.score !== null) {
        if (b.score !== a.score) {
          return b.score - a.score;
        }
      }
      if (b.item.effective_date !== a.item.effective_date) {
        return b.item.effective_date > a.item.effective_date ? 1 : -1;
      }
      return b.item.id > a.item.id ? 1 : b.item.id < a.item.id ? -1 : 0;
    });

    const total = candidates.length;
    const pageSlice = candidates.slice(offset, offset + limit);
    const highlightTerms = [...parsed.positivePhrases, ...parsed.positiveTerms];

    const results: SearchResultDTO[] = pageSlice.map(({ item, score }) => {
      const snippetSource = item.lead_excerpt || item.title;
      return {
        item: toSearchItemDTO(item),
        rank: score,
        snippet: parsed.isEmpty ? [] : buildSafeSnippets(snippetSource, highlightTerms),
      };
    });

    return {
      query: parsed.raw || null,
      sort: effectiveSort,
      total,
      limit,
      offset,
      results,
    };
  }
}
