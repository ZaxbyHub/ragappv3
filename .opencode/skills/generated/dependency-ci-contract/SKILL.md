---
name: dependency-ci-contract
description: Keep dependency and lockfile changes aligned with CI expectations.
generated_from_knowledge:
  - f178f314-bce2-474b-bb4f-091d6662b5f7
  - d38f4988-551c-4d4d-b4b2-be6dc0d75c30
source_knowledge_ids:
  - f178f314-bce2-474b-bb4f-091d6662b5f7
  - d38f4988-551c-4d4d-b4b2-be6dc0d75c30
generated_at: 2026-07-08T07:30:00.000Z
confidence: 0.60
status: active
version: 1
skill_origin: generated
---

# Dependency and CI Contract

## Trigger

- Changing dependency versions
- Updating package.json
- Regenerating lockfiles
- Investigating test/build failures after dependency changes

## Required Procedure

- Run typecheck and build locally and confirm they pass before presenting code for review.
- Verify node_modules matches the lockfile before reporting test failures.
- Re-run failed CI gates after dependency sync before escalating.

## Forbidden Shortcuts

- Change dependency versions that cause typecheck or build to fail.
- Report test failures as code defects before confirming the environment matches the lockfile baseline.

## Delegation Template

When delegating a task affected by this skill, include:

```
SKILLS: file:.opencode/skills/generated/dependency-ci-contract/SKILL.md
```

## Reviewer Checks

- Verify typecheck and build pass after dependency changes.
- Verify environment consistency before approving failure attribution.

## Test Engineer Checks

- Confirm node_modules hash matches baseline before running tests.
- Confirm TypeScript version matches project requirement before trusting test results.
- Run the test suite on a clean install before flagging implementation as the cause of test failure.

## Source Knowledge IDs

- f178f314-bce2-474b-bb4f-091d6662b5f7 — Dependency version changes must not break CI gates. If typecheck or build cannot run after a change, the change is invalid and must be reverted before review.
- d38f4988-551c-4d4d-b4b2-be6dc0d75c30 — Run `npm ci` or verify lockfile sync before attributing test/build failures to code changes; missing dependencies produce spurious failures that waste review cycles.
