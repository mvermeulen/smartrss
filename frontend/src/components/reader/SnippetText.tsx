/**
 * XSS-safe structured search snippet renderer (1b §8.2, 1c §8.2).
 * Renders every fragment strictly as React text nodes with `<mark>` around highlighted terms.
 */

import type { SnippetFragmentDTO } from "../../types/api";

export function SnippetText({ fragments }: { fragments: readonly SnippetFragmentDTO[][] }) {
  if (!fragments || fragments.length === 0) {
    return null;
  }
  return (
    <div
      data-testid="search-snippet"
      className="mt-1 space-y-0.5 text-xs text-slate-600 dark:text-slate-400"
    >
      {fragments.map((group, gIdx) => (
        <p key={gIdx} className="line-clamp-2">
          {gIdx > 0 ? "… " : ""}
          {group.map((frag, fIdx) =>
            frag.highlight ? (
              <mark
                key={fIdx}
                className="rounded bg-amber-200 px-0.5 text-slate-900 dark:bg-amber-500/40 dark:text-amber-100"
              >
                {frag.text}
              </mark>
            ) : (
              <span key={fIdx}>{frag.text}</span>
            ),
          )}
        </p>
      ))}
    </div>
  );
}
