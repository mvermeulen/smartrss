/**
 * Extract the URL from an obvious Markdown `[label](https://...)` or `<https://...>`
 * paste before submitting a feed URL (E-10).
 */
const MARKDOWN_LINK_RE = /^\[[^\]]*\]\((https?:\/\/[^)\s]+)\)$/i;
const ANGLE_BRACKET_URL_RE = /^<(https?:\/\/[^>\s]+)>$/i;

export function cleanPastedFeedUrl(raw: string): string {
  const trimmed = raw.trim();
  const mdMatch = MARKDOWN_LINK_RE.exec(trimmed);
  if (mdMatch?.[1]) {
    return mdMatch[1];
  }
  const angleMatch = ANGLE_BRACKET_URL_RE.exec(trimmed);
  if (angleMatch?.[1]) {
    return angleMatch[1];
  }
  return trimmed;
}
