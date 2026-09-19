# Claude Code Swarm Mode

Normal behavior is the default.

If `.claude/session/swarm-mode.md` exists, swarm mode is enabled for the current session and you must read that file before starting complex work.

When swarm mode is enabled:
- Quality is the only success metric.
- There is no time pressure.
- Do not compress a workflow just because the task is large.
- Prefer parallel subagents for disjoint investigation and review work.
- Keep implementation, validation, and final judgment in separate contexts when possible.
- Explorer-style work is for breadth and candidate generation.
- Reviewer-style work is for validation of candidate findings or implementation quality.
- Critic-style work is for final challenge of reviewer-confirmed findings or high-impact implementation conclusions.
- Do not let the same context both invent and approve a finding when a separate verification pass is possible.
- No approval without positive evidence of what was checked.
- No high-severity finding without exact evidence and, when relevant, runtime-aware verification.
- Preserve Claude Code speed by parallelizing broadly and reserving the deepest validation for high-risk or ambiguous work.
- Across many different repositories, explore local patterns first rather than assuming one project's conventions apply to another.

If `.claude/session/swarm-mode.md` does not exist, behave normally.

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## Shared repository instructions

The repository's engineering conventions, CI gates, skill layout, and testing
policy are maintained once in the root `AGENTS.md` and imported here, so Claude
Code resolves the same instructions as Codex (which reads `AGENTS.md` natively):

@AGENTS.md

## Claude Code specifics

- **Frontend tests:** for jsdom gotchas (router context for `<Link>`, driving
  Radix `Select`, virtualized lists), see
  `.claude/skills/ci-compatibility-audit/references/frontend-testing-gotchas.md`
  for the repo's established mock patterns before improvising.
