---
name: swarm-pr-feedback
description: Ingest an external PR review bundle (from swarm-pr-review, a human reviewer, or CI), verify each finding against current code, fix confirmed findings, add regression tests, amend the commit, and push. Use when given a structured review output with classified findings to resolve on an existing PR branch.
disable-model-invocation: true
---

# /swarm-pr-feedback

Use this skill to resolve an external review bundle on a PR branch. It covers
the full loop: ingest → verify → fix → test → amend → push → update PR body.

This skill is the counterpart to `swarm-pr-review` (which *generates* a review)
and `commit-pr` (which opens a fresh PR). Use it when a review already exists
and you need to act on its findings.

---

## Pre-flight: get onto the correct branch first

Explorers and verification steps read files from the working tree. If the PR
branch is not checked out locally, you verify and fix against the wrong code.
Before ingesting the review bundle:

```bash
git status --porcelain            # must be empty; stash if dirty
git fetch origin <branch-name>
git checkout <branch-name>        # or: git checkout --track origin/<branch-name>
```

Then scope every diff/verification to the PR's actual commit range
(`base_ref..head_ref`), not the full branch accumulation. See `swarm-pr-review`
Phase 0 "PR Branch Checkout (mandatory)" for the canonical version.

---

## Inputs

The user provides one or more of:
- A structured review bundle (from `swarm-pr-review` output, a human reviewer,
  or an AI code review tool) containing findings with severity, file:line
  references, and classification status.
- A PR number or branch name.
- An explicit list of findings to accept, defer, or reject.

---

## Non-negotiable rules

1. Verify every finding against actual current code before fixing. A finding
   classified as CONFIRMED by the reviewer is still a candidate until you open
   the file and read the cited lines.
2. Fix confirmed findings only. Do not broaden scope with adjacent cleanup.
3. For every fix, add or update a regression test that would have caught the
   bug. The test must fail without the fix and pass with it, or document why
   a test is not feasible.
4. Amend the existing commit (not a new one) so the PR history stays clean,
   then force-push with `--force-with-lease`. Note: amending is explicitly
   correct here — PR review follow-up is a well-defined amend use case.
   This overrides the general convention to prefer new commits.
5. Update the PR body with an explicit `## Review follow-up` section listing
   Accepted, Rejected, and Deferred findings with short evidence.
6. Run all frontend/backend CI gates locally before pushing. A CI-only failure
   costs a full push → fail → fixup round trip.

---

## Workflow

### Step 1 — Ingest and classify

Parse the review bundle into a structured list. For each finding record:
- Finding ID (e.g., F-001)
- Severity (CRITICAL / HIGH / MEDIUM / LOW)
- Claim (what the reviewer said is wrong)
- Cited file:line
- Reviewer classification (CONFIRMED / DISPROVED / UNVERIFIED / PRE_EXISTING)
- Coverage gap vs correctness bug (coverage gaps may be deferred)

If the bundle lacks explicit classification, treat every finding as UNVERIFIED
until you verify it yourself.

### Step 2 — Verify findings against current code

Open every cited file and line. Do not trust line numbers from a review bundle —
the branch may have changed. For each finding:

- **CONFIRMED by reviewer + code matches**: accept for fixing.
- **CONFIRMED by reviewer but code does not match**: classify as `not reproduced`;
  document the discrepancy; do not fix.
- **UNVERIFIED**: open the file, read the logic, make your own judgment.
- **Coverage gap (not a correctness bug)**: decide whether to add the test now
  or defer. Prefer adding if the test is simple and the code path is real.
- **PRE_EXISTING / out of scope**: document; do not fix.

Use the `review-finding-validator` skill for large bundles (>5 findings) or
when findings are ambiguous or high-risk.

### Step 3 — Plan fixes

For each accepted finding, state:
- The exact code change (file, function, what changes and why).
- The regression test to add (what it asserts, why it would catch this).
- Any behavioral-change test trap: pre-existing tests that assert the old
  behavior and will fail after the fix. Update them with a comment.

For correctness fixes that touch auth, session handling, payments, migrations,
or concurrency: treat as high-risk and apply extra scrutiny before fixing.

### Step 4 — Implement

Apply fixes in dependency order (fix foundations before things that depend on
them). After each fix:
- Re-read the changed function to confirm the fix is complete and no dead
  branches were introduced.
- Confirm the regression test fails without the fix (reason through it if a
  live failing run isn't practical).

### Step 5 — Validate

Run all applicable gates from the project's CI contract. Use the exact npm
script and direct-executable forms that CI uses — bare `npx`/`python -m`
invocations may miss config flags the scripts encode.

**Frontend** (from `frontend/`):
```
npm run typecheck         # tsc --noEmit, matches CI
npm run lint              # eslint with project config, matches CI
npm test                  # vitest run, matches CI
npm run build             # production build (for path-critical changes)
```

**Backend** (from `backend/`):
```
ruff check .              # matches CI directly
pytest <touched modules>  # matches CI directly
```

Do not push until all gates are green. A single CI-only lint or type error
costs an extra push → fail → fixup commit.

### Step 6 — Amend and push

```bash
git add <changed files>               # stage explicitly; never git add -A
git commit --amend                    # keep PR to one meaningful commit
                                      # (explicit amend use case — overrides
                                      #  the general prefer-new-commit rule)
git push --force-with-lease -u origin <branch>
```

Never use plain `--force`. If `--force-with-lease` fails, check for concurrent
pushes before retrying.

### Step 7 — Update PR body

Add or update a `## Review follow-up` section in the PR description:

```md
## Review follow-up

**F-001 — ACCEPTED** (short title)
[What was wrong, what changed, what test was added.]

**F-002 — REJECTED** (short title)
[Why: not reproduced / pre-existing / out of scope + evidence.]

**F-003 — DEFERRED** (short title)
[Why deferred (coverage gap, separate issue, out of PR scope) + issue link if filed.]
```

---

## Persisting findings (survive context compaction)

Review-bundle resolution touches many findings across ingest, verification, and
fixing. If held only in conversation context, a context compaction silently
erases the per-finding classification and forces re-verification. After
ingestion (Step 1) and after fixing (Step 4), persist findings to a structured
scratch artifact (e.g. `.swarm/evidence/review-{id}/{phase}.json` — gitignored
local working state, NOT committed). Minimum fields per finding: `finding_id`,
`status` (PENDING/CONFIRMED/DISPROVED/PRE_EXISTING), `severity`, `file_line`,
`evidence`, `next_action`. The canonical schema and checkpoint triggers live in
the `swarm-pr-review` skill ("Persisting Findings to Survive Context
Compaction") — reference it rather than duplicating. On resume after
compaction, reload these artifacts and continue from the last checkpoint.

---

## Handling coverage gaps

Coverage gaps (correct code, missing test) are lower priority than correctness
bugs. Default behavior:

- **Simple, in-scope gap**: add the test now. One assertion is better than zero.
- **Complex or out-of-scope gap**: file a GitHub issue, add the issue link to
  the PR body under Deferred, and move on.

Do not add speculative tests for hypothetical inputs the code will never see.

---

## Handling a review with no confirmed findings

If every finding is DISPROVED or PRE_EXISTING:
- Document all classifications in the PR body.
- Do not amend the commit (nothing changed).
- Confirm the PR is ready for merge as-is.

---

## Quick reference — common finding patterns

| Pattern | Usual fix | Regression test shape |
|---|---|---|
| Backoff/timer not reset after clean path | Reset variable in the correct branch | Fake timers; verify next attempt fires in base delay |
| Missing null guard on optional | Add guard; decide error vs silent skip | Pass `null` / `undefined`; assert no crash |
| Silent event drop (wrong dispatcher) | Route event to correct handler | Emit the event; assert callback fires |
| Cookie path mismatch under subpath | Use path helper, not hardcoded string | Monkeypatch `app_root_path`; assert cookie path |
| Token in URL (security) | Move to header or POST body | Assert no token in logged URL |
