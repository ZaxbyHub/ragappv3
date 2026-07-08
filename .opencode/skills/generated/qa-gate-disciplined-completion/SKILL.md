---
name: qa-gate-disciplined-completion
description: Verify all required QA gate artifacts and completion criteria before marking a task or phase complete.
generated_from_knowledge:
  - a7682dc0-94e5-4ba0-9e68-c91ab1f1cc91
  - 7c3b7977-c130-4d10-a346-6d8459e5f25a
  - fd18d241-e695-4942-bf52-e112b93ca340
  - 39fa83c4-8807-4733-9ee1-02f0a90fa2ed
  - 7f172662-6bbd-4471-99a7-7bb500d62f36
  - 971de146-9d5e-4370-98eb-bd3008f169c1
source_knowledge_ids:
  - a7682dc0-94e5-4ba0-9e68-c91ab1f1cc91
  - 7c3b7977-c130-4d10-a346-6d8459e5f25a
  - fd18d241-e695-4942-bf52-e112b93ca340
  - 39fa83c4-8807-4733-9ee1-02f0a90fa2ed
  - 7f172662-6bbd-4471-99a7-7bb500d62f36
  - 971de146-9d5e-4370-98eb-bd3008f169c1
generated_at: 2026-07-08T07:30:00.000Z
confidence: 0.60
status: active
version: 1
skill_origin: generated
---

# QA Gate Disciplined Completion

## Trigger

- Marking a task or phase as complete
- Presenting code for review
- Calling `phase_complete`
- Running final verification gates

## Required Procedure

- Verify all required QA gate artifact files exist in `.swarm/evidence/` before claiming a task is complete.
- Run the relevant test suite before declaring a task complete and verify tests pass in the actual test run output.
- Run `ruff check .` locally before marking any task done; verify `ruff check <changed_files>` returns zero errors.
- Check all task statuses before calling `phase_complete`; never call `phase_complete` while any task in that phase is still `in_progress`.
- Confirm all success criteria (lint, typecheck, coverage %, test count) before marking done.
- Scope lint/typecheck verification to the diff of changed files only, or ensure the baseline has no pre-existing CI failures; fix or explicitly suppress pre-existing failures before phase-end verification.

## Forbidden Shortcuts

- Change dependency versions that cause typecheck or build to fail.
- Fail a verification gate on pre-existing lint issues not introduced by the current phase.

## Delegation Template

When delegating a task affected by this skill, include:

```
SKILLS: file:.opencode/skills/generated/qa-gate-disciplined-completion/SKILL.md
```

## Reviewer Checks

- Verify required QA gate artifacts exist before approving.
- Confirm tests were actually run and passed.
- Verify lint is clean for changed files.
- Confirm `phase_complete` was not called with in-progress tasks.
- Verify success criteria were explicitly agreed and met.
- Ensure pre-existing CI failures were fixed or suppressed before phase-end verification.

## Source Knowledge IDs

- a7682dc0-94e5-4ba0-9e68-c91ab1f1cc91 — Before presenting code for review, ensure all required QA gate artifacts (drift-verifier.json, hallucination-guard, etc.) exist and are populated. Claiming completion without required evidence is a revert-triggering gap.
- 7c3b7977-c130-4d10-a346-6d8459e5f25a — Coders must actually run the test suite and verify tests pass before marking a task complete. Checking a box that says "I did not run tests" is a skipped verification.
- fd18d241-e695-4942-bf52-e112b93ca340 — Coders must run lint locally before marking implementation complete; lint failures on touched files are the coder's responsibility.
- 39fa83c4-8807-4733-9ee1-02f0a90fa2ed — Never call phase_complete while any task in that phase has status in_progress — this creates plan/reality divergence that blocks future planning operations.
- 7f172662-6bbd-4471-99a7-7bb500d62f36 — Test success criteria must be explicitly agreed before execution — "all tests pass" alone is insufficient when the task also requires clean lint, type-check pass, or specific coverage thresholds.
- 971de146-9d5e-4370-98eb-bd3008f169c1 — Phase-end verification that runs lint/typecheck must operate on a baseline where pre-existing CI failures are already fixed or explicitly suppressed.
