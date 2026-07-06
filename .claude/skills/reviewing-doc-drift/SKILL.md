---
name: reviewing-doc-drift
description: Verify that README changes, release notes, changelog bullets, migration notes, PR descriptions, examples, and docstrings match what the code actually ships.
---

# Reviewing Docs and Release Drift

## Goal
Detect claimed features that are not actually shipped, and shipped behavior that is not documented.

## Workflow
1. Extract atomic claims from README/docs, release notes, changelog, PR text, examples, comments, docstrings, and migration notes.
2. For each claim, locate structural proof in code, tests, config, routes, exports, handlers, migrations, or user-visible wiring.
3. Classify each claim:
   - SUPPORTED
   - PARTIALLY_SUPPORTED
   - UNSUPPORTED
   - CONTRADICTED
   - STEALTH_CHANGE
4. Emit defects only for unsupported, contradicted, or material stealth changes.

## Required checks
- feature claims map to actual handlers, exports, routes, commands, or user-facing paths
- resilience claims map to retries, backoff, fallback, or recovery logic
- caching claims map to cache read, write, and invalidation behavior
- security claims map to enforced checks where the privileged action executes
- compatibility claims map to actual platform-safe behavior
- breaking changes have migration notes or equivalent guidance
- for renamed public paths/routes, verify consumers import the router object rather than individual Pydantic models, hardcoded path strings, or named response-model classes; renaming a path is safe when only the mounted router is imported, but breaking if models or paths are imported directly
- release/changelog scope matches actual changed behavior
- examples would work if followed literally
