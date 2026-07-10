---
name: qa-sweep
description: >
  Apply when implementing features, fixing bugs, debugging errors, investigating failures,
  tracing root causes, reviewing tech debt, tracing issues, planning fixes, or completing
  any task. Enforces parallel sub-agent implementation, independent adversarial review,
  and a 95% confidence gate before stopping.
---

## QA & Independent Review Protocol

Follow this protocol on every implementation, fix, debugging, or review task.

### Phase 1 — Parallel Implementation
- Use parallel sub-agents to speed up independent units of work wherever possible when the active tool policy and user request allow delegation.
- If sub-agents are unavailable or not authorized, perform the same independent explorer/reviewer/critic passes locally and label them as local fallback passes.
- Each sub-agent must read relevant source code end-to-end before making changes.
- Reference official documentation to verify whether any behavior is intended before treating it as a bug.
- Do not trust assumptions — prove every behavior against actual code.

### Phase 2 — Independent Adversarial Review (Mandatory)
After implementation, spawn a FRESH sub-agent that has not participated in any prior work when sub-agents are authorized. Give it this directive verbatim:
> "Assume all work done by the implementing agent is incorrect until you can prove otherwise with
> absolute evidence from the actual code. The implementing agent makes frequent mistakes and tends
> to miss edge cases. Do not trust any claim without tracing it yourself. Review every change,
> test, and edge case end-to-end through the real source."

If sub-agents are not authorized, run this as a local fallback adversarial review and disclose that fallback.

The review agent must:
- Independently trace each change end-to-end through the codebase
- Search for related issues and regressions the implementing agent may have introduced
- Verify documented behavior vs. actual code behavior
- Surface every edge case not explicitly covered
- Treat external PR review findings as claims until independently validated; when they affect a PR update or publish flow, route accepted follow-up through `commit-pr`.

### Phase 3 — Completeness Verification
Spawn a SECOND independent agent to verify original planned work vs. delivered work when authorized:
> "Assume nothing was completed correctly or fully. Map every originally planned item to actual
> code changes and verify each one independently. Do not trust the implementing agent's report."

If sub-agents are not authorized, run this as a separate local fallback completeness pass before reporting completion.

- **Regression-test baseline verification:** For every new regression test, identify the **pre-fix commit or baseline hash** (issue body, plan acceptance criteria, or "fails on commit X" line). Run the test against that baseline to confirm it fails, then run it against the fix branch to confirm it passes. Use `HEAD~1` ONLY when the fix is a single commit immediately before the test; in multi-commit or merge-batch fixes, prefer the documented pre-fix SHA. A regression test that does not fail under the documented pre-fix baseline is not a regression guard — it is theater.

### Stop Condition
Do NOT stop until ≥95% confident that:
- All issues, related issues, and edge cases are covered
- All review agent findings have been addressed
- Delivered work matches the original plan completely

If below 95%, state what remains and continue working.
