/// <reference lib="webworker" />

/**
 * Minimal auditable SmartRSS Service Worker (1c §7, 1c-D06, U-08, U-14).
 *
 * - Build-time injected precache manifest (`__SMARTRSS_PRECACHE_MANIFEST__`, `__SMARTRSS_BUILD_HASH__`)
 * - Cache-first for static app-shell assets AND navigations (`/index.html`), keeping all tabs
 *   coherent with the active SW and honoring user-gated `SKIP_WAITING` updates (`U-14`).
 * - Strictly bypasses (network-only) all cross-origin requests, `/api/*`, and `/client-config.json`.
 * - Fetches precache entries with `{ cache: "reload" }` on install so runner CSP updates take effect (`U-08`).
 */

declare const self: ServiceWorkerGlobalScope;

const PRECACHE_URLS: string[] = __SMARTRSS_PRECACHE_MANIFEST__ as unknown as string[];
const BUILD_HASH: string = __SMARTRSS_BUILD_HASH__ as unknown as string;

const CACHE_PREFIX = "smartrss-shell-";
const CACHE_NAME = `${CACHE_PREFIX}${BUILD_HASH}-${encodeURIComponent(self.location.href)}`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      for (const url of PRECACHE_URLS) {
        const req = new Request(url, { cache: "reload" });
        const res = await fetch(req);
        if (res.ok) {
          await cache.put(url, res);
        }
      }
      // Auto-activate ONLY on initial install (no active worker yet); updates are user-gated (U-14)
      if (!self.registration.active) {
        await self.skipWaiting();
      }
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  const data = event.data as { type?: string } | null;
  if (data?.type === "SKIP_WAITING") {
    void self.skipWaiting();
  }
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") {
    return;
  }

  const url = new URL(req.url);

  // Strictly bypass cross-origin (LAN API), same-origin /api/*, and /client-config.json
  if (url.origin !== self.location.origin) {
    return;
  }
  if (url.pathname.startsWith("/api/") || url.pathname === "/client-config.json") {
    return;
  }

  // Coherent cache-first navigation -> precached /index.html (U-14)
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE_NAME);
        const cached = await cache.match("/index.html");
        if (cached) {
          return cached;
        }
        return fetch(req);
      })(),
    );
    return;
  }

  // Static app-shell assets: cache-first with network fallback
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(url.pathname);
      if (cached) {
        return cached;
      }
      return fetch(req);
    })(),
  );
});
