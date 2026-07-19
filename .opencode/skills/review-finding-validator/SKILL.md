---
name: review-finding-validator
description: Validate external reviewer, CI, audit, swarm, or PR findings as claims before implementing or reporting them. Use when given a bundle of review findings, requested-changes comments, audit output, or suspected regressions that must be classified with evidence.
effort: medium
---

# Review Finding Validator

Use this skill to classify review findings before treating them as true.

## Operating Rule

Every finding is a claim until the current worktree proves it. Do not implement unless the user explicitly asks for fixes.

## Required Classifications

Classify each item as exactly one:

- `CONFIRMED`: direct source, diff, runtime, or test evidence proves the finding.
- `PARTIALLY_VALID`: part of the claim is true, but severity, scope, or fix direction is overstated.
- `DISPROVED`: current code, config, docs, or tests contradict the claim.
- `UNVERIFIED`: evidence is insufficient or blocked.
- `PRE_EXISTING`: valid, but not introduced by the current branch/diff.
- `OUT_OF_SCOPE`: valid or plausible, but unrelated to the requested change.

## Workflow

1. Establish the reviewed scope:
   - current branch and base branch
   - PR number or URL if available
   - `git diff --stat <base>...HEAD` when reviewing branch changes
   - current dirty files if reviewing local changes
2. For every finding, open the cited file and surrounding context.
3. Search sibling files and callers when the claim depends on runtime wiring.
4. Prefer behavior-level validation over source-string checks.
5. Run the smallest safe command when the claim depends on execution behavior.
6. Record evidence before deciding classification.
7. Fix only `CONFIRMED` or accepted `PARTIALLY_VALID` items when the user asked for implementation.

## Output Format

For each finding:

```text
ID:
Classification:
Evidence:
Decision:
Minimal fix target:
```

Keep rejected findings short, but include the exact reason they were rejected.

## Guardrails

- Do not rely on stale review line numbers without checking the current file.
- Do not assume a file exists because the review cited it.
- Do not accept tests as proof when they assert only source strings or mocks.
- Do not broaden the patch to adjacent cleanup unless needed to resolve a confirmed finding.
