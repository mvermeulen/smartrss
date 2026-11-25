# Project Skills

Add reusable Antigravity skills for this repository here:

```text
.agents/skills/<skill-name>/
├── SKILL.md          # Required: YAML frontmatter (name, description) + instructions
├── references/       # Optional: detailed reference docs
└── scripts/          # Optional: helper scripts
```

Skills are discovered automatically and loaded on demand based on their `name` and `description`.

## Roles

**Antigravity** is the SmartRSS designer/implementer and uses the skills in this directory. **Claude Code** is the reviewer; its reviewer skills live in [`.claude/skills/`](../../.claude/skills/). See [`docs/development-process.md` §7](../../docs/development-process.md#7-review-gates).

## Available Skills

| Skill | Use when |
| :--- | :--- |
| [`prepare-for-review`](prepare-for-review/SKILL.md) | A design draft or milestone implementation is ready to hand to the reviewer |
| [`address-review-findings`](address-review-findings/SKILL.md) | A review in `docs/reviews/` is `Open`, or verification found follow-ups |
