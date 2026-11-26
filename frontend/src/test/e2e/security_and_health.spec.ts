import { expect, test } from "@playwright/test";

import {
  pairViaUi,
  queryDbState,
  resetAndSeed,
  startApiServer,
  startClientRunner,
  type ManagedProcess,
} from "./helpers.ts";

const API_PORT = 18102;
const RUNNER_PORT = 14182;
const API_ORIGIN = `http://localhost:${API_PORT}`;
const RUNNER_ORIGIN = `http://localhost:${RUNNER_PORT}`;

test.describe("1c-T12 & 1c-T13: Hostile XSS/mXSS + CSP Proof & System Health / Metrics / Dead-Letter UI", () => {
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

  test("1c-T12: hostile XSS/mXSS neutralization, zero remote image requests, positive & negative CSP proof", async ({
    page,
  }) => {
    const seed = resetAndSeed({ items: 2, hostile: true });

    const blockedRequests: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes("blocked.invalid")) {
        blockedRequests.push(req.url());
      }
    });

    // Capture every securitypolicyviolation event in the document
    await page.addInitScript(() => {
      (
        window as unknown as { __cspViolations: Array<{ directive: string; uri: string }> }
      ).__cspViolations = [];
      document.addEventListener("securitypolicyviolation", (e) => {
        (
          window as unknown as { __cspViolations: Array<{ directive: string; uri: string }> }
        ).__cspViolations.push({
          directive: e.effectiveDirective,
          uri: e.blockedURI,
        });
      });
    });

    await page.goto(RUNNER_ORIGIN);
    await pairViaUi(page, seed.pairing_code);

    // Open the hostile item
    const hostileItemId = seed.item_ids[0]!;
    await page.getByTestId(`item-row-${hostileItemId}`).click();
    await expect(page.getByTestId("safe-html-body")).toBeVisible();

    // Verify safe relative link resolved properly and evil JS link neutralized
    const safeLink = page.getByTestId("safe-html-body").locator("a", {
      hasText: "Safe Relative Link",
    });
    await expect(safeLink).toHaveAttribute(
      "href",
      "https://alpha.example.com/2026/11/safe-relative",
    );
    await expect(safeLink).toHaveAttribute("target", "_blank");
    await expect(safeLink).toHaveAttribute("rel", "noopener noreferrer");

    // Verify zero <img> tags rendered and remote image placeholder shown
    expect(await page.getByTestId("safe-html-body").locator("img").count()).toBe(0);
    await expect(page.getByTestId("safe-html-body")).toContainText("Blocked Diagram");

    // Verify extracted text renders as inert plain text
    await page.getByTestId("load-extracted-btn").click();
    await expect(page.getByTestId("extracted-plain-text")).toContainText(
      "<script>ignored()</script>",
    );

    // Search for "Hostile" (matches item with literal <script>alert(1)</script> in title) -> inert snippet rendering
    await page.getByTestId("global-search-input").fill("Hostile");
    await expect(page.getByTestId("reader-item-list")).toContainText("Hostile");
    await expect(page.getByTestId("reader-item-list")).toContainText("<script>alert(1)</script>");

    // Assert zero XSS execution, zero requests to blocked.invalid, zero unexpected CSP violations
    const xssVal = await page.evaluate(
      () => (window as unknown as { __xssExecuted?: unknown }).__xssExecuted,
    );
    expect(xssVal).toBeUndefined();
    expect(blockedRequests).toEqual([]);

    const cspBeforeNegative = await page.evaluate(
      () =>
        (window as unknown as { __cspViolations: Array<{ directive: string; uri: string }> })
          .__cspViolations,
    );
    expect(cspBeforeNegative).toEqual([]);

    // Negative CSP proof: fetch to foreign origin MUST raise a securitypolicyviolation (connect-src)
    await page.evaluate(async () => {
      try {
        await fetch("https://other.example/blocked-by-csp");
      } catch {
        // Expected CSP block
      }
    });
    await page.waitForFunction(
      () =>
        (window as unknown as { __cspViolations: Array<{ directive: string; uri: string }> })
          .__cspViolations.length > 0,
      { timeout: 5_000 },
    );
    const cspAfterNegative = await page.evaluate(
      () =>
        (window as unknown as { __cspViolations: Array<{ directive: string; uri: string }> })
          .__cspViolations,
    );
    expect(
      cspAfterNegative.some(
        (v) => v.directive.startsWith("connect-src") && v.uri.includes("other.example"),
      ),
    ).toBe(true);
  });

  test("1c-T13: System Health, 24h/7d/30d stage metrics, feed cost, pipeline runs, severity-sorted feed health & dead-letter retry/dismiss", async ({
    page,
  }) => {
    const seed = resetAndSeed({ items: 3, healthFixtures: true });
    await page.goto(RUNNER_ORIGIN);
    await pairViaUi(page, seed.pairing_code);

    // Navigate to System Health view
    await page.getByTestId("nav-health").click();
    await expect(page.getByTestId("system-health-view")).toBeVisible();

    // Verify stage metrics table across 24h, 7d, 30d windows
    await expect(page.getByTestId("stage-metrics-table")).toContainText("poll_feed");
    await expect(page.getByTestId("stage-metrics-table")).toContainText("extract_content");

    await page.getByTestId("metrics-window-7d").click();
    await expect(page.getByTestId("stage-metrics-table")).toContainText("poll_feed");
    await page.getByTestId("metrics-window-30d").click();
    await expect(page.getByTestId("stage-metrics-table")).toContainText("poll_feed");

    // Verify per-feed cost table and pipeline runs history table
    await expect(page.getByTestId("feed-cost-table")).toBeVisible();
    await expect(page.getByTestId("pipeline-runs-table")).toContainText("partial");

    // Verify Feed Health table sorts broken before degraded before healthy
    const feedRows = await page
      .getByTestId("feed-health-table")
      .locator("tbody tr")
      .allTextContents();
    expect(feedRows.length).toBeGreaterThanOrEqual(3);
    expect(feedRows[0]).toContain("Broken Wire");
    expect(feedRows[1]).toContain("Beta Science");
    expect(feedRows[2]).toContain("Alpha Engineering");

    // Dead-letter table shows 2 dead-letter jobs; Retry job 1 and Dismiss job 2
    await expect(page.getByTestId("dead-letter-count")).toHaveText("2");
    const retryId = seed.dead_letter_job_ids[0]!;
    const dismissId = seed.dead_letter_job_ids[1]!;

    await page.getByTestId(`dl-retry-${retryId}`).click();
    await expect(page.getByTestId("health-action-status")).toContainText(`#${retryId}`);
    await expect(page.getByTestId("dead-letter-count")).toHaveText("1");

    await page.getByTestId(`dl-dismiss-${dismissId}`).click();
    await expect(page.getByTestId("health-action-status")).toContainText(`#${dismissId}`);
    await expect(page.getByTestId("dead-letter-count")).toHaveText("0");

    // Verify PostgreSQL reflects retry (queued) and dismiss (deleted from dead-letter queue)
    const dbState = queryDbState();
    expect(dbState.jobs[String(retryId)]?.status).toBe("queued");
    expect(dbState.jobs[String(dismissId)]).toBeUndefined();
  });
});
