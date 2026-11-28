#!/usr/bin/env python3
"""Enforce >= 90% branch+line coverage on every critical core module (1a §12, 1b §10.1, I-08)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CRITICAL_MODULES: tuple[str, ...] = (
    # Milestone 1a
    "src/smartrss/db/sync_log.py",
    "src/smartrss/security/safe_http.py",
    "src/smartrss/security/auth.py",
    "src/smartrss/security/middleware.py",
    "src/smartrss/domain/url_canon.py",
    "src/smartrss/domain/backoff.py",
    "src/smartrss/domain/expiration.py",
    "src/smartrss/extract/client.py",
    "src/smartrss/jobs/queue.py",
    "src/smartrss/jobs/locks.py",
    "src/smartrss/worker/stages/poll_feed.py",
    "src/smartrss/worker/stages/expire_items.py",
    # Milestone 1b
    "src/smartrss/domain/sync_merge.py",
    "src/smartrss/domain/fts_text.py",
    "src/smartrss/sync/service.py",
    "src/smartrss/search/fts.py",
    # Milestone 1d-ops
    "src/smartrss/feeds/service.py",
)

MIN_CRITICAL_PCT = 90.0


def main() -> int:
    cov_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backend/coverage.json")
    if not cov_path.is_file():
        print(f"ERROR: Coverage JSON not found at {cov_path}", file=sys.stderr)
        return 1

    data = json.loads(cov_path.read_text(encoding="utf-8"))
    files = data.get("files", {})

    failures: list[str] = []
    print(f"Critical module coverage gate (>= {MIN_CRITICAL_PCT:.0f}%):")
    for mod in CRITICAL_MODULES:
        entry = files.get(mod)
        if entry is None:
            failures.append(f"  MISSING {mod}")
            continue
        pct = float(entry["summary"]["percent_covered"])
        marker = "OK  " if pct >= MIN_CRITICAL_PCT else "FAIL"
        print(f"  [{marker}] {mod:<46} {pct:6.2f}%")
        if pct < MIN_CRITICAL_PCT:
            failures.append(f"{mod}: {pct:.2f}% < {MIN_CRITICAL_PCT:.0f}%")

    if failures:
        print("\nCritical module coverage gate FAILED:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print("All critical core modules meet the >= 90% coverage gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
