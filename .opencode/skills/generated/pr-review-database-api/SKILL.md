---
name: pr-review-database-api
description: PR review checklist for database-backed FastAPI + SQLite + React APIs. Use when reviewing changes that touch API response models, database queries, or frontend-backend data contracts.
generated_from_knowledge:
  - 9e69117c-f877-42e6-9ade-848b8552a03b
  - 13da653c-37f5-4193-9a88-644f9a31fa2c
  - 5dd99955-2103-41c7-ba31-b1c1d8ca17c7
  - 670ea64b-3607-4a9c-9252-b09e12d22444
  - cfde9fb8-03e1-4f5c-95e7-13a460b85293
  - 0aa2d48d-66a3-4ff9-89bd-37bdb3a9f8d7
confidence: 0.50
status: active
trigger:
  - files:
      include:
        - "backend/app/api/routes/*.py"
        - "backend/app/services/*.py"
        - "frontend/src/lib/api.ts"
        - "frontend/src/pages/*.tsx"
        - "backend/app/models/database.py"
      exclude:
        - "*.test.*"
        - "*.spec.*"
---

# PR Review: Database-Backed API

Use this skill alongside `swarm-pr-review` or `reviewing-code-core` when reviewing changes that touch API response models, SQL queries, or frontend-backend data contracts.

## Required Checks

### 1. Response Model / SELECT Column Alignment
- [ ] For every new or changed field in a Pydantic `BaseModel` response class, verify it appears in the SQL SELECT column list of every query path that returns that model.
- [ ] Check `list_*` endpoints separately from `get_*` endpoints — they often have different SELECT columns.
- [ ] Check that `_row_to_*_response` helper functions read the column (with a guard like `if "col" in keys`) and that the column is actually present in the query.
- [ ] Red flag: field in model, absent from SELECT, silently returns `null` via a guard clause.

### 2. Frontend / Backend Type Boundary
- [ ] Verify the TypeScript interface in `frontend/src/lib/api.ts` matches the backend response model.
- [ ] Do not assume backend `int` maps to frontend `number` — check the actual interface definition.
- [ ] Watch for `Set<string>.has(number)` or similar mismatches when document IDs are strings in the API but numbers in the database.
- [ ] Verify that frontend selection state (bulk selection, filters) uses the same ID type as the API response.

### 3. SQLite Transaction Safety
- [ ] When a route uses `BEGIN IMMEDIATE` or `BEGIN EXCLUSIVE`, check that no helper function called before it commits on the same shared connection.
- [ ] If a helper does commit (e.g., `mark_claims_stale_by_file`), verify it cannot raise before its internal commit, or use a separate connection.
- [ ] Check that `_purge_file_derived_data` style helpers do not leave implicit transactions open.
- [ ] Prefer separate connections for cleanup/audit vs. atomic operations.

### 4. Unbounded Concurrent Requests
- [ ] Scan for `Promise.allSettled` over list items in polling hooks or effects.
- [ ] If found, verify there is a batch endpoint or concurrency limit (`p-limit`, chunked requests).
- [ ] Red flag: `fetchWikiStatuses` firing N concurrent GETs for N indexed documents.
- [ ] Check that adaptive polling backoff does not amplify the flood on every tick.

### 5. SQL Performance
- [ ] Correlated subqueries for per-row aggregation (e.g., `COUNT(*) ... WHERE dt.tag_id = t.id`) are correct but suboptimal at scale.
- [ ] For tables expected to grow >100 rows, recommend JOIN + GROUP BY rewrite.
- [ ] Check that `IN (...)` subqueries have supporting indexes.
- [ ] Verify `INSERT OR IGNORE` in nested loops — for bulk operations, prefer `executemany` or batch INSERT.

### 6. Test Name / Behavior Alignment
- [ ] Tests named after behavior (e.g., "emits X on selection") must actually trigger and assert that behavior.
- [ ] A test that only renders the component without firing events is a coverage gap, not a behavior test.
- [ ] Check that `fireEvent` or `userEvent` calls exist for interaction tests.

## Forbidden Shortcuts
- Do not assume backend int = frontend number without checking the API type.
- Do not approve response model changes without checking all query SELECT columns.
- Do not ignore `Promise.allSettled` over dynamic lists without checking for batch endpoints.

## Delegation Template
When delegating a PR review affected by this skill, include:

```
SKILLS: file:.opencode/skills/generated/pr-review-database-api/SKILL.md
```

## Source Knowledge
- 9e69117c-f877-42e6-9ade-848b8552a03b — Response model / SELECT alignment
- 13da653c-37f5-4193-9a88-644f9a31fa2c — Frontend/backend type boundaries
- 5dd99955-2103-41c7-ba31-b1c1d8ca17c7 — SQLite transaction edge cases
- 670ea64b-3607-4a9c-9252-b09e12d22444 — Unbounded concurrent requests
- cfde9fb8-03e1-4f5c-95e7-13a460b85293 — Correlated subquery performance
- 0aa2d48d-66a3-4ff9-89bd-37bdb3a9f8d7 — Test name/behavior alignment
