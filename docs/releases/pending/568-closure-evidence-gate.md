# 568 — Closure-evidence gate (Workstream K, PR 6 of 7)

## What changed

- New CI workflow `.github/workflows/closure-evidence.yml` (`Closure
  evidence`) and gate script `scripts/check_closure_evidence.py` (issue #568,
  finding E13): a PR whose body says `Closes #N`/`Fixes #N` must name closure
  evidence — a backend pytest test id that `verify-test` re-executes at the
  pre-fix commit (must fail there) and at the PR head (must pass), or an
  existing captured artifact. Issues labeled `high`/`critical` additionally
  require an approving review from outside the fix's own file family
  (mapping documented in `docs/ci/closure-evidence-gate.md`).
- The flagship motivating case (issue #343: closed 2026-07-08 recording a
  rename that landed only 2026-09-12 in 7c3d404b) is documented in the gate's
  documentation as the failure mode this gate exists to catch.
- New proving suite `backend/tests/test_closure_evidence_gate.py`; the
  `commit-pr` skill's PR-body template now carries a conditional `##
  Closure evidence` section for closing PRs; `CONTRIBUTING.md` and
  `AGENTS.md` mention the new gate; `scripts/check_pr_scope_drift.py`'s
  CI-contract list includes the new script.

## Operator-visible outcomes

- New (initially non-blocking) check run on PRs targeting master:
  `Closure evidence / Closure evidence gate`. Its log lines are prefixed
  `closure-evidence:` (OK / WARN / FAIL / ERROR / evidence-test).
- Rollout is **warn** for one release cycle: violations are visible as
  `closure-evidence: WARN` observations and never fail the check. The flip to
  **enforce** is a one-line default change in the workflow (`inputs.mode ||
  'warn'` → `'enforce'`), to be recorded here when it happens; enforce mode
  fails the check on violations (GitHub cannot block the merge-time
  auto-close itself — enforcement is the required failing check plus the
  documented reopen-on-merge procedure).
- Enforce-mode live-fetch failures fail closed (`closure-evidence: ERROR`,
  exit 1); warn mode never fails the check (rollout contract).

## Rollback

Disable or delete `.github/workflows/closure-evidence.yml`. The gate touches
no application code, schema, or config defaults; the script remains runnable
locally for verification (`python scripts/check_closure_evidence.py
evaluate --pr-data <snapshot.json> --mode warn`).
