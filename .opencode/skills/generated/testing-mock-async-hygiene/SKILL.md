---
name: testing-mock-async-hygiene
description: Keep mocks, async setup, and test assertions clean and aligned with the code under test.
generated_from_knowledge:
  - c3cfc3bb-0f9d-4d3b-9295-89f1bfbc82af
  - 5e3c36f2-fb34-4976-a566-b65ba3c169e4
  - fc0771fa-fd12-4696-ba15-4a57cf8755d4
  - 87aa8368-21df-4bb3-a1f5-e48b75f52e64
  - 26690c1b-b69d-4999-85cf-ee5ac7b63bc3
  - ea40215e-552f-4b49-bb42-5188f512efcc
  - b414164d-5567-40f5-9624-56f3f4bed586
source_knowledge_ids:
  - c3cfc3bb-0f9d-4d3b-9295-89f1bfbc82af
  - 5e3c36f2-fb34-4976-a566-b65ba3c169e4
  - fc0771fa-fd12-4696-ba15-4a57cf8755d4
  - 87aa8368-21df-4bb3-a1f5-e48b75f52e64
  - 26690c1b-b69d-4999-85cf-ee5ac7b63bc3
  - ea40215e-552f-4b49-bb42-5188f512efcc
  - b414164d-5567-40f5-9624-56f3f4bed586
generated_at: 2026-07-08T07:30:00.000Z
confidence: 0.60
status: active
version: 1
skill_origin: generated
---

# Mock and Async Test Hygiene

## Trigger

- Setting up multiple API endpoint mocks in a single test
- Testing components with async initialization
- Mocking async functions
- Test failures mention "mocks" or "defensive handling"
- Investigating test failures before attributing them to implementation
- Writing test selectors
- Changing a function's return signature

## Required Procedure

- Use `mockImplementation` with URL routing when multiple endpoints are mocked in a single test; chain `mockResolvedValueOnce` calls in the exact order API calls will occur.
- Await all async setup/state-population operations before triggering handlers under test.
- Ensure async mocks resolve before any code that depends on their resolved value executes.
- Implement the behavior the test verifies; investigate failures that mention mocks or defensive handling as contract divergence.
- Before attributing test failures to implementation, verify the test environment (node_modules, TypeScript version, lockfile state) is consistent with the baseline.
- Use `data-testid` attributes or semantic role/text selectors when multiple similar elements exist.
- Audit all direct-call test sites and their unpacking patterns whenever a tuple-returning function signature changes.

## Forbidden Shortcuts

- Weaken tests to make them pass.
- Mock around missing implementation.
- Use CSS class names as test selectors.
- Rely on implicit tuple length matching for assertion validation.

## Delegation Template

When delegating a task affected by this skill, include:

```
SKILLS: file:.opencode/skills/generated/testing-mock-async-hygiene/SKILL.md
```

## Test Engineer Checks

- Verify async setup is awaited before handlers are triggered.
- Verify all parallel mock calls resolve.
- Add edge-case assertions before concluding BUGS_FOUND: none.
- Add or update tests covering the trigger condition and the forbidden shortcut.

## Reviewer Checks

- Verify mock setup order matches API call order.
- Verify implementation changes match test contracts, not just that tests pass.
- Verify selectors target the correct element and not a sibling with similar tag.
- Verify tuple unpacking matches the current return signature.

## Source Knowledge IDs

- c3cfc3bb-0f9d-4d3b-9295-89f1bfbc82af — In test files with multiple API endpoint mocks, set up mocks in call order or use URL-routed mockImplementation to prevent one mock from consuming another's response.
- 5e3c36f2-fb34-4976-a566-b65ba3c169e4 — When testing components with async initialization that populates state used in event handlers, tests must await that initialization before triggering the handler.
- fc0771fa-fd12-4696-ba15-4a57cf8755d4 — Mock implementations for async functions must resolve before the code under test runs.
- 87aa8368-21df-4bb3-a1f5-e48b75f52e64 — Test failures citing "mocks" or "defensive handling" indicate the implementation diverged from the contract defined by tests.
- 26690c1b-b69d-4999-85cf-ee5ac7b63bc3 — Before attributing test failures to implementation, explicitly verify that the test environment is consistent with the baseline.
- ea40215e-552f-4b49-bb42-5188f512efcc — Robust test selectors avoid brittle class-name assertions.
- b414164d-5567-40f5-9624-56f3f4bed586 — Tuple signature changes require updating all direct-call test sites.
