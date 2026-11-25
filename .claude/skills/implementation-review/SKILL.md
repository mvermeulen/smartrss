---
name: implementation-review
description: Use when the project owner asks Claude Code to review a SmartRSS milestone implementation (code, tests, migrations, scripts, docs) against its approved component design. Runs make check, reproduces suspected bugs, walks the failure-class checklist, and writes a severity-ranked findings document in docs/reviews/. Claude is the reviewer only; Antigravity fixes.
---

# Implementation Review (reviewer: Claude Code)

You are the **reviewer**. **Antigravity** implemented the milestone and will fix the findings. The **project owner** approves. Don't fix the code yourself unless the owner asks. Never set `Implementation Approved`.

Process: [`docs/development-process.md` §7](../../../docs/development-process.md#7-review-gates). Checklist: [`docs/review-checklist.md`](../../../docs/review-checklist.md). Example: [`docs/reviews/2026-11-25-1a-implementation-review.md`](../../../docs/reviews/2026-11-25-1a-implementation-review.md).

## Steps

1. **Scope**: prefer a committed branch diff: `git log --oneline main..milestone/<id>` and `git diff --stat main...milestone/<id>`. If you were handed an uncommitted tree, note that in the header as a process deviation. Record the base and head commits.
2. **Baseline gates**: run `make check` and capture the test count, total coverage, and the critical-module gate table. A failing `make check` is itself a Blocker.
3. **Pick a finding-ID prefix** not yet used in `docs/reviews/`.
4. **Spec conformance**: read the approved component design section by section against the code. Every deviation is either a finding or must already be written back into the design doc.
5. **Walk `docs/review-checklist.md`** systematically, every row, over the diff. The highest-yield passes in `1a` were:
   - every third-party parser or library call that receives fetched data (S1);
   - every read → network/LLM → write path, including error and exception hooks (C1);
   - every `while True` / daemon loop and its `except` clauses (L1);
   - every "is anything still running?" query (C2);
   - every bulk SQL statement (D1);
   - every shell script that handles credentials (S3).
6. **Test quality**:
   - Map each row of the design's test-matrix section to a test. Missing rows are findings.
   - Read the coverage `Missing` column for error, shutdown, and fallback paths in the worker and stage modules. Uncovered error handling is where bugs hide (`I-02`).
   - Check for property tests where the design or `development-process.md` asks for them, and for recorded fixtures under `backend/tests/fixtures/`.
7. **Reproduce before asserting**: for each suspected Blocker or Major, try a minimal reproduction (a snippet in the project venv, a targeted test in the scratchpad). Mark each finding *verified by reproduction* or *found by code reading*.
8. **Docs**: status headers, `docs/README.md` rows, env-var comments, `README.md`, data-model and architecture drift.
9. **Write** `docs/reviews/YYYY-MM-DD-<milestone>-implementation-review.md` using the §7.2 format:
   - Outcome summary: what's solid, the severity table, and a recommendation on what must be fixed before committing or approving.
   - Findings with `path:line`, impact, a failure scenario, and the fix plus regression test.
   - A "Verification performed" section with the `make check` output and the reproductions.
   - Add one row to `docs/README.md` (status `Open`).
10. **Report to the owner**: give the counts and the blockers. The next step is for Antigravity to run `address-review-findings`, then for Claude to run `verify-review-resolution`.
