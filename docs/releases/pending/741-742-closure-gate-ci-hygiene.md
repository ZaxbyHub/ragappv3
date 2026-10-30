# Closure-evidence gate evidence semantics + CI execution hygiene (#741, #742)

Issues: #741 (Workstream H PR 3 of 10), #742 (Workstream H PR 4 of 10).

## What changed

### scripts/check_closure_evidence.py (#741)

- **URL artifacts are advisory, by behavior not just by comment.** A body
  whose only evidence is `artifact: https://...` no longer yields a false
  "named but missing from the repository" violation: URL-form captures are
  skipped at resolution (`URL_ARTIFACT_RE`), exactly as
  docs/ci/closure-evidence-gate.md always promised.
- **Backtick-wrapped artifacts resolve.** The artifact capture is
  Markdown-noise-stripped (emphasis/backticks/NBSP deleted, never replaced
  with spaces) before `.rstrip(".")`, so "artifact: `docs/x.md`" resolves to
  the real path. The close-ref path already normalized; now the artifact
  path does too. Missing and traversal-shaped artifacts still fail (pinned).
- **List fetches paginate and fail closed.** The commits, files and reviews
  lists are paginated (bare first page, `&page=N` after) and the fetched
  counts are cross-checked against the pull request's own
  `commits`/`changed_files` totals — a mismatch (e.g. GitHub's 250-commit
  list cap, which ends on a SHORT page at exactly 250 and so defeats
  page-size loops) raises `FetchError` instead of silently evaluating a
  truncated snapshot whose invisible commits could have held the
  cross-family reviewer.
- **Review histories are reduced per login (decided rule).** Cross-family
  approval for `high`/`critical` issues counts only each reviewer's LATEST
  non-COMMENTED review: an approval later superseded by CHANGES_REQUESTED or
  DISMISSED no longer satisfies the rule; CHANGES_REQUESTED later
  superseded by APPROVED does. Empty/missing logins never count; the `[bot]`
  exclusion is unchanged. Documented in docs/ci/closure-evidence-gate.md.

### .github/workflows/closure-evidence.yml (#741)

- **The gate runs the BASE branch's copy of the checker.** The Evaluate step
  restores `scripts/` from the PR's base ref (`git checkout origin/<base>
  -- scripts/`) before invoking, so a PR that edits the gate script or its
  tests cannot grade itself. Both checker invocations and the inline JSON
  parsers run under `python -I` (isolated mode), so a PR-added module under
  `scripts/` cannot shadow stdlib imports either. On `workflow_dispatch`
  the PR head is checked out first so artifact evidence resolves against
  the PR's tree. The preflight is status-captured: a failing `gh`/`git`
  records a truthful `closure-evidence: ERROR — base-copy preflight failed`
  (eval_rc=1) without running the checker — warn mode stays non-blocking,
  enforce mode fails closed.

### .github/workflows/ci.yml (#742)

- **Backend Test step bounded.** Step-level `timeout-minutes: 50`, strictly
  inside the job's 60-minute budget: a post-test wedge is cut off and names
  the step instead of burning the job (the per-test `--timeout-method=thread`
  from #1021 is untouched and still pinned).
- **No frontend tree in the Backend job — and the frontend-toolchain nodes
  keep exactly one CI venue.** The #566 guardrail's TypeScript parser now
  installs outside the repo tree
  (`npm install --prefix "$RUNNER_TEMP/h03-gate-ts" typescript@6.0.3`) with
  `NODE_PATH` pointing the walker's `require('typescript')` at it, so
  `frontend/node_modules` never appears in the Backend job. The two
  issue-258 frontend-toolchain nodes are pytest tests that need the
  frontend install, so their single CI venue is a scoped pytest step in the
  Frontend job ("Frontend-toolchain contract nodes"); the step fails the
  job if either node skips in its only venue ("never in neither"), and the
  tsconfig.node.json typecheck they pin (no npm script invokes it —
  `typecheck:contracts` covers the separate contracts surface) keeps live
  CI coverage.

### Node floor aligned at one value (#742)

`>= 22.22.2` everywhere it is stated: `.devcontainer/devcontainer.json`
(was 22.22.0 — fresh devcontainer builds failed `npm ci --engine-strict`
with EBADENGINE against jsdom 30's transitive
`^22.22.2 || ^24.15.0 || >=26`), `frontend/package.json` engines (was
`>=22.22.0`, admitting the rejected 22.22.0/22.22.1), the package-lock
root-entry mirror, CONTRIBUTING.md, INSTALLATION.md, and the ci.yml pin
citations in Dockerfile / frontend/Dockerfile (which had drifted to naming
a "22.22.0" pin that ci.yml no longer had).

### Tests

- backend/tests/test_h03_closure_evidence.py — frozen acceptance checks
  (arm's-length authored; URL advisory, backtick resolution, commits/files
  never silently truncated, superseded-approval fixture, base-copy workflow
  pin).
- backend/tests/test_h03b_closure_evidence_edges.py — edge coverage beyond
  the frozen set: 250-cap fail-closed via count mismatch, superseding review
  beyond page one, DISMISSED-latest, COMMENTED-only, empty-login,
  `python -I` shadow-module probe, and the Evaluate preflight shell
  contracts (failing gh / failing git checkout keep warn mode non-blocking
  and never run the checker).
- backend/tests/test_h04_ci_hygiene.py — frozen acceptance checks (job
  timeouts + hang stacks as PRESERVING pins of the #1034 state; step
  timeout, frontend-toolchain scoping, devcontainer/engines/docs Node floor
  as DISCRIMINATING).
- Truthfulness touch-ups only in test_issue566_config_loader_native.py and
  test_issue258_coverage_gate.py (skip-reason strings describing the new
  CI layout); no behavior change.

## Notes for operators

- The closure-evidence gate remains in warn mode; this PR makes its verdicts
  truthful, it does not flip enforcement.
- A `high`/`critical` PR whose cross-family approval was satisfied only by a
  superseded approval will now WARN — that is the decided rule doing its
  job.
- #742's job-timeout and hang-stack legs were already delivered at master by
  PR #1034 (#1021); this PR pins them (frozen PRESERVING checks) and closes
  the remaining legs (step-level timeout, job-scope isolation, Node floor).
- The post-test hang-elimination AC (#742 AC4, host-only) is carried by
  #1021's named wedge classes and canary; the ≥25-clean-run batch accrues on
  CI history over time.
