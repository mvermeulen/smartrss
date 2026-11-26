import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { expect, test } from "@playwright/test";

import {
  DIST_DIR,
  deleteCategoryInDb,
  pairViaUi,
  queryDbState,
  resetAndSeed,
  startApiServer,
  startClientRunner,
  type ManagedProcess,
} from "./helpers.ts";

const API_PORT = 18101;
const RUNNER_PORT = 14181;
const API_ORIGIN = `http://localhost:${API_PORT}`;
const RUNNER_ORIGIN = `http://localhost:${RUNNER_PORT}`;

test.describe("1c-T09, 1c-T10, 1c-T11: Reader, Detached Offline Reload, 500-Mutation Flush, SW Update & Rollback UX", () => {
  let api: ManagedProcess | null = null;
  let runner: ManagedProcess | null = null;
  let tmpDist: string | null = null;

  test.beforeEach(async () => {
    tmpDist = fs.mkdtempSync(path.join(os.tmpdir(), "smartrss-e2e-dist-"));
    fs.cpSync(DIST_DIR, tmpDist, { recursive: true });
    api = await startApiServer({
      port: API_PORT,
      allowedHosts: `localhost:${API_PORT},127.0.0.1:${API_PORT}`,
      corsAllowOrigins: RUNNER_ORIGIN,
    });
    runner = await startClientRunner({
      port: RUNNER_PORT,
      apiOrigin: API_ORIGIN,
      distDir: tmpDist,
    });
  });

  test.afterEach(async () => {
    await runner?.stop();
    await api?.stop();
    runner = null;
    api = null;
    if (tmpDist) {
      fs.rmSync(tmpDist, { recursive: true, force: true });
      tmpDist = null;
    }
  });

  test("1c-T09: pairing, bootstrap, 3-pane reader, sorting, filters, keyboard shortcuts & storage.persist", async ({
    page,
  }) => {
    const seed = resetAndSeed({ items: 6 });

    // Spy on navigator.storage.persist() before page scripts execute
    await page.addInitScript(() => {
      let called = false;
      (window as unknown as { __persistCalled?: boolean }).__persistCalled = false;
      if (navigator.storage && typeof navigator.storage.persist === "function") {
        const orig = navigator.storage.persist.bind(navigator.storage);
        navigator.storage.persist = async () => {
          called = true;
          (window as unknown as { __persistCalled?: boolean }).__persistCalled = called;
          return orig();
        };
      }
    });

    await page.goto(RUNNER_ORIGIN);
    await pairViaUi(page, seed.pairing_code);

    // 3-pane layout visible
    await expect(page.getByTestId("reader-sidebar")).toBeVisible();
    await expect(page.getByTestId("reader-item-list")).toBeVisible();

    // Verify storage.persist() was invoked on startup
    const persistCalled = await page.evaluate(
      () => (window as unknown as { __persistCalled?: boolean }).__persistCalled === true,
    );
    expect(persistCalled).toBe(true);

    // Verify sort headers work (Date, Feed, Relevance)
    await expect(page.getByTestId("sort-btn-date")).toBeVisible();
    await expect(page.getByTestId("sort-btn-feed")).toBeVisible();
    await expect(page.getByTestId("sort-btn-relevance")).toBeVisible();
    await page.getByTestId("sort-btn-feed").click();
    await page.getByTestId("sort-btn-date").click();

    // Select first item via keyboard 'j', inspect reading pane and external link attributes
    await page.keyboard.press("j");
    await expect(page.getByTestId("reading-pane")).toBeVisible();
    const extLink = page.getByTestId("open-external-btn");
    await expect(extLink).toHaveAttribute("target", "_blank");
    await expect(extLink).toHaveAttribute("rel", "noopener noreferrer");

    // Keyboard shortcuts: 'k', 'j', 's' (star), 'm' (toggle read), '/' (focus search)
    await page.keyboard.press("j");
    await page.keyboard.press("k");
    await page.keyboard.press("s");
    await page.keyboard.press("m");

    // Focus search via '/' and filter
    await page.keyboard.press("/");
    await expect(page.getByTestId("global-search-input")).toBeFocused();
    await page.getByTestId("global-search-input").fill("PostgreSQL");
    await expect(page.getByTestId("search-mode-badge")).toContainText("Full Archive");
    await page.getByTestId("global-search-input").fill("");

    // Switch smart filter to Starred
    await page.getByTestId("smart-filter-starred").click();
    await expect(page.getByTestId("reader-item-list")).toContainText("Article");

    // Wait for debounced outbox sync to flush cleanly to PostgreSQL
    await page.waitForFunction(
      () => document.querySelector('[data-testid="pending-outbox-badge"]') === null,
      { timeout: 10_000 },
    );
  });

  test("1c-T10: detached offline reload survival, offline mutations, 500-mutation <5s flush & SW update banner", async ({
    page,
  }) => {
    const seed = resetAndSeed({ items: 6 });

    await page.goto(RUNNER_ORIGIN);
    await pairViaUi(page, seed.pairing_code);

    // Wait for Service Worker to control the page before disconnecting
    await page.waitForFunction(
      async () => {
        if (!("serviceWorker" in navigator)) return false;
        const reg = await navigator.serviceWorker.ready;
        return Boolean(reg.active && navigator.serviceWorker.controller);
      },
      { timeout: 15_000 },
    );

    const pageErrors: string[] = [];
    page.on("pageerror", (err) => pageErrors.push(err.message));

    // Stop the API server AND the localhost runner? No: simulate LAN API unreachable while
    // localhost runner stays up OR even offline browser! First stop the LAN API server to test
    // true detached LAN outage, AND also verify SW serves shell with browser setOffline(true)!
    await api!.stop();
    api = null;

    // Trigger sync -> transitions to Detached
    await page.getByTestId("manual-sync-btn").click();
    await expect(page.getByTestId("connection-status-pill")).toContainText("Detached");

    // Also set Chromium offline so even localhost runner cannot answer -> proves SW precache serves shell!
    await page.context().setOffline(true);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("reader-sidebar")).toBeVisible();
    await expect(page.getByTestId("connection-status-pill")).toContainText("Detached");
    expect(pageErrors).toEqual([]);

    // Online-only Add Feed button is disabled while detached
    await expect(page.getByTestId("sidebar-add-feed-btn")).toBeDisabled();

    // Run detached local MiniSearch
    await page.getByTestId("global-search-input").fill("PostgreSQL");
    await expect(page.getByTestId("search-mode-badge")).toContainText("Local");
    await expect(page.getByTestId("reader-item-list")).toContainText("PostgreSQL");
    await page.getByTestId("global-search-input").fill("");

    // Perform offline mutations: toggle read, toggle star, feed unsubscribe/mute, bulk mark-read
    const item0Id = seed.item_ids[0]!;
    const item2Id = seed.item_ids[2]!;
    await page.getByTestId(`item-row-${item0Id}`).click();
    await page.getByTestId("toggle-read-btn").click();
    await page.getByTestId("toggle-star-btn").click();

    // Mute beta feed via quick actions on an item from beta feed (idx=1 or 3)
    const betaItemId = seed.item_ids[1]!;
    await page.getByTestId(`item-row-${betaItemId}`).click();
    await page.getByTestId("feed-quick-actions-btn").click();
    await page.getByTestId("quick-feed-status-muted").click();

    // Bulk mark read (All in scope) and confirm in dialog
    await page.getByTestId("smart-filter-all").click();
    await page.getByTestId("bulk-mark-read-menu-btn").click();
    await page.getByTestId("bulk-read-all-btn").click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: /Mark Read/i })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // Pending badge shows all 4 queued mutations (read, star, mute, bulk-read)
    await expect(page.getByTestId("pending-outbox-badge")).toContainText("4");

    // Hard reload AGAIN while offline -> outbox & optimistic UI survive!
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("pending-outbox-badge")).toBeVisible();
    expect(pageErrors).toEqual([]);

    // Restore network + restart LAN API server
    api = await startApiServer({
      port: API_PORT,
      allowedHosts: `localhost:${API_PORT},127.0.0.1:${API_PORT}`,
      corsAllowOrigins: RUNNER_ORIGIN,
    });
    await page.context().setOffline(false);

    // Wait for automatic reconnect & outbox drain to 0
    await expect(page.getByTestId("connection-status-pill")).toContainText("Online", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("pending-outbox-badge")).toHaveCount(0, { timeout: 15_000 });

    // Verify PostgreSQL persisted all offline mutations cleanly
    const dbState = queryDbState();
    expect(dbState.items[item0Id]?.state).toBe("read");
    expect(dbState.items[item0Id]?.is_starred).toBe(true);
    expect(dbState.items[item2Id]?.state).toBe("read");
    expect(dbState.feeds[seed.feed_ids.beta]?.status).toBe("muted");

    // --- 1c-T10 (b): Timed 500-mutation flush from offline outbox completes in < 5.0 s (NFR-SYNC-02) ---
    await page.context().setOffline(true);
    await page.evaluate(async (itemIds: string[]) => {
      const openReq = indexedDB.open("smartrss_v1");
      const idb = await new Promise<IDBDatabase>((resolve, reject) => {
        openReq.onsuccess = () => resolve(openReq.result);
        openReq.onerror = () => reject(openReq.error);
      });
      const tx = idb.transaction("outbox", "readwrite");
      const store = tx.objectStore("outbox");
      const nowIso = new Date().toISOString();
      for (let i = 0; i < 500; i++) {
        const targetId = itemIds[i % itemIds.length]!;
        const mutId = crypto.randomUUID().toLowerCase();
        store.add({
          client_mutation_id: mutId,
          type: "item_set_starred",
          payload: {
            type: "item_set_starred",
            client_mutation_id: mutId,
            client_timestamp: nowIso,
            item_id: targetId,
            is_starred: i % 2 === 0,
          },
          created_at: nowIso,
          attempts: 0,
          last_error: null,
        });
      }
      await new Promise<void>((resolve, reject) => {
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
      idb.close();
    }, seed.item_ids);

    await page.context().setOffline(false);
    const t0 = Date.now();
    await page.getByTestId("manual-sync-btn").click();
    await page.waitForFunction(
      async () => {
        const openReq = indexedDB.open("smartrss_v1");
        const idb = await new Promise<IDBDatabase>((resolve, reject) => {
          openReq.onsuccess = () => resolve(openReq.result);
          openReq.onerror = () => reject(openReq.error);
        });
        const tx = idb.transaction("outbox", "readonly");
        const count = await new Promise<number>((resolve, reject) => {
          const req = tx.objectStore("outbox").count();
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => reject(req.error);
        });
        idb.close();
        return count === 0;
      },
      { timeout: 10_000 },
    );
    const flushMs = Date.now() - t0;
    expect(flushMs).toBeLessThan(5_000);

    // --- 1c-T10 (c): Serving updated /sw.js triggers "Update available — Reload" banner and activates ---
    const swPath = path.join(tmpDist!, "sw.js");
    fs.appendFileSync(swPath, "\n// smartrss-e2e-sw-bump-v2\n", "utf-8");
    await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      await reg?.update();
    });
    await expect(page.getByTestId("sw-update-banner")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("sw-update-reload-btn").click();
    await page.waitForLoadState("load");
    await expect(page.getByTestId("connection-status-pill")).toContainText("Online", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("sw-update-banner")).toHaveCount(0);
  });

  test("1c-T11: rejection rollback UX (category_not_found) & clock-drift warning banner", async ({
    page,
  }) => {
    const seed = resetAndSeed({ items: 4 });
    await page.goto(RUNNER_ORIGIN);
    await pairViaUi(page, seed.pairing_code);

    // Go offline and move Alpha Engineering to TempCategory via quick actions
    await page.context().setOffline(true);
    const alphaItemId = seed.item_ids[0]!;
    await page.getByTestId(`item-row-${alphaItemId}`).click();
    await page.getByTestId("feed-quick-actions-btn").click();
    await page.getByTestId("quick-feed-category-select").selectOption(seed.category_ids.temp);

    // Delete TempCategory on the backend while client is detached
    deleteCategoryInDb(seed.category_ids.temp);

    // Intercept sync/pull response to also simulate > 5 min clock drift on reconnect
    await page.route(`${API_ORIGIN}/api/v1/sync/pull*`, async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as Record<string, unknown>;
      body.server_now = new Date(Date.now() + 10 * 60 * 1000).toISOString();
      await route.fulfill({ response, json: body });
    });

    // Reconnect and sync -> push returns rejected (category_not_found)
    await page.context().setOffline(false);
    await page.getByTestId("manual-sync-btn").click();

    // Rejection notification toast appears and feed reverts cleanly to Tech category
    await expect(page.getByTestId("sync-notification-toast")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("sync-notification-toast")).toContainText(
      "Target category was deleted",
    );

    // Clock-drift warning banner renders because server_now is 10 min ahead
    await expect(page.getByTestId("clock-drift-banner")).toBeVisible({ timeout: 15_000 });

    // Confirm DB kept Alpha Engineering in original Tech category
    const dbState = queryDbState();
    expect(dbState.feeds[seed.feed_ids.alpha]?.category_id).toBe(seed.category_ids.tech);
  });
});
