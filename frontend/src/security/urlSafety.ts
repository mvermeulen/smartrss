/**
 * Strict URL scheme validation & relative URL resolution (Checklist S4, U-13).
 *
 * Only `http:` and `https:` URLs with a non-empty hostname and no ASCII control chars
 * are ever rendered as clickable links.
 */

function hasControlChars(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) {
      return true;
    }
  }
  return false;
}

function hasExplicitScheme(value: string): boolean {
  return /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value);
}

export function isSafeHttpUrl(raw: string | null | undefined): boolean {
  if (!raw || typeof raw !== "string") {
    return false;
  }
  const trimmed = raw.trim();
  if (!trimmed || hasControlChars(trimmed)) {
    return false;
  }
  // Reject protocol-relative URLs (e.g. //evil.example)
  if (trimmed.startsWith("//")) {
    return false;
  }
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return false;
    }
    return parsed.hostname.length > 0;
  } catch {
    return false;
  }
}

/**
 * Validate an absolute URL or resolve a relative URL against a validated http(s) baseUrl.
 * Fails closed (returns null) for javascript:, data:, vbscript:, file:, blob:, //, or invalid URLs.
 */
export function resolveSafeHttpUrl(
  raw: string | null | undefined,
  baseUrl?: string | null,
): string | null {
  if (!raw || typeof raw !== "string") {
    return null;
  }
  const trimmed = raw.trim();
  if (!trimmed || hasControlChars(trimmed) || trimmed.startsWith("//")) {
    return null;
  }

  if (hasExplicitScheme(trimmed)) {
    return isSafeHttpUrl(trimmed) ? new URL(trimmed).href : null;
  }

  if (!baseUrl || !isSafeHttpUrl(baseUrl)) {
    return null;
  }

  try {
    const resolved = new URL(trimmed, baseUrl).href;
    return isSafeHttpUrl(resolved) ? resolved : null;
  } catch {
    return null;
  }
}

export function safeExternalHref(
  raw: string | null | undefined,
  baseUrl?: string | null,
): string | undefined {
  return resolveSafeHttpUrl(raw, baseUrl) ?? undefined;
}
