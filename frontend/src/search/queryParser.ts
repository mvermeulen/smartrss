/**
 * Pure websearch-compatible query parser matching Milestone 1b's `websearch_to_tsquery`
 * syntax and negation-only guard (1c §8.2, 1b §8.2).
 */

export interface ParsedSearchQuery {
  raw: string;
  positiveTerms: string[];
  positivePhrases: string[];
  excludedTerms: string[];
  excludedPhrases: string[];
  isEmpty: boolean;
}

export class NegationOnlyQueryError extends Error {
  constructor() {
    super("Search query must include at least one positive required search term");
    this.name = "NegationOnlyQueryError";
  }
}

export function parseSearchQuery(raw: string | null | undefined): ParsedSearchQuery {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) {
    return {
      raw: "",
      positiveTerms: [],
      positivePhrases: [],
      excludedTerms: [],
      excludedPhrases: [],
      isEmpty: true,
    };
  }

  const positiveTerms: string[] = [];
  const positivePhrases: string[] = [];
  const excludedTerms: string[] = [];
  const excludedPhrases: string[] = [];

  // Match optional leading `-` followed by either `"quoted phrase"` or non-whitespace token
  const tokenRegex = /(-)?(?:"([^"]*)"|(\S+))/g;
  let match: RegExpExecArray | null;
  while ((match = tokenRegex.exec(trimmed)) !== null) {
    const isNegated = match[1] === "-";
    const quoted = match[2];
    const bare = match[3];

    if (quoted !== undefined) {
      const phrase = quoted.replace(/\s+/g, " ").trim().toLowerCase();
      if (!phrase) {
        continue;
      }
      if (isNegated) {
        excludedPhrases.push(phrase);
      } else {
        positivePhrases.push(phrase);
      }
    } else if (bare !== undefined) {
      // Strip leading/trailing punctuation-only noise while keeping inner hyphenated words
      const cleaned = bare.replace(/^[^\p{L}\p{N}_]+|[^\p{L}\p{N}_]+$/gu, "").toLowerCase();
      if (!cleaned) {
        continue;
      }
      if (cleaned === "or" && !isNegated) {
        // Treat standalone OR connector as non-term
        continue;
      }
      if (isNegated) {
        excludedTerms.push(cleaned);
      } else {
        positiveTerms.push(cleaned);
      }
    }
  }

  const hasPositive = positiveTerms.length > 0 || positivePhrases.length > 0;
  const hasExcluded = excludedTerms.length > 0 || excludedPhrases.length > 0;

  if (!hasPositive && hasExcluded) {
    throw new NegationOnlyQueryError();
  }

  return {
    raw: trimmed,
    positiveTerms,
    positivePhrases,
    excludedTerms,
    excludedPhrases,
    isEmpty: !hasPositive && !hasExcluded,
  };
}
