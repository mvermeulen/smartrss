/**
 * Safe feed HTML container mounting a single-pass DOMPurify DocumentFragment directly via
 * `replaceChildren()` (zero `dangerouslySetInnerHTML`, zero mXSS double-parse, U-13).
 */

import { useEffect, useRef } from "react";
import { sanitizeFeedHtmlToFragment } from "../../security/sanitizeHtml";

export function SafeHtml({
  html,
  baseUrl,
}: {
  html: string | null | undefined;
  baseUrl?: string | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) {
      return;
    }
    const fragment = sanitizeFeedHtmlToFragment(html, baseUrl);
    el.replaceChildren(fragment);
  }, [html, baseUrl]);

  return <div ref={containerRef} data-testid="safe-html-body" className="srss-prose text-sm" />;
}
