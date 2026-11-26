/**
 * Server reachability, CORS_ALLOW_ORIGINS & ALLOWED_HOSTS misconfiguration probe (U-17, U-17-F1).
 *
 * Step 1: `GET /api/v1/health` (checks liveness & CORS). On TypeError, follows up with
 *         `fetch(..., { mode: "no-cors" })`: if that resolves, server is reachable but
 *         `CORS_ALLOW_ORIGINS` is missing this origin.
 * Step 2: Because `/api/v1/health` is intentionally exempt from `HostAllowListMiddleware`,
 *         when Step 1 succeeds, probes unauthenticated `GET /api/v1/devices` (hits
 *         `HostAllowListMiddleware` -> `CORSMiddleware` -> immediate `401` with zero DB queries).
 *         Readable `401` confirms both Host and CORS; TypeError means `ALLOWED_HOSTS` rejected it.
 */

export type ProbeOutcome =
  | { status: "reachable"; healthOk: boolean }
  | { status: "detached" }
  | {
      status: "misconfigured";
      cause: "cors" | "allowed_hosts";
      message: string;
    };

async function timedFetch(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = globalThis.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetchImpl(url, {
      ...init,
      credentials: "omit",
      cache: "no-store",
      signal: controller.signal,
    });
  } finally {
    globalThis.clearTimeout(timer);
  }
}

export async function probeServerReachability(
  apiOrigin: string,
  fetchImpl?: typeof fetch,
  timeoutMs = 3_000,
): Promise<ProbeOutcome> {
  const doFetch: typeof fetch = fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  const base = apiOrigin.replace(/\/+$/, "");
  const healthUrl = `${base}/api/v1/health`;
  const hostCheckUrl = `${base}/api/v1/devices`;

  // Step 1: Liveness & CORS_ALLOW_ORIGINS check
  let healthOk = false;
  try {
    const healthRes = await timedFetch(
      doFetch,
      healthUrl,
      { method: "GET", headers: { Accept: "application/json" } },
      timeoutMs,
    );
    healthOk = healthRes.ok;
    void healthRes.body?.cancel?.().catch(() => undefined);
  } catch (err) {
    // AbortError = real timeout -> detached; TypeError could be offline OR CORS rejection
    if (err instanceof DOMException && err.name === "AbortError") {
      return { status: "detached" };
    }
    try {
      const noCorsRes = await timedFetch(
        doFetch,
        healthUrl,
        { method: "GET", mode: "no-cors" },
        timeoutMs,
      );
      void noCorsRes.body?.cancel?.().catch(() => undefined);
      return {
        status: "misconfigured",
        cause: "cors",
        message:
          "Server is reachable, but CORS_ALLOW_ORIGINS in the server's .env does not allow this client origin.",
      };
    } catch {
      return { status: "detached" };
    }
  }

  // Step 2: ALLOWED_HOSTS check via unauthenticated GET /api/v1/devices (U-17-F1)
  try {
    const devicesRes = await timedFetch(
      doFetch,
      hostCheckUrl,
      { method: "GET", headers: { Accept: "application/json" } },
      timeoutMs,
    );
    void devicesRes.body?.cancel?.().catch(() => undefined);
    // Readable 401 (expected without Bearer token) or 200 proves Host + CORS are valid
    if (devicesRes.status === 401 || devicesRes.ok) {
      return { status: "reachable", healthOk };
    }
    if (devicesRes.status === 400) {
      return {
        status: "misconfigured",
        cause: "allowed_hosts",
        message:
          "Server rejected this Host header (HTTP 400). Add this server hostname:port to ALLOWED_HOSTS in .env.",
      };
    }
    return { status: "reachable", healthOk };
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      return { status: "detached" };
    }
    return {
      status: "misconfigured",
      cause: "allowed_hosts",
      message:
        "Server is reachable, but rejected the Host header on API routes. Add this server hostname:port to ALLOWED_HOSTS in .env.",
    };
  }
}
