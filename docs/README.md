# SmartRSS Documentation

This directory holds the requirements, architecture, design specifications, ADRs, and review records for **SmartRSS**.

## Document Status

| Document | Purpose | Status |
| :--- | :--- | :--- |
| [Requirements](requirements.md) | User stories, requirement IDs, acceptance criteria, non-goals, and milestones (`1a`–`1g`) | Approved |
| [Architecture](architecture.md) | Container topology, `LLMAdapter`, capacity budgets, security, and backup/restore ops | Approved |
| [Data Model](data-model.md) | Entities, state machines, sync surface, per-field conflict rules, job queue, and lifecycle | Approved |
| [Component Designs](design/README.md) | Milestone-sequenced detailed subsystem specifications (`1a`–`1g`) | In Progress (`1a` Draft) |
| [Decisions (ADRs)](decisions/README.md) | Architecture Decision Records (`0001`–`0006` Accepted) | Accepted |
| [Development Process](development-process.md) | Testing standards, invariant matrix, coverage, regression policy, doc hygiene, and approvals | Approved |
| [Claude Design Review (2026-11-22)](reviews/2026-11-22-initial-design-review.md) | Initial design review (`R-01`–`R-34`, `Q-01`–`Q-11`) with recorded resolutions | Addressed |
| [Copilot Design Review (2026-11-22)](reviews/2026-11-22-high-level-design-review.md) | High-level design review (`B-01`–`B-05`, `M-01`–`M-14`, minors) with recorded resolutions | Addressed |
| [Second-Pass Design Review (2026-11-23)](reviews/2026-11-23-second-pass-design-review.md) | Verification pass on prior resolutions; `S-01`/`S-02` addressed in ADRs 0004/0006, `S-03`–`S-07` carried into milestone component designs | Addressed |
| [`1a` Component Design Review (2026-11-24)](reviews/2026-11-24-1a-component-design-review.md) | Review of the `1a` component design (`A-01`–`A-33`): 6 blockers, 12 major, 15 minor | Open |
