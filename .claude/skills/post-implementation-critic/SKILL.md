---
name: post-implementation-critic
description: >
  Independent adversarial challenge of completed implementation work — after
  fixes are written, after tests are added, before final merge. Catches missed
  bug-class siblings, test false-greens, transaction/atomicity regressions, and
  incomplete fix coverage. Distinct from swarm-pr-review (incoming PR review)
  and code-review (diff review). Use when: "run a critic pass", "challenge the
  work", "do a final sanity check", or after a multi-finding fix session before
  pushing.
---

# Post-Implementation Critic

Run this after implementation and test-writing are done, before the final push
or merge request. The goal is to catch what the implementer context cannot
catch because it was the same context that wrote the code.

## When to use

- After fixing a bundle of review findings (e.g., "resolve all F-001..F-012")
- After writing new test coverage for recently-added features
- Before closing a long implementation session or marking a PR ready

**Do NOT use** as a substitute for `review-finding-validator` (that's for
classifying incoming findings before implementing them) or `swarm-pr-review`
(that's for deep incoming PR review). This is a post-implementation challenge.

---

## Core mechanic

Spawn **two independent critic subagents** with **disjoint scopes** in a
single parallel message. Each agent reads the actual code (not your summary of
it) and defaults to suspicion.

Give each agent a focused scope and a checklist of specific risks — do not give
them open-ended "find any bugs" prompts, which produce noisy output. Each
scope should be non-overlapping so findings don't duplicate.

---

## Scope split patterns

### After a multi-finding fix session

Split by: the source changes made vs. the test changes made.

**Critic A — Source correctness:**
- Are the bug classes fully closed, or is the same class still present in
  sibling endpoints/functions not named in the original findings?
- Are any fixes correct in isolation but broken when composed (e.g.,
  two fixes that interact on a shared transaction)?
- Does any fix introduce a new regression (e.g., a rollback that discards
  unrelated pending work)?

**Critic B — Test quality and false-greens:**
- Does each test actually exercise the live wiring it claims to test? Would
  it still pass if the fix were reverted?
- Are there `expectedFailure`/`xfail` markers that should instead be fixed?
- Does any test pin previously-buggy behavior (making a future correct fix
  fail the test)?
- Are any negative-path assertions reachable — or does the happy path execute
  before the negative case fires?

### After adding test coverage for new features

Split by: store-level (unit) tests vs. route-level (integration) tests and
frontend tests.

---

## Critic agent prompt structure

Each agent prompt must include:

1. **Explicit skepticism instruction:** "Default to suspicion. Find bugs, regressions,
   and false claims. Do not approve. Treat code comments and commit messages as
   unverified."
2. **The actual file paths and line ranges to read** — never ask the critic to
   infer scope.
3. **A specific checklist** matching the scope (see checklists below).
4. **The verdict format:** `CONFIRMED BUG / POTENTIAL BUG / DISPROVED`, each
   with exact file:line evidence and a one-line falsification probe.
5. **A word budget** to keep output concise and actionable (under 600 words
   per critic).

---

## Standard checklists by risk area

### Atomicity / transaction
- Does any helper called within a transaction do an unconditional `commit()`
  or `rollback()`? (Rollback discards all prior pending work on the connection.)
- Does any fix add a new call to a helper that was previously commit-safe but
  now runs inside another transaction?
- If a `commit=False` flag was added to a helper, are ALL callers that embed
  it in a transaction actually passing `commit=False`?

### Security class completeness (existence oracle, auth, scoping)
- Name the bug class that was fixed (e.g., "403 returned instead of 404 for
  cross-vault pages"). Grep for all code with the same structural shape in the
  same file and sibling files. For each sibling, confirm the fix is present OR
  explain why it is not affected.
- Check that both the read AND the write side of the same endpoint class are
  consistent.

### Test false-greens
- For each test that asserts a new behavior, ask: would this test pass if the
  source change were reverted? (If yes, it is not testing the new behavior.)
- Are any assertions on mocked values rather than real return values?
- Does the test exercise the full call chain (route → store → DB) or only part
  of it?

### Dead code / 409 or error branch
- For each error handler (e.g., `except IntegrityError → 409`), confirm the
  exception CAN actually reach that handler from the store layer — is the
  store suppressing it with `OR IGNORE`, `try/except/pass`, or similar before
  it propagates?

---

## Handling results

- **CONFIRMED BUG**: fix it before the final push. Do not defer.
- **POTENTIAL BUG**: investigate with a targeted read and decide; document the
  reason if you leave it unfixed.
- **DISPROVED**: record explicitly so you can cite it if the same concern
  resurfaces.

A confirmed bug found by a critic before merge is free. The same bug found
post-merge costs a hotfix cycle, a regression report, and trust.

---

## Anti-patterns to reject

- "Probably fine" — not a valid classification. Read the code and decide.
- Accepting a critic's DISPROVED without checking what the critic actually read
  (ask for the exact file:line they verified).
- Running only one critic covering everything — disjoint scopes exist so each
  critic can go deep rather than broad.
- Letting the same context that wrote the code also run the critic prompt
  in-line — this defeats the independence guarantee.
