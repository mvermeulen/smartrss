---
name: verify-review-resolution
description: Use when a SmartRSS review in docs/reviews/ is marked Addressed and the project owner asks Claude Code to verify that the findings were actually resolved. Re-checks every finding (fix present on all paths, regression test fails with the fix reverted, fixed lines covered, docs synced, make check green), records follow-ups, and sets Verified only if nothing remains.
---

# Verify Review Resolution (reviewer: Claude Code)

You are the **verifier** for a review that **Antigravity** has marked `Addressed`. Trust nothing in the resolution notes without checking it. In `1a`, both verification passes found real gaps: `N-01`–`N-11` for the design, and the `I-02` test that never reached the fix, the half-fixed `I-18`, a duplicate index row, and an unguarded error path for `I-04`. In `1b`, the review had no Blockers or Majors, yet reverting a Minor fix (`K-01`'s OPML lock) left the full suite green and led to an unvalidated sibling input (`K-01-F1`).

Don't fix the gaps yourself unless the owner asks; record them as follow-ups. You may set the review to `Verified`. Never set `Approved` / `Implementation Approved`.

## Steps

1. **Load the review**: list every finding ID, its severity, and its claimed resolution. Note any findings with no resolution entry; each one is a gap.
2. **Run `make check`**: record the test count, total coverage, and the critical gate table. Compare them with the numbers the review recorded. A coverage difference under 0.1 point is normal run-to-run variation; a different test count is not.
3. **For each finding, in order of severity**:
   - **Fix present?** Read the changed code at the cited location. Does it address the root cause the finding describes, not just the example given?
   - **All paths?** Search for sibling paths with the same shape (success, error, `304`/`410`/`429`, unexpected-exception hooks, the reaper, CLI variants, bulk/import variants) and confirm each one has the same guard. Include *inputs* with the same shape: if the fix validates `category_id` on one path, look for every other request field that sets the same column (defaults, optional fields; `K-01-F1`). Reproduce any suspected gap with a temporary test, then delete it.
   - **Test reaches the fix?** Check the `term-missing` coverage output for the fixed lines. For **every finding with a code fix** (Nits optional), do the revert-the-fix check yourself, **once per fixed path**:
     1. Copy the file to the scratchpad.
     2. Disable that path's fix with a minimal edit (one path at a time).
     3. Run the cited test and confirm it **fails**. If the resolution names no test for that path, run the **full** suite: if it stays green, the path is untested (a follow-up).
     4. Restore the file from the copy and confirm it passes.
     5. Confirm the restore with `grep`/`diff`. Never leave the tree modified.
   - **Docs synced?** The design-doc deviations, env comments, and index rows that the finding touches are updated.
   - Give the finding one verdict: ✅ resolved / ⚠️ partially resolved / ❌ not resolved / ⏸ deferred (with owner acceptance?).
4. **Repo-wide drift checks**:
   - Duplicate or stale rows in `docs/README.md` and `docs/design/README.md`.
   - Status headers that disagree with each other.
   - Every `CRITICAL_MODULES` entry matches the critical-modules section of each approved design (`1a` §12, `1b` §10.1).
   - New untracked files that should or shouldn't be committed (secrets, caches).
5. **Record the results**: append `## Verification pass (<date>)` to the review, with:
   - a table of finding → verdict → evidence (test name, revert result, coverage);
   - follow-up items for each ⚠️/❌, using either the next unused ID prefix or `<ID>-F1`, and stating the problem and the expected fix;
   - residual risks that need owner acceptance.
6. **Set the status**:
   - **No ⚠️/❌ remain**: set the review header to `Verified (<date>)` and update its `docs/README.md` row. Tell the owner the milestone is ready for their approval decision. If the owner then approves, update all five status locations in one commit ([development-process.md §4.1](../../../docs/development-process.md#41-approval-mechanism-coding-gate)).
   - **Otherwise**: leave the status at `Addressed — verification found follow-ups`. Hand back to Antigravity (`address-review-findings`).
7. **Report to the owner**: give the verdict counts, each follow-up in one line, and the `make check` summary. Commit only if the owner asks.
