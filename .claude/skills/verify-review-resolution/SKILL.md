---
name: verify-review-resolution
description: Use when a SmartRSS review in docs/reviews/ is marked Addressed and the project owner asks Claude Code to verify that the findings were actually resolved. Re-checks every finding (fix present on all paths, regression test fails with the fix reverted, fixed lines covered, docs synced, make check green), records follow-ups, and sets Verified only if nothing remains.
---

# Verify Review Resolution (reviewer: Claude Code)

You are the **verifier** for a review that **Antigravity** has marked `Addressed`. Trust nothing in the resolution notes without checking it. In `1a`, both verification passes found real gaps: `N-01`–`N-11` for the design, and the `I-02` test that never reached the fix, the half-fixed `I-18`, a duplicate index row, and an unguarded error path for `I-04`.

Don't fix the gaps yourself unless the owner asks; record them as follow-ups. You may set the review to `Verified`. Never set `Approved` / `Implementation Approved`.

## Steps

1. **Load the review**: list every finding ID, its severity, and its claimed resolution. Note any findings with no resolution entry; each one is a gap.
2. **Run `make check`**: record the test count, total coverage, and the critical gate table. Compare them with the numbers the review recorded.
3. **For each finding, in order of severity**:
   - **Fix present?** Read the changed code at the cited location. Does it address the root cause the finding describes, not just the example given?
   - **All paths?** Search for sibling paths with the same shape (success, error, `304`/`410`/`429`, unexpected-exception hooks, the reaper, CLI variants) and confirm each one has the same guard.
   - **Test reaches the fix?** Check the `term-missing` coverage output for the fixed lines. For every **Blocker/Major**, do the revert-the-fix check yourself:
     1. Copy the file to the scratchpad.
     2. Disable the fix with a minimal edit.
     3. Run the single test and confirm it **fails**.
     4. Restore the file from the copy and confirm it passes.
     5. Confirm the restore with `grep`/`diff`. Never leave the tree modified.
   - **Docs synced?** The design-doc deviations, env comments, and index rows that the finding touches are updated.
   - Give the finding one verdict: ✅ resolved / ⚠️ partially resolved / ❌ not resolved / ⏸ deferred (with owner acceptance?).
4. **Repo-wide drift checks**:
   - Duplicate or stale rows in `docs/README.md` and `docs/design/README.md`.
   - Status headers that disagree with each other.
   - Every `CRITICAL_MODULES` entry matches design §12.
   - New untracked files that should or shouldn't be committed (secrets, caches).
5. **Record the results**: append `## Verification pass (<date>)` to the review, with:
   - a table of finding → verdict → evidence (test name, revert result, coverage);
   - follow-up items for each ⚠️/❌, using either the next unused ID prefix or `<ID>-F1`, and stating the problem and the expected fix;
   - residual risks that need owner acceptance.
6. **Set the status**:
   - **No ⚠️/❌ remain**: set the review header to `Verified (<date>)` and update its `docs/README.md` row. Tell the owner the milestone is ready for their approval decision.
   - **Otherwise**: leave the status at `Addressed — verification found follow-ups`. Hand back to Antigravity (`address-review-findings`).
7. **Report to the owner**: give the verdict counts, each follow-up in one line, and the `make check` summary. Commit only if the owner asks.
