# Closure-evidence gate

The closure-evidence gate (`.github/workflows/closure-evidence.yml`, driven by
`scripts/check_closure_evidence.py`) enforces the repository's existing
regression-evidence convention at the moment a pull request claims to close an
issue. It adds enforcement to a standard that already existed in writing — it
does not redefine it:

- `.opencode/skills/issue-tracer/SKILL.md` — "Regression test fails before the
  fix and passes after the fix when feasible."
- `.opencode/skills/swarm-implement/SKILL.md` — "Regression tests must be
  falsifiable." (verify the fix actually guards the repair: temporarily revert
  only the source fix, run the test, confirm it fails)

A PR whose body contains `Closes #N` / `Fixes #N` (GitHub's closing keywords)
must name closure evidence the gate can check, and for `high`/`critical`
issues it must additionally carry an approving review from outside the fix's
own file family.

## The flagship motivating case: issue #343

Issue #343 was closed on **2026-07-08** (via PR #349). Its body recorded, as
an already-completed fact, that issue #283 had "renamed `/eval/ragas` to
`/eval/heuristic`" — and #343's own body contained tests hitting
`POST /api/eval/heuristic` that could not have passed at closure time. Git
history proves the claim was false when it was recorded:
`git log -G 'eval/heuristic' -- backend/app/api/routes/eval.py` shows exactly
one commit touching that route — **7c3d404b, dated 2026-09-12** — so the
rename the closure recorded did not exist in the code until **66 days after**
the issue was closed.

A closure-evidence gate catches exactly this: the PR that closed #343 would
have had to name the tests its own body quoted, and `verify-test` would have
re-executed them against the pre-fix commit — where the route did not exist
and the tests could not pass. (Today the route exists as the canonical
`/eval/heuristic` with `/eval/ragas` kept as a documented deprecated alias;
the gap is the historical 66-day window, not a current code defect.)

## What the gate checks

1. **Closing references.** The PR body is parsed for GitHub's closing keywords
   (`close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved #N`) on a
   markdown-normalized copy — GitHub resolves closing keywords after markdown
   rendering, so `**Closes** #1`, `Closes:#1`, and NBSP-separated variants
   auto-close issues and are all detected (emphasis markers and backticks are
   treated as spaces). No closing reference → the gate is not applicable and
   passes.
2. **Named closure evidence.** With a closing reference present, the body must
   name, in order of precedence:
   - a **backend pytest test id** — `backend/tests/<file>.py(::node)?`. The
     *first* parseable id wins; exactly one
     `closure-evidence: evidence-test <id>` line is emitted and the workflow
     re-executes that test (additional ids are noted, never re-executed); or
   - if no test id is named, a **captured artifact** — `artifact:
     <repo-relative-path>` — which must exist in the repository at the PR
     head. A named-but-missing artifact fails the gate: that is the #343
     pattern in artifact form, a claim with nothing behind it. URL-form
     artifacts are advisory only (the gate never makes network calls to
     validate them).
3. **Re-execution (`verify-test`).** The named test is re-run at the
   merge-base of the PR's base branch and HEAD (the pre-fix commit — obtained
   via `git merge-base origin/<base-ref> HEAD`) and at the PR head. The gate
   accepts only **fails-at-base, passes-at-head**. A test that *passes* at
   base is rejected as the #343 pattern (it detects nothing the PR repairs);
   a brand-new test counts as failing at base, because it cannot run there.
4. **Cross-family reviewer for `high`/`critical` issues.** If ANY
   closing-referenced issue carries the `high` or `critical` label, at least
   one APPROVED review must come from a login that authored no commit in the
   PR touching the file families of the PR's changed files. Approvals from
   `[bot]` accounts never count — a bot approval is not a human second-family
   review.

### File-family mapping

Families are an explicit prefix table (longest prefix wins); anything else
falls back to the first two path segments:

| Path prefix | Family |
|---|---|
| `backend/app/api` | backend-api |
| `backend/app/services` | backend-services |
| `backend/app/models` | backend-models |
| `backend/app` (other) | backend-core |
| `backend/tests` | backend-tests |
| `frontend/src` | frontend-src |
| `scripts/` | tooling-scripts |
| `.github/` | ci-workflows |
| `docs/` | docs |

## Enforcement semantics (what GitHub can and cannot block)

GitHub closes the issue **at merge** as a side effect of the closing keyword;
no hook can block that event itself. Enforcement is therefore:

1. a **required failing check** on the PR in enforce mode (the job exits 1),
   which branch protection keeps unmergeable; and
2. the documented **reopen-on-merge procedure** below, for the case where a
   violating PR merges anyway (e.g. admin override).

**Reopen-on-merge procedure** (operator): if a merged PR turns out to have
closed an issue without valid evidence, reopen the issue with a comment
linking the gate run (`closure-evidence: FAIL` in the Actions log) and naming
the missing evidence. The gate's output lines (`closure-evidence: OK / WARN /
FAIL / ERROR / evidence-test`) are designed to be quoted directly.

## Modes, rollout, and rollback

- **warn** (default): the gate evaluates and re-executes everything, prints
  `closure-evidence: WARN — …` (or `ERROR`) observations, and never fails the
  check. This is the one-release-cycle rollout mode so existing open PRs are
  not retroactively blocked.
- **enforce**: violations print `closure-evidence: FAIL — …` and fail the
  check. Dispatch runs may select the mode via the `mode` input (used by the
  gate's proving PRs).
- **Flip to enforce**: change the default in the workflow's `MODE` expression
  (one line: `inputs.mode || 'warn'` → `'enforce'`) and record the flip in the
  release notes. The mode governs the exit code only — diagnostics are always
  truthful in both modes.
- **Rollback**: disable or delete `.github/workflows/closure-evidence.yml`.
  No application code, schema, or config is touched by the gate.

## Error policy and limitations

- **Live-fetch errors**: `evaluate --pr N` fetches via `gh api` with one retry
  on transient failures. A 404 on a referenced issue skips that reference
  (issue gone); any other fetch failure (including `gh` being unavailable)
  prints `closure-evidence: ERROR — …` — exit 0 in warn mode, exit 1
  (fail-closed) in enforce mode.
- **Fork PRs**: the job is skipped for forks (`head.repo.full_name` guard) —
  fork runs cannot authenticate `gh` against the base repo. Documented
  constraint; dispatch-driven evaluation remains available in-repo.
- **Frontend evidence**: frontend vitest ids are accepted as *named artifacts
  only* and are **not re-executed** by `verify-test` in v1 ("where feasible"
  per issue #568 — only backend pytest ids are re-executed). Name the vitest
  file/function as the artifact and link its captured run output.
- **Commit cap**: per-commit files are fetched for the first 100 PR commits;
  a commit whose files could not be fetched is treated conservatively — its
  author counts as same-family and can never serve as the cross-family
  approver (an unfetched commit must not weaken the reviewer rule).

## Satisfying the gate (for PR authors)

Add a section like this to the PR body when it closes an issue:

```markdown
Closes #<issue>

Closure evidence: backend/tests/test_<thing>.py::test_<name> — fails on the
pre-fix commit (merge-base), passes on this PR head (re-executed by
verify-test in the Closure evidence workflow).
```

For artifact-only evidence: `Closure evidence. artifact: <repo-relative-path>`
pointing at a file that exists in the repository.
