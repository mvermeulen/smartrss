---
name: design-review
description: Use when the project owner asks Claude Code to review a SmartRSS component design (docs/design/*) or ADR before approval. Produces a findings document in docs/reviews/ with severity-ranked, uniquely-ID'd findings and suggested fixes. Claude is the reviewer only; Antigravity resolves findings.
---

# Design Review (reviewer: Claude Code)

You are the **reviewer**. **Antigravity** is the designer/implementer, and the **project owner** approves. Write findings; don't rewrite the design yourself unless the owner asks. Never set a design to `Approved`.

Process: [`docs/development-process.md` §7](../../../docs/development-process.md#7-review-gates). Checklist: [`docs/review-checklist.md`](../../../docs/review-checklist.md). Example: [`docs/reviews/2026-11-24-1a-component-design-review.md`](../../../docs/reviews/2026-11-24-1a-component-design-review.md).

## Steps

1. **Scope**: note the design doc, its status, and the commit (`git rev-parse HEAD`, or the branch head). Read the design together with `requirements.md`, `architecture.md`, `data-model.md`, the referenced ADRs, `development-process.md`, and earlier milestone designs it builds on.
2. **Pick a finding-ID prefix** not yet used in `docs/reviews/`: `grep -ho '^### [A-Z]-' docs/reviews/*.md | sort -u`.
3. **Review for**:
   - **Requirements coverage**: every requirement ID the design claims is actually satisfied, and nothing in scope is missing.
   - **Consistency**: entities, state machines, and sync rules agree with `data-model.md` and the ADRs. Note anything that needs a doc update in the same commit.
   - **State machines and transactions**: every transition and failure path is defined. Check for a slow I/O call inside a DB transaction, a dead-letter or retry loop, and work that can never complete.
   - **Concurrency**: for each read → I/O → write sequence, ask what happens if the user edits the same row meanwhile ([checklist C1–C6](../../../docs/review-checklist.md#2-concurrency--state)).
   - **Security**: the ADR 0006 boundaries and [checklist §1](../../../docs/review-checklist.md#1-security-boundaries). Verify library claims (for example "httpx caps decompression") instead of trusting them.
   - **Test plan**: invariants from `development-process.md` §2.4 are covered, critical modules are listed for the coverage gate, and hostile fixtures are planned.
   - **Operability**: migrations, backup/restore, cron vs daemon modes, configuration defaults.
4. **Verify claims cheaply where possible**: read the library source in `backend/.venv`, run small snippets, check PostgreSQL semantics. Say which claims you verified and which you only inferred.
5. **Write** `docs/reviews/YYYY-MM-DD-<milestone>-design-review.md` using the §7.2 format:
   - Header (date, scope with commit, specs checked, reviewer = "Claude Code (<model>)", status `Open`).
   - **Review outcome**: what's solid, the main risks, a severity table, and a recommendation.
   - Findings grouped by Blocker / Major / Minor / Nit. For each one: the location (§ or `path:line`), the problem, a concrete failure scenario, and a suggested fix, including the test to add.
   - Questions or trade-offs that need an owner decision, listed separately.
6. **Add one row** to the `docs/README.md` index (status `Open`).
7. **Report to the owner**: give the counts by severity and the top 3 issues. The next step is for Antigravity to run `address-review-findings`. Commit only if the owner asks.
