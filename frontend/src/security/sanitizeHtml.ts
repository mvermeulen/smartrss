/**
 * Single-pass DOMPurify feed HTML sanitizer with remote image blocking (1c §9.2, 1c-D02, U-13).
 *
 * Uses DOMPurify with RETURN_DOM_FRAGMENT: true so untrusted markup is parsed ONCE and
 * mounted directly via `replaceChildren()` with zero re-serialization / zero `dangerouslySetInnerHTML`,
 * eliminating double-parse mXSS surfaces (Checklist B4).
 */

import createDOMPurify from "dompurify";
import { resolveSafeHttpUrl } from "./urlSafety";

export const MAX_RAW_HTML_CHARS = 65_536;
const MAX_ALT_CHARS = 120;

const ALLOWED_TAGS: string[] = [
  "p",
  "br",
  "a",
  "ul",
  "ol",
  "li",
  "blockquote",
  "pre",
  "code",
  "em",
  "strong",
  "b",
  "i",
  "u",
  "s",
  "del",
  "sub",
  "sup",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "dl",
  "dt",
  "dd",
  "figure",
  "figcaption",
  "span",
  "div",
];

const ALLOWED_ATTR: string[] = ["href", "title", "colspan", "rowspan", "target", "rel", "class"];

const FORBID_TAGS: string[] = [
  "script",
  "style",
  "iframe",
  "object",
  "embed",
  "form",
  "input",
  "textarea",
  "select",
  "button",
  "svg",
  "math",
  "img",
  "picture",
  "video",
  "audio",
  "source",
  "link",
  "meta",
  "base",
  "template",
  "noscript",
];

const FORBID_ATTR: string[] = [
  "style",
  "id",
  "name",
  "src",
  "srcset",
  "onerror",
  "onload",
  "formaction",
];

function extractHostLabel(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "external image";
  }
}

function buildBlockedImagePlaceholder(
  doc: Document,
  rawAlt: string | null,
  rawSrc: string | null,
  baseUrl?: string | null,
): HTMLElement | null {
  const cleanAlt = (rawAlt ?? "").replace(/\s+/g, " ").trim().slice(0, MAX_ALT_CHARS);
  const safeSrc = resolveSafeHttpUrl(rawSrc, baseUrl);

  if (!safeSrc && !cleanAlt) {
    return null;
  }

  const span = doc.createElement("span");
  span.setAttribute("class", "srss-blocked-img");

  if (safeSrc) {
    const label = cleanAlt || extractHostLabel(safeSrc);
    span.appendChild(doc.createTextNode(`[Image blocked: ${label}] `));
    const link = doc.createElement("a");
    link.setAttribute("href", safeSrc);
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noopener noreferrer");
    link.appendChild(doc.createTextNode("Open image"));
    span.appendChild(link);
  } else {
    span.appendChild(doc.createTextNode(`[Image blocked: ${cleanAlt}]`));
  }
  return span;
}

/**
 * Sanitize untrusted RSS HTML into a safe DOM DocumentFragment in a single DOMPurify pass.
 */
export function sanitizeFeedHtmlToFragment(
  rawHtml: string | null | undefined,
  baseUrl?: string | null,
): DocumentFragment {
  if (!rawHtml) {
    return document.createDocumentFragment();
  }

  const bounded =
    rawHtml.length > MAX_RAW_HTML_CHARS ? rawHtml.slice(0, MAX_RAW_HTML_CHARS) : rawHtml;

  // Use a fresh isolated DOMPurify instance per call so hooks carry call-scoped baseUrl cleanly
  const purify = createDOMPurify(window);
  const trustedPlaceholders = new WeakSet<Element>();

  purify.addHook("uponSanitizeElement", (node) => {
    if (node.nodeType !== 1) {
      return;
    }
    const el = node as Element;
    const tag = el.tagName.toUpperCase();
    if (tag === "IMG") {
      const ownerDoc = el.ownerDocument ?? document;
      const placeholder = buildBlockedImagePlaceholder(
        ownerDoc,
        el.getAttribute("alt"),
        el.getAttribute("src"),
        baseUrl,
      );
      if (placeholder && el.parentNode) {
        trustedPlaceholders.add(placeholder);
        el.parentNode.replaceChild(placeholder, el);
      } else if (el.parentNode) {
        el.parentNode.removeChild(el);
      }
    } else if (tag === "PICTURE") {
      const innerImg = el.querySelector("img");
      const ownerDoc = el.ownerDocument ?? document;
      const placeholder = innerImg
        ? buildBlockedImagePlaceholder(
            ownerDoc,
            innerImg.getAttribute("alt"),
            innerImg.getAttribute("src"),
            baseUrl,
          )
        : null;
      if (placeholder && el.parentNode) {
        trustedPlaceholders.add(placeholder);
        el.parentNode.replaceChild(placeholder, el);
      } else if (el.parentNode) {
        el.parentNode.removeChild(el);
      }
    }
  });

  purify.addHook("afterSanitizeAttributes", (node) => {
    if (node.nodeType !== 1) {
      return;
    }
    const el = node as Element;

    // Strip any untrusted class except nodes created by our blocked-image hook (V-10)
    if (el.hasAttribute("class") && !trustedPlaceholders.has(el)) {
      el.removeAttribute("class");
    }

    if (el.tagName.toUpperCase() === "A") {
      const rawHref = el.getAttribute("href");
      const resolved = resolveSafeHttpUrl(rawHref, baseUrl);
      if (!resolved) {
        el.removeAttribute("href");
        el.removeAttribute("target");
        el.removeAttribute("rel");
      } else {
        el.setAttribute("href", resolved);
        el.setAttribute("target", "_blank");
        el.setAttribute("rel", "noopener noreferrer");
      }
    } else {
      el.removeAttribute("href");
      el.removeAttribute("target");
      el.removeAttribute("rel");
    }
  });

  const fragment = purify.sanitize(bounded, {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    FORBID_TAGS,
    FORBID_ATTR,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    KEEP_CONTENT: true,
  });

  purify.removeAllHooks();
  return fragment;
}
