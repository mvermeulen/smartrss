/**
 * Tests for 1c-T02 (single-pass DOMPurify XSS/mXSS, remote image blocking, relative links, URL safety)
 * and U-05 (uuidv4 insecure-context fallback).
 */

import { describe, expect, it } from "vitest";
import { MAX_RAW_HTML_CHARS, sanitizeFeedHtmlToFragment } from "../../security/sanitizeHtml";
import { isSafeHttpUrl, resolveSafeHttpUrl, safeExternalHref } from "../../security/urlSafety";
import { uuidv4 } from "../../security/uuid";
import { HOSTILE_XSS_FIXTURES } from "../fixtures/hostileHtml";

function renderFragmentToContainer(frag: DocumentFragment): HTMLDivElement {
  const div = document.createElement("div");
  div.replaceChildren(frag);
  return div;
}

describe("uuidv4 (U-05 insecure context support)", () => {
  const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

  it("generates lowercase RFC 4122 v4 UUID using randomUUID when present", () => {
    const id = uuidv4();
    expect(id).toMatch(UUID_V4_RE);
    expect(id).toBe(id.toLowerCase());
  });

  it("falls back to crypto.getRandomValues when crypto.randomUUID is undefined", () => {
    const origCrypto = globalThis.crypto;
    const fallbackCrypto = {
      getRandomValues: (arr: Uint8Array) => origCrypto.getRandomValues(arr),
    };
    Object.defineProperty(globalThis, "crypto", {
      value: fallbackCrypto,
      configurable: true,
    });
    try {
      const a = uuidv4();
      const b = uuidv4();
      expect(a).toMatch(UUID_V4_RE);
      expect(b).toMatch(UUID_V4_RE);
      expect(a).not.toBe(b);
    } finally {
      Object.defineProperty(globalThis, "crypto", {
        value: origCrypto,
        configurable: true,
      });
    }
  });

  it("throws clearly if Web Crypto API is completely absent", () => {
    const origCrypto = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", {
      value: undefined,
      configurable: true,
    });
    try {
      expect(() => uuidv4()).toThrow("Web Crypto API unavailable");
    } finally {
      Object.defineProperty(globalThis, "crypto", {
        value: origCrypto,
        configurable: true,
      });
    }
  });
});

describe("urlSafety (S4, U-13)", () => {
  it("allows only http(s) URLs with hostnames", () => {
    expect(isSafeHttpUrl("https://example.com/a?b=1")).toBe(true);
    expect(isSafeHttpUrl("http://smartrss.lan:8080/feed")).toBe(true);
    expect(isSafeHttpUrl("")).toBe(false);
    expect(isSafeHttpUrl(null)).toBe(false);
    expect(isSafeHttpUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeHttpUrl("JAVASCRIPT:alert(1)")).toBe(false);
    expect(isSafeHttpUrl("data:text/html,<h1>x</h1>")).toBe(false);
    expect(isSafeHttpUrl("vbscript:msgbox(1)")).toBe(false);
    expect(isSafeHttpUrl("file:///etc/passwd")).toBe(false);
    expect(isSafeHttpUrl("blob:https://example.com/123")).toBe(false);
    expect(isSafeHttpUrl("//evil.example/path")).toBe(false);
    expect(isSafeHttpUrl("https://example.com/\x00evil")).toBe(false);
    expect(isSafeHttpUrl("not a url")).toBe(false);
  });

  it("resolves relative URLs against validated http(s) baseUrl and fails closed otherwise", () => {
    expect(resolveSafeHttpUrl("/2026/11/post", "https://blog.example.com/feed")).toBe(
      "https://blog.example.com/2026/11/post",
    );
    expect(resolveSafeHttpUrl("./fig.png", "https://blog.example.com/posts/1/")).toBe(
      "https://blog.example.com/posts/1/fig.png",
    );
    expect(resolveSafeHttpUrl("/2026/11/post", null)).toBeNull();
    expect(resolveSafeHttpUrl("/2026/11/post", "javascript:alert(1)")).toBeNull();
    expect(resolveSafeHttpUrl("//evil.example/x", "https://blog.example.com")).toBeNull();
    expect(resolveSafeHttpUrl("javascript:alert(1)", "https://blog.example.com")).toBeNull();
    expect(safeExternalHref("/rel", "https://blog.example.com")).toBe(
      "https://blog.example.com/rel",
    );
    expect(safeExternalHref("javascript:alert(1)")).toBeUndefined();
  });
});

describe("sanitizeFeedHtmlToFragment (1c-T02, U-13, Checklist B4)", () => {
  it("neutralizes all XSS & mXSS fixtures with zero scripts, handlers, styles, or imgs", () => {
    for (const [name, html] of Object.entries(HOSTILE_XSS_FIXTURES)) {
      const container = renderFragmentToContainer(
        sanitizeFeedHtmlToFragment(html, "https://blog.example.com/articles/1"),
      );
      expect(container.querySelector("script"), `${name}: script`).toBeNull();
      expect(container.querySelector("img"), `${name}: img`).toBeNull();
      expect(container.querySelector("picture"), `${name}: picture`).toBeNull();
      expect(container.querySelector("svg"), `${name}: svg`).toBeNull();
      expect(container.querySelector("math"), `${name}: math`).toBeNull();
      expect(container.querySelector("iframe"), `${name}: iframe`).toBeNull();
      expect(container.querySelector("form"), `${name}: form`).toBeNull();
      expect(container.querySelector("style"), `${name}: style`).toBeNull();
      expect(container.querySelector("noscript"), `${name}: noscript`).toBeNull();
      expect(container.querySelector("[style]"), `${name}: [style]`).toBeNull();
      expect(container.querySelector("[onerror]"), `${name}: [onerror]`).toBeNull();
      expect(container.querySelector("[onload]"), `${name}: [onload]`).toBeNull();
      expect(container.querySelector("[id]"), `${name}: [id]`).toBeNull();
      expect(container.querySelector("[name]"), `${name}: [name]`).toBeNull();
      expect(container.innerHTML.toLowerCase(), `${name}: js scheme`).not.toContain("javascript:");
    }
  });

  it("replaces remote images and pictures with safe alt/link placeholders and strips foreign classes", () => {
    const imgContainer = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(HOSTILE_XSS_FIXTURES.imgOnError),
    );
    const placeholder = imgContainer.querySelector("span.srss-blocked-img");
    expect(placeholder).not.toBeNull();
    expect(placeholder?.textContent).toContain("[Image blocked: Hero Chart]");
    const link = placeholder?.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://tracker.invalid/pixel.gif");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");

    const picContainer = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(HOSTILE_XSS_FIXTURES.pictureElement),
    );
    expect(picContainer.querySelector("span.srss-blocked-img")?.textContent).toContain("Diagram");

    const dataNoAlt = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(HOSTILE_XSS_FIXTURES.dataImgNoAlt),
    );
    expect(dataNoAlt.textContent?.trim()).toBe("");

    const dataWithAlt = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(HOSTILE_XSS_FIXTURES.dataImgWithAlt),
    );
    expect(dataWithAlt.textContent).toContain("[Image blocked: Inline chart]");
    expect(dataWithAlt.querySelector("a")).toBeNull();

    const styled = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(HOSTILE_XSS_FIXTURES.inlineStyleAndClass),
    );
    const p = styled.querySelector("p");
    expect(p?.hasAttribute("class")).toBe(false);
    expect(p?.hasAttribute("style")).toBe(false);
    expect(p?.hasAttribute("id")).toBe(false);

    // Image with valid URL and NO alt falls back to hostname label
    const noAltImg = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(`<img src="https://cdn.example.org/banner.jpg" />`),
    );
    expect(noAltImg.querySelector("span.srss-blocked-img")?.textContent).toContain(
      "[Image blocked: cdn.example.org]",
    );

    // Empty <picture> with no <img> is removed cleanly
    const emptyPic = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(
        `<picture><source srcset="https://x.test/a.webp" /></picture><p>ok</p>`,
      ),
    );
    expect(emptyPic.querySelector("picture")).toBeNull();
    expect(emptyPic.textContent?.trim()).toBe("ok");
  });

  it("resolves relative links and relative images against baseUrl", () => {
    const container = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(
        HOSTILE_XSS_FIXTURES.relativeLink,
        "https://blog.example.com/feed/index.html",
      ),
    );
    const links = Array.from(container.querySelectorAll("a"));
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "https://blog.example.com/2026/11/post?id=42",
      "https://blog.example.com/images/fig1.png",
    ]);
    for (const a of links) {
      expect(a.getAttribute("target")).toBe("_blank");
      expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    }
  });

  it("strips unsafe hrefs while preserving link text and handles empty/oversized inputs", () => {
    const jsLink = renderFragmentToContainer(
      sanitizeFeedHtmlToFragment(HOSTILE_XSS_FIXTURES.jsHref),
    );
    const anchor = jsLink.querySelector("a");
    expect(anchor?.textContent).toBe("Click me");
    expect(anchor?.hasAttribute("href")).toBe(false);

    expect(renderFragmentToContainer(sanitizeFeedHtmlToFragment(null)).childNodes.length).toBe(0);
    const huge = "<p>" + "a".repeat(MAX_RAW_HTML_CHARS + 5000) + "</p>";
    const bounded = renderFragmentToContainer(sanitizeFeedHtmlToFragment(huge));
    expect((bounded.textContent ?? "").length).toBeLessThanOrEqual(MAX_RAW_HTML_CHARS);
  });
});
