---
name: reviewing-code-core
description: Evidence-first code review for correctness bugs, AI-generated code smells, unsupported claims, unwired functionality, dependency hallucinations, and release-to-code mismatches.
---

# Reviewing Code Core

## Use this skill when
- reviewing a PR, diff, branch, or local changes
- auditing a repository for shipped bugs or hidden implementation gaps
- validating that release notes, changelog bullets, docs, and comments match what the code actually ships
- detecting LLM-generated code smells, fake abstractions, placeholders, and unwired functionality

## Review stance
Treat code as plausible until verified, not correct until disproven.
Treat docs, release notes, PR text, comments, docstrings, examples, and tests as claims or hints, not proof.

Never emit a defect without exact file and line support.
Never approve changes without stating what was checked.

## Workflow

### Phase 0 — Reconstruct intent
Extract intended behavior from:
- diff and changed files
- tests
- docs and README edits
- release notes and changelog bullets
- PR description or task brief
- changed exports, routes, commands, config keys, migrations, schemas, and handlers

Convert prose and scattered signals into atomic obligations.

### Phase 1 — Summarize actual behavior
Independently summarize what the code actually does.
Do not compare yet.

### Phase 2 — Compare obligations vs implementation
For each obligation, classify:
- SUPPORTED
- PARTIALLY_SUPPORTED
- UNSUPPORTED
- CONTRADICTED
- STEALTH_CHANGE

For database-backed API changes, additionally verify:
- Response model fields are returned by the underlying SQL SELECT (check model definition against every query path that populates it)
- Frontend API types match backend serialization (do not assume backend int = frontend number; verify the actual API contract)

### Phase 3 — Run the substance gate
Check for:
- placeholders, TODOs, stubs, no-ops
- unwired functionality
- fake wrappers and dead abstractions
- duplicate generated logic
- tests that assert existence instead of behavior
- comments, release notes, or docstrings that overstate what exists
- polished scaffolding around missing behavior

### Phase 4 — Run AI-slop checks
Check for:
- happy-path-only logic
- off-by-one and boundary failures
- stale API usage
- context rot against local file conventions
- unnecessary async or fake abstractions
- generated duplication
- requirement-conflicting plausible implementations

### Phase 5 — Emit verdict
Use exact file/line evidence only.
Classify severity first.
Only then propose fixes.

## Severity rules
- CRITICAL — security/supply-chain issue, broken shipped feature, data-loss risk, unsupported shipped claim in a production path, or missing auth/validation at a critical boundary
- HIGH — real bug under normal conditions, stealth shipped behavior, materially unsupported docs/release claim, or serious test blind spot on critical behavior
- MEDIUM — bounded correctness or maintainability issue with plausible impact
- LOW — localized quality issue or drift
- INFO — useful note or coverage caveat

## AI-specific pattern names
Use these labels when relevant:
- mapping-hallucination
- naming-hallucination
- resource-hallucination
- logic-hallucination
- claim-hallucination
- phantom-dependency
- stale-api
- context-rot
- happy-path-only
- unwired-functionality

## Approval rules
Do not approve if any of the following are true:
- a claimed feature lacks structural proof
- a new dependency is unverified
- a critical trust boundary lacks validation
- a route, command, export, schema, or handler is unwired
- the review cannot cite exact file/line support

If approving, explicitly list:
- files reviewed
- obligations checked
- claims validated
- dependencies validated
- trust boundaries inspected
- tests or commands used as evidence

## Output format
VERDICT: APPROVED | REJECTED
RISK: CRITICAL | HIGH | MEDIUM | LOW

ISSUES:
- [severity] file:line — issue

FIXES:
- [priority order remediation]
