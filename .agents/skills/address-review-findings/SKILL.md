---
name: address-review-findings
description: Use when a SmartRSS review in docs/reviews/ has status Open (or has new verification-pass follow-ups) and the implementer must resolve its findings. Fixes each finding with a proven regression test (fails with the fix reverted), records per-finding resolutions and evidence in the review, syncs docs, and sets the review to Addressed for the reviewer to verify.
---

# Address Review Findings (implementer: Antigravity)

You are the **implementer** responding to a review written by **Claude Code**. You resolve the findings and record evidence. The reviewer verifies, and the project owner approves. You may set the review status to `Addressed`, but **never** to `Verified`, and never set a design or implementation to `Approved` / `Implementation Approved`.

Process reference: [`docs/development-process.md` §2.3 and §7](../../../docs/development-process.md#7-review-gates).

## Steps

1. **Triage**
   - Read the whole review. List every finding ID with its severity.
   - Blockers and Majors **must** be fixed. If you believe one is wrong or not worth fixing, don't skip it silently. Write a rebuttal under that finding and leave the decision to the owner ("Owner decision needed").
   - Minors and Nits: fix them, or mark them `Deferred → <milestone/issue>` with a reason.

2. **Fix each finding, one at a time, on the milestone branch.** For each fix:
   1. Write or adjust the regression test the review asks for (name it `test_<id>_<behavior>`, e.g. `test_i04_…`).
   2. Apply the fix.
   3. **Prove the test.** Temporarily revert or disable the fix, run *just that test*, and confirm it **fails for the expected reason**. Restore the fix and confirm it passes. A test that passes either way is not a regression test; rewrite it so it reaches the fixed code path.
   4. **Check coverage of the fix.** In the `pytest --cov-report=term-missing` output, the fixed lines must not appear as missing.
   5. Apply the same guard on **every** path with the same shape. For example, if the fix adds a check on the success path, look for the error, `304`, and unexpected-exception paths too. (In `1a`, `I-04` was first fixed on all paths but one.)
   6. If the finding reveals a new *class* of bug, add a row to `docs/review-checklist.md`.

3. **Sync the docs** (same commit as the fixes): design-doc deviations, `data-model.md`, `architecture.md`, `.env*.example` comments, `README.md`.

4. **Run `make check`.** It must pass.

5. **Record resolutions in the review file.** Append a `## Resolutions` section with one entry per finding:
   ```
   - **I-04** — Fixed. <one-line description of the change> (`path:line`).
     Test: `test_i04_…`. Evidence: reverted <what>, test failed (<assertion>);
     restored, passed. Fixed lines covered.
   - **I-15** — Fixed partially; residual risk: <…> → *Owner acceptance requested*.
   - **I-22** — Deferred to 1b (<reason>).
   ```
   Add the `make check` summary (test count, total coverage, critical gate).

6. **Set the status**: the review header becomes `Addressed (<date>) — awaiting verification`, and the review's row in `docs/README.md` is updated (same row, never a duplicate). Commit.

7. **Hand back**: tell the owner the review is ready for Claude Code's verification pass. List any findings that need an owner decision or residual-risk acceptance.

## Verification-pass follow-ups
If the reviewer's verification pass appends new findings (for example a `Follow-up verification` section or a new ID prefix), treat them exactly like the original findings: repeat steps 2–7 and append to `## Resolutions`.
