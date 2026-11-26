import { expect, test } from "@playwright/test";

import {
  bumpFeedVersionInDb,
  pairViaUi,
  queryDbState,
  resetAndSeed,
  startApiServer,
  startClientRunner,
  startNginxWebContainer,
  type ManagedProcess,
} from "./helpers.ts";

test.describe("1c-T14: Feeds/OPML, Diff-Only EditFeedModal, 409 Conflict & Device Revocation/Sign Out", () => {
  const API_PORT = 18103;
  const RUNNER_PORT = 14183;
  const API_ORIGIN = `http://localhost:${API_PORT}`;
  const RUNNER_ORIGIN = `http://localhost:${RUNNER_PORT}`;

  let api: ManagedProcess | null = null;
  let runner: ManagedProcess | null = null;

  test.beforeEach(async () => {
    api = await startApiServer({
      port: API_PORT,
      allowedHosts: `localhost:${API_PORT},127.0.0.1:${API_PORT}`,
      corsAllowOrigins: RUNNER_ORIGIN,
    });
    runner = await startClientRunner({
      port: RUNNER_PORT,
      apiOrigin: API_ORIGIN,
    });
  });

  test.afterEach(async () => {
    await runner?.stop();
    await api?.stop();
    runner = null;
    api = null;
  });

  test("1c-T14: OPML import/export, diff-only PATCH with 409 conflict diff resolution, pairing second device, revoke & Sign Out", async ({
    browser,
  }) => {
    const seed = resetAndSeed({ items: 4 });

    const ctx1 = await browser.newContext();
    const page1 = await ctx1.newPage();
    await page1.goto(RUNNER_ORIGIN);
    await pairViaUi(page1, seed.pairing_code);

    // --- OPML import & export ---
    await page1.getByTestId("nav-feeds").click();
    await expect(page1.getByTestId("feed-manager-view")).toBeVisible();

    const opmlSample = `<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>SmartRSS E2E</title></head>
  <body>
    <outline text="ImportedCat" title="ImportedCat">
      <outline type="rss" text="Gamma Blog" title="Gamma Blog" xmlUrl="https://gamma.example.com/rss.xml" htmlUrl="https://gamma.example.com" />
    </outline>
  </body>
</opml>`;
    await page1.getByTestId("opml-xml-textarea").fill(opmlSample);
    await page1.getByTestId("import-opml-btn").click();
    await expect(page1.getByTestId("opml-import-result")).toContainText("Imported 1");

    // Verify OPML export triggers a download with valid OPML XML
    const downloadPromise = page1.waitForEvent("download");
    await page1.getByTestId("export-opml-btn").click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toContain(".opml");

    // --- Diff-only EditFeedModal PATCH + 409 Conflict diff dialog ---
    const patchPayloads: Array<Record<string, unknown>> = [];
    page1.on("request", (req) => {
      if (req.method() === "PATCH" && req.url().includes(`/api/v1/feeds/${seed.feed_ids.alpha}`)) {
        const body = req.postData();
        if (body) {
          patchPayloads.push(JSON.parse(body) as Record<string, unknown>);
        }
      }
    });

    await page1.getByTestId(`edit-feed-btn-${seed.feed_ids.alpha}`).click();
    await page1
      .getByTestId("curation-instructions-input")
      .fill("Prioritize consensus papers and benchmarks.");

    // Simulate concurrent edit on the server bumping config_version
    bumpFeedVersionInDb(seed.feed_ids.alpha, "Server-side concurrent instructions.");

    await page1.getByTestId("save-feed-edit-btn").click();

    // Conflict diff dialog renders showing both versions
    await expect(page1.getByTestId("conflict-diff-dialog")).toBeVisible();
    await expect(page1.getByTestId("conflict-diff-dialog")).toContainText(
      "Server-side concurrent instructions.",
    );
    await expect(page1.getByTestId("conflict-diff-dialog")).toContainText(
      "Prioritize consensus papers and benchmarks.",
    );

    // Assert first PATCH omitted unchanged LWW fields (status, category_id, title)
    expect(patchPayloads.length).toBeGreaterThanOrEqual(1);
    expect(patchPayloads[0]).toHaveProperty("curation_instructions");
    expect(patchPayloads[0]).toHaveProperty("expected_config_version", 1);
    expect(patchPayloads[0]).not.toHaveProperty("status");
    expect(patchPayloads[0]).not.toHaveProperty("category_id");
    expect(patchPayloads[0]).not.toHaveProperty("title");

    // Resolve conflict by overwriting with local edit (sends expected_config_version=2)
    await page1.getByTestId("conflict-overwrite-btn").click();
    await expect(page1.getByTestId("conflict-diff-dialog")).toHaveCount(0);

    const dbAfterConflict = queryDbState();
    expect(dbAfterConflict.feeds[seed.feed_ids.alpha]?.curation_instructions).toBe(
      "Prioritize consensus papers and benchmarks.",
    );
    expect(dbAfterConflict.feeds[seed.feed_ids.alpha]?.config_version).toBe(3);

    // --- Generate pairing code for second device, pair it, queue offline edit on device 2, revoke device 2 ---
    await page1.getByTestId("nav-settings").click();
    await expect(page1.getByTestId("settings-devices-view")).toBeVisible();
    await page1.getByTestId("generate-pairing-code-btn").click();
    const rawCodeText =
      (await page1.getByTestId("generated-pairing-code").locator("div").first().textContent()) ??
      "";
    const secondCode = rawCodeText.trim();
    expect(secondCode).toMatch(/^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/);

    const ctx2 = await browser.newContext();
    const page2 = await ctx2.newPage();
    await page2.goto(RUNNER_ORIGIN);
    await page2.locator("#device-name").fill("Second Ubuntu Laptop");
    await pairViaUi(page2, secondCode);

    // Queue an offline mutation on device 2 before revoking it
    await ctx2.setOffline(true);
    await page2.getByTestId(`item-row-${seed.item_ids[0]}`).click();
    await page2.getByTestId("toggle-star-btn").click();
    await expect(page2.getByTestId("pending-outbox-badge")).toBeVisible();

    // Find device 2 ID from DB and revoke it from device 1's Settings UI
    const dbDevices = queryDbState().devices;
    const deviceIds = Object.keys(dbDevices);
    expect(deviceIds.length).toBe(2);
    const secondDeviceId = deviceIds.find((id) => dbDevices[id]?.name === "Second Ubuntu Laptop")!;

    await page1.reload();
    await page1.getByTestId("nav-settings").click();
    page1.once("dialog", (d) => void d.accept());
    await page1.getByTestId(`revoke-device-${secondDeviceId}`).click();
    await expect(page1.getByTestId(`revoke-device-${secondDeviceId}`)).toHaveCount(0, {
      timeout: 10_000,
    });

    // Device 2 comes back online -> sync receives 401 -> transitions to auth_revoked WITHOUT wiping outbox
    await ctx2.setOffline(false);
    await page2.getByTestId("manual-sync-btn").click();
    await expect(page2.getByTestId("auth-revoked-banner")).toBeVisible({ timeout: 15_000 });
    await expect(page2.getByTestId("pending-outbox-badge")).toBeVisible();

    // --- Sign Out on Device 1 revokes own device server-side ---
    await page1.getByTestId("sign-out-clear-btn").click();
    await expect(page1.locator("#pair-code")).toBeVisible({ timeout: 15_000 });

    const finalDevices = queryDbState().devices;
    for (const dev of Object.values(finalDevices)) {
      expect(dev.revoked_at).not.toBeNull();
    }

    await ctx2.close();
    await ctx1.close();
  });
});

test.describe("1c-T15: Non-Localhost Topology, Insecure Nginx web Fallback & ALLOWED_HOSTS Misconfiguration", () => {
  test("1c-T15 (a): insecure HTTP Nginx web fallback (http://smartrss.test) pairs, bootstraps & mutates with getRandomValues + fenced IDB lock", async ({
    page,
  }) => {
    const API_PORT = 18104;
    const NGINX_PORT = 18088;
    const seed = resetAndSeed({ items: 4 });

    // Bind API on 0.0.0.0 so the Docker Nginx container can reach it via host.docker.internal
    const api = await startApiServer({
      port: API_PORT,
      host: "0.0.0.0",
      allowedHosts: `smartrss.test:${NGINX_PORT},localhost:${API_PORT},127.0.0.1:${API_PORT},host.docker.internal:${API_PORT}`,
      corsAllowOrigins: `http://smartrss.test:${NGINX_PORT}`,
    });
    const web = await startNginxWebContainer({ httpPort: NGINX_PORT, apiPort: API_PORT });

    try {
      const pageErrors: string[] = [];
      page.on("pageerror", (err) => pageErrors.push(err.message));

      await page.goto(`http://smartrss.test:${NGINX_PORT}`);

      // Prove this is genuinely an insecure context (non-localhost plain HTTP)
      const isSecure = await page.evaluate(() => window.isSecureContext);
      expect(isSecure).toBe(false);

      await pairViaUi(page, seed.pairing_code);

      // Connected LAN fallback banner is displayed
      await expect(page.getByTestId("lan-fallback-banner")).toBeVisible();

      // Perform read, star, and feed status toggle mutations -> proves getRandomValues UUIDv4 + IDB lease lock!
      const itemId = seed.item_ids[0]!;
      await page.getByTestId(`item-row-${itemId}`).click();
      await page.getByTestId("toggle-read-btn").click();
      await page.getByTestId("toggle-star-btn").click();
      await page.getByTestId("feed-quick-actions-btn").click();
      await page.getByTestId("quick-feed-status-muted").click();

      await page.getByTestId("manual-sync-btn").click();
      await expect(page.getByTestId("pending-outbox-badge")).toHaveCount(0, { timeout: 15_000 });
      expect(pageErrors).toEqual([]);

      const dbState = queryDbState();
      expect(dbState.items[itemId]?.state).toBe("read");
      expect(dbState.items[itemId]?.is_starred).toBe(true);
      expect(dbState.feeds[seed.feed_ids.alpha]?.status).toBe("muted");
    } finally {
      await web.stop();
      await api.stop();
    }
  });

  test("1c-T15 (b) & (c): localhost runner against non-localhost API origin & missing ALLOWED_HOSTS probe -> misconfigured", async ({
    browser,
  }) => {
    const API_PORT = 18105;
    const RUNNER_PORT = 14185;
    const API_ORIGIN = `http://api.smartrss.test:${API_PORT}`;
    const RUNNER_ORIGIN = `http://localhost:${RUNNER_PORT}`;

    const seed = resetAndSeed({ items: 3 });

    // (b) Valid non-localhost API origin with matching ALLOWED_HOSTS & CORS_ALLOW_ORIGINS
    let api = await startApiServer({
      port: API_PORT,
      allowedHosts: `api.smartrss.test:${API_PORT},127.0.0.1:${API_PORT}`,
      corsAllowOrigins: RUNNER_ORIGIN,
    });
    const runner = await startClientRunner({
      port: RUNNER_PORT,
      apiOrigin: API_ORIGIN,
    });

    try {
      const ctx1 = await browser.newContext();
      const page1 = await ctx1.newPage();

      await page1.addInitScript(() => {
        (window as unknown as { __cspViolations: string[] }).__cspViolations = [];
        document.addEventListener("securitypolicyviolation", (e) => {
          (window as unknown as { __cspViolations: string[] }).__cspViolations.push(
            `${e.effectiveDirective}:${e.blockedURI}`,
          );
        });
      });

      await page1.goto(RUNNER_ORIGIN);
      await pairViaUi(page1, seed.pairing_code);
      await expect(page1.getByTestId(`item-row-${seed.item_ids[0]}`)).toBeVisible();

      const cspViolations = await page1.evaluate(
        () => (window as unknown as { __cspViolations: string[] }).__cspViolations,
      );
      expect(cspViolations).toEqual([]);
      await ctx1.close();

      // (c) U-17-F1: Restart API omitting api.smartrss.test:<port> from ALLOWED_HOSTS while keeping CORS valid
      await api.stop();
      api = await startApiServer({
        port: API_PORT,
        allowedHosts: `localhost:${API_PORT},127.0.0.1:${API_PORT}`,
        corsAllowOrigins: RUNNER_ORIGIN,
      });

      const ctx2 = await browser.newContext();
      const page2 = await ctx2.newPage();
      await page2.goto(RUNNER_ORIGIN);
      await expect(page2.getByTestId("probe-misconfigured")).toBeVisible({ timeout: 15_000 });
      await expect(page2.getByTestId("probe-misconfigured")).toContainText("ALLOWED_HOSTS");
      await ctx2.close();
    } finally {
      await runner.stop();
      await api.stop();
    }
  });
});
