---
name: reviewing-security
description: Inspect trust boundaries, validation, authn/authz, deserialization, command execution, path handling, secrets, and failure handling with an evidence-first security review.
---

# Reviewing Security

## Trust boundaries
Enumerate and inspect:
- HTTP inputs
- CLI args
- env vars
- file reads and writes
- subprocess invocations
- deserializers and parsers
- SQL and ORM boundaries
- IPC and queue inputs
- authn and authz checks
- template and rendering sinks

## Mandatory questions
- Is input validated at the actual boundary?
- Can user input reach filesystem, shell, SQL, template, or render sinks?
- Are privileged operations guarded where they execute?
- Are secrets or tokens logged, hardcoded, or exposed in examples?
- Are errors swallowed instead of handled?
- Are there security-relevant defaults that fail open?

## Sibling-endpoint consistency check

When a security class is fixed at named locations (e.g., existence-oracle
403→404, missing vault scoping, auth-before-fetch ordering), always grep for
all sibling handlers with the same structural pattern and verify each instance.

A fix applied to 3 of 4 endpoints is not a fix — it is a newly introduced
inconsistency. The unfixed sibling is often harder to find than the original
because the original finding gives false confidence that the class is closed.

Checklist when fixing an endpoint security class:
1. Grep for the same structural pattern (guard condition, status code, query
   shape) across all route handlers in the same file and sibling route files.
2. For each sibling, confirm the fix is present OR confirm the sibling is not
   reachable / not affected and state why.
3. Update tests for ALL affected siblings, not just the originally-named ones.

## Hard fail conditions
- missing auth or authz on privileged path
- injection or path traversal risk
- unsafe deserialization or arbitrary code execution pattern
- secret exposure
- trust boundary with no meaningful validation
