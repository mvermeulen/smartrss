import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./src/test/e2e",
  timeout: 60_000,
  expect: {
    timeout: 12_000,
  },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    headless: true,
    viewport: { width: 1440, height: 900 },
    actionTimeout: 10_000,
    trace: "retain-on-failure",
    launchOptions: {
      args: [
        "--host-resolver-rules=MAP smartrss.test 127.0.0.1, MAP api.smartrss.test 127.0.0.1",
      ],
    },
  },
});
