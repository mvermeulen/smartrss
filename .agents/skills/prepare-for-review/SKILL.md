---
name: prepare-for-review
description: Use when a SmartRSS component design draft or a milestone implementation is complete and ready to hand to the reviewer (Claude Code). Self-checks against the review checklist, runs make check, fixes doc and status drift, commits to the milestone branch, and writes a review-request summary for the project owner.
---

# Prepare for Review (implementer: Antigravity)

You are the **designer/implementer**. The **reviewer is Claude Code**, invoked by the project owner. Your job is to hand over work that is complete, verifiable, and has a stable, committed scope. Do **not** write the review yourself, and never set any status to `Approved` / `Implementation Approved`.

Process reference: [`docs/development-process.md` §7](../../../docs/development-process.md#7-review-gates). Checklist: [`docs/review-checklist.md`](../../../docs/review-checklist.md).

## Steps

1. **Pin the scope**
   - Work on a branch `milestone/<id>` (for example `milestone/1b`). Commit all work in focused commits (feature + tests + docs together). The reviewer should review a **committed diff**, not an uncommitted tree.
   - Record the base commit: `git merge-base main HEAD`.

2. **Self-check against `docs/review-checklist.md`**
   - Walk every section that applies (security, concurrency, long-running processes, DB, encoding, tests/docs).
   - For each hit, fix it now. Don't leave known issues for the reviewer to find.

3. **Spec conformance**
   - Re-read the milestone's component design. Every deviation must be either reverted or written back into the design doc (same commit).
   - Every row of the design's test-matrix section (§"Verification & Test Plan") maps to at least one named test.
   - The `CRITICAL_MODULES` list in `scripts/check-critical-coverage.py` matches the design's critical-module list.

4. **Run the gates**
   - `make check` must pass. Capture the summary lines: test count, total coverage, critical-module gate table.

5. **Docs and status**
   - Design-only hand-off: set the design header to `In Design Review`.
   - Implementation hand-off: set it to `Implemented` (code done) and then `In Implementation Review`.
   - Update the matching row in `docs/README.md` and `docs/design/README.md`. Update the row; never add a duplicate.
   - `AGENTS.md` "Project Status" stays accurate.

6. **Write the review request** (print it for the owner; don't create a review file):
   ```
   Review request: <design|implementation> of milestone <id>
   Branch: milestone/<id>   Base: <sha>   Head: <sha>
   Scope: <paths>
   Specs: <design doc>, <ADRs/data-model sections touched>
   make check: <N> passed, total <X>%, critical gate OK (<lowest module> <Y>%)
   Known deviations from design (documented in): <list or "none">
   Known open risks / questions for reviewer: <list>
   ```

## Don'ts
- Don't approve, verify, or close anything yourself.
- Don't push to `main`. The branch merges to `main` only after the owner sets `Implementation Approved`.
- Don't hand off with failing `make check`, uncommitted changes, or TODOs inside the scope.
