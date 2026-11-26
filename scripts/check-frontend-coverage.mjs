#!/usr/bin/env node
/**
 * Two-tier frontend coverage gate (1c §12.1, Owner Decision Q-1).
 *
 * Enforces:
 *   - >= 90% line & branch coverage on the 6 critical sync & security modules
 *   - >= 80% line & branch coverage on the 4 core store & search modules
 *
 * Usage: node scripts/check-frontend-coverage.mjs frontend/coverage/coverage-summary.json
 */

import fs from "node:fs";
import path from "node:path";

const CRITICAL_90 = [
  "src/security/uuid.ts",
  "src/security/urlSafety.ts",
  "src/security/sanitizeHtml.ts",
  "src/sync/reconciliation.ts",
  "src/sync/outbox.ts",
  "src/sync/engine.ts",
];

const CORE_80 = [
  "src/db/schema.ts",
  "src/db/clientMeta.ts",
  "src/search/queryParser.ts",
  "src/search/localSearch.ts",
];

const summaryPath = process.argv[2] || "frontend/coverage/coverage-summary.json";
if (!fs.existsSync(summaryPath)) {
  console.error(`ERROR: Coverage summary JSON not found at ${summaryPath}`);
  process.exit(1);
}

const raw = JSON.parse(fs.readFileSync(summaryPath, "utf-8"));
const entries = Object.entries(raw).filter(([k]) => k !== "total");

function findEntry(relPath) {
  const normalizedTarget = relPath.replaceAll("\\", "/");
  for (const [key, val] of entries) {
    const normalizedKey = key.replaceAll("\\", "/");
    if (normalizedKey === normalizedTarget || normalizedKey.endsWith("/" + normalizedTarget)) {
      return val;
    }
  }
  return null;
}

const failures = [];

function checkGroup(label, files, threshold) {
  console.log(`Frontend ${label} coverage gate (>= ${threshold}% lines & branches):`);
  for (const rel of files) {
    const entry = findEntry(rel);
    if (!entry) {
      console.log(`  [FAIL] ${rel.padEnd(36)} MISSING`);
      failures.push(`MISSING ${rel}`);
      continue;
    }
    const linesPct = Number(entry.lines?.pct ?? 0);
    const branchesPct = Number(entry.branches?.pct ?? 0);
    const ok = linesPct >= threshold && branchesPct >= threshold;
    const marker = ok ? "OK  " : "FAIL";
    console.log(
      `  [${marker}] ${rel.padEnd(36)} lines ${linesPct.toFixed(2).padStart(6)}%  branches ${branchesPct.toFixed(2).padStart(6)}%`
    );
    if (!ok) {
      failures.push(
        `${rel}: lines=${linesPct.toFixed(2)}% branches=${branchesPct.toFixed(2)}% (need >= ${threshold}%)`
      );
    }
  }
}

checkGroup("critical sync/security", CRITICAL_90, 90);
checkGroup("core store/search", CORE_80, 80);

if (failures.length > 0) {
  console.error("\nFrontend coverage gate FAILED:");
  for (const f of failures) {
    console.error(`  - ${f}`);
  }
  process.exit(1);
}

console.log("All frontend critical (>= 90%) and core (>= 80%) coverage gates passed.");
