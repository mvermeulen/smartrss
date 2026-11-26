import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(__dirname, "../../../..");
export const BACKEND_DIR = path.join(REPO_ROOT, "backend");
export const FRONTEND_DIR = path.join(REPO_ROOT, "frontend");
export const DIST_DIR = path.join(FRONTEND_DIR, "dist");
export const RUNNER_SCRIPT = path.join(REPO_ROOT, "client-runner", "smartrss_client_runner.py");
export const E2E_BACKEND_SCRIPT = path.join(__dirname, "e2e_backend.py");

export const DEFAULT_TEST_DB_URL =
  process.env.TEST_DATABASE_URL ??
  "postgresql+psycopg://smartrss_test:smartrss_test@127.0.0.1:5433/smartrss_test";

export interface SeedResult {
  pairing_code: string;
  category_ids: { tech: string; science: string; temp: string };
  feed_ids: { alpha: string; beta: string; broken?: string };
  item_ids: string[];
  dead_letter_job_ids: number[];
}

export interface DbStateSnapshot {
  sync_mutation_count: number;
  items: Record<string, { state: string; is_starred: boolean; title: string }>;
  feeds: Record<
    string,
    {
      title: string;
      status: string;
      category_id: string | null;
      config_version: number;
      curation_instructions: string;
    }
  >;
  jobs: Record<string, { status: string; job_type: string }>;
  devices: Record<string, { name: string; revoked_at: string | null }>;
}

function runBackendHelper<T>(args: string[]): T {
  const out = execFileSync(
    "uv",
    ["run", "--directory", BACKEND_DIR, "python", E2E_BACKEND_SCRIPT, ...args],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, TEST_DATABASE_URL: DEFAULT_TEST_DB_URL },
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  return JSON.parse(out.trim()) as T;
}

export function resetAndSeed(opts?: {
  items?: number;
  hostile?: boolean;
  healthFixtures?: boolean;
  includeExpired?: boolean;
}): SeedResult {
  const args = ["reset-and-seed", "--items", String(opts?.items ?? 6)];
  if (opts?.hostile) args.push("--hostile");
  if (opts?.healthFixtures) args.push("--health-fixtures");
  if (opts?.includeExpired) args.push("--include-expired");
  return runBackendHelper<SeedResult>(args);
}

export function createPairingCode(name = "Second Device", platform = "ubuntu"): string {
  return runBackendHelper<{ code: string }>(["pair-code", "--name", name, "--platform", platform])
    .code;
}

export function deleteCategoryInDb(categoryId: string): void {
  runBackendHelper(["delete-category", categoryId]);
}

export function bumpFeedVersionInDb(feedId: string, instructions: string): void {
  runBackendHelper(["bump-feed-version", feedId, "--instructions", instructions]);
}

export function queryDbState(): DbStateSnapshot {
  return runBackendHelper<DbStateSnapshot>(["query-state"]);
}

export async function waitForHttpReady(url: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await new Promise<boolean>((resolve) => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve((res.statusCode ?? 500) < 500);
      });
      req.on("error", () => resolve(false));
      req.setTimeout(1000, () => {
        req.destroy();
        resolve(false);
      });
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

export interface ManagedProcess {
  stop: () => Promise<void>;
}

export async function startApiServer(opts: {
  port: number;
  host?: string;
  allowedHosts: string;
  corsAllowOrigins: string;
}): Promise<ManagedProcess> {
  const bindHost = opts.host ?? "127.0.0.1";
  const child: ChildProcess = spawn(
    "uv",
    [
      "run",
      "--directory",
      BACKEND_DIR,
      "uvicorn",
      "smartrss.api.app:create_app",
      "--factory",
      "--host",
      bindHost,
      "--port",
      String(opts.port),
      "--log-level",
      "warning",
    ],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        DATABASE_URL: DEFAULT_TEST_DB_URL,
        ALLOWED_HOSTS: opts.allowedHosts,
        CORS_ALLOW_ORIGINS: opts.corsAllowOrigins,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });

  try {
    await waitForHttpReady(`http://127.0.0.1:${opts.port}/api/v1/health`);
  } catch (err) {
    child.kill("SIGTERM");
    throw new Error(`API server failed to start on :${opts.port}: ${String(err)}\n${stderr}`);
  }

  return {
    stop: async () => {
      if (child.exitCode === null) {
        child.kill("SIGTERM");
        await new Promise<void>((resolve) => {
          const timer = setTimeout(() => {
            child.kill("SIGKILL");
            resolve();
          }, 4000);
          child.once("exit", () => {
            clearTimeout(timer);
            resolve();
          });
        });
      }
    },
  };
}

export async function startClientRunner(opts: {
  port: number;
  apiOrigin: string;
  distDir?: string;
}): Promise<ManagedProcess> {
  const distDir = opts.distDir ?? DIST_DIR;
  const child: ChildProcess = spawn(
    "python3",
    [
      RUNNER_SCRIPT,
      "--port",
      String(opts.port),
      "--dist-dir",
      distDir,
      "--api-origin",
      opts.apiOrigin,
    ],
    {
      cwd: REPO_ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });

  try {
    await waitForHttpReady(`http://127.0.0.1:${opts.port}/client-config.json`);
  } catch (err) {
    child.kill("SIGTERM");
    throw new Error(`Client runner failed to start on :${opts.port}: ${String(err)}\n${stderr}`);
  }

  return {
    stop: async () => {
      if (child.exitCode === null) {
        child.kill("SIGTERM");
        await new Promise<void>((resolve) => {
          const timer = setTimeout(() => {
            child.kill("SIGKILL");
            resolve();
          }, 4000);
          child.once("exit", () => {
            clearTimeout(timer);
            resolve();
          });
        });
      }
    },
  };
}

/**
 * Starts the real Nginx `smartrss-web` Docker image serving `frontend/dist` and proxying
 * `/api/` to the host's ephemeral E2E FastAPI server, using the exact `deploy/nginx/nginx.conf`
 * and `deploy/nginx/security-headers.conf`.
 */
export async function startNginxWebContainer(opts: {
  httpPort: number;
  apiPort: number;
}): Promise<ManagedProcess> {
  const name = `smartrss-e2e-web-${opts.httpPort}-${Date.now()}`;
  const rawConf = fs.readFileSync(path.join(REPO_ROOT, "deploy", "nginx", "nginx.conf"), "utf-8");
  const patchedConf = rawConf.replace(
    "http://api:8000",
    `http://host.docker.internal:${opts.apiPort}`,
  );
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "smartrss-e2e-nginx-"));
  const confPath = path.join(tmpDir, "default.conf");
  const htmlDir = path.join(tmpDir, "html");
  fs.writeFileSync(confPath, patchedConf, "utf-8");
  fs.cpSync(DIST_DIR, htmlDir, { recursive: true });
  fs.copyFileSync(
    path.join(REPO_ROOT, "deploy", "nginx", "client-config.json"),
    path.join(htmlDir, "client-config.json"),
  );

  execFileSync(
    "docker",
    [
      "run",
      "-d",
      "--rm",
      "--name",
      name,
      "--add-host=host.docker.internal:host-gateway",
      "-p",
      `127.0.0.1:${opts.httpPort}:80`,
      "-v",
      `${confPath}:/etc/nginx/conf.d/default.conf:ro`,
      "-v",
      `${htmlDir}:/usr/share/nginx/html:ro`,
      "smartrss-web:latest",
    ],
    { stdio: "pipe" },
  );

  try {
    await waitForHttpReady(`http://127.0.0.1:${opts.httpPort}/client-config.json`);
  } catch (err) {
    try {
      execFileSync("docker", ["rm", "-f", name], { stdio: "ignore" });
    } catch {
      // ignore
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
    throw err;
  }

  return {
    stop: async () => {
      try {
        execFileSync("docker", ["rm", "-f", name], { stdio: "ignore" });
      } catch {
        // ignore
      }
      fs.rmSync(tmpDir, { recursive: true, force: true });
    },
  };
}

export async function pairViaUi(page: Page, pairingCode: string): Promise<void> {
  await page.locator("#pair-code").fill(pairingCode);
  await page.locator('button[type="submit"]').click();
  await page.getByTestId("connection-status-pill").waitFor({ state: "visible", timeout: 15_000 });
  await page.waitForFunction(
    () => {
      const pill = document.querySelector('[data-testid="connection-status-pill"]');
      return pill?.textContent?.includes("Online") === true;
    },
    { timeout: 15_000 },
  );
}
