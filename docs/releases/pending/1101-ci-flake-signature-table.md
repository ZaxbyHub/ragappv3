# ci-compatibility-audit skill carries the Backend CI flake signature table + rerun decision rule (#1101)

## What changed

- `.agents/skills/ci-compatibility-audit/SKILL.md` gains a new section,
  "Backend CI flake signature table (issue #1101)", placed between
  "Environment caveats" and "Output". It is a purely additive docs change:
  no CI workflow, test, or production code changed, and the
  `.claude/skills/ci-compatibility-audit/` thin pointer is untouched (the
  canonical home of this repo-specific skill is `.agents/skills/`, per
  `docs/engineering/skill-conventions.md`).
- The table covers the five documented ragappv3 Backend flake families, each
  row carrying its log signature and its diff-foreign discriminator:
  1. xdist sqlite moving-set — `sqlite3.OperationalError: unable to open
     database file` via `get_connection` in
     `backend/app/models/database.py` (e.g. under
     `require_health_probe_auth` in `backend/app/api/deps.py`), a moving
     subset of failures on one xdist worker (PR #642).
  2. C32 embedding-reuse `main()` exit —
     `backend/tests/issue513_checks/test_c32_embedding_reuse_enrichment_retry.py`
     failing `assert main() == 0` with the `C32 CHECK: FAIL: ...` line.
  3. secretscan rglob-during-coverage-walk (#943) —
     `scripts/check_secretscan.py` crashing with `FileNotFoundError` from
     inside its repo-wide `Path.rglob` walk when a transient coverage-artifact
     directory vanishes mid-walk.
  4. Backend whole-job freeze with empty live log — the job dying at exactly
     its `timeout-minutes: 60` with all xdist workers silent and no
     pytest-timeout markers (2026-10-23 incident; master froze identically
     four minutes later).
  5. #1021 hang-class canary timeout — the env-gated canary step
     (`RUN_CI_HANG_CANARY=1`) failing
     `backend/tests/test_ci_hang_canary.py::test_wedge_starved_pool_join_aborted_by_configured_timeout`
     with `Failed: Timeout (>30.0s) from pytest-timeout` and a crashed worker
     (PR #1095 feedback push, run 42448434901; cleared by rerun with zero
     code change).
- Immediately before the rerun rule, the section states the classification
  protocol: compare the failed-test SET across reruns (a non-repeating,
  moving set is infra, not a regression), confirm the diff touches none of
  the failing surface, and only then reclassify a red Backend job as
  diff-foreign and run `gh run rerun <run-id> --failed`.

## Why

During the PR #1095 feedback cycle the Backend job failed on the #1021
canary family and the correct `gh run rerun --failed` action was taken from
scattered project-memory rows, not from any repo artifact — the skill a
pre-push auditor actually reads carried no flake-family signatures, no rerun
decision rule, and no diff-foreign classification protocol (issue #1101,
verified by grep at anchor 324fbb19). The signatures are now quoted into the
skill itself, so classification is mechanical and reproducible by anyone
with the repo, independent of out-of-repo memory.

## Migration steps

None — docs-only. For pre-push auditors: load the `ci-compatibility-audit`
skill and classify a red Backend job through the new table + protocol
instead of recalling incident memory.

## Known caveats

- The table is descriptive, not exhaustive: these are the five DOCUMENTED
  Backend families. A red job matching none of the rows is not automatically
  a flake — diagnose it (the `ci-fix-monitor` skill's classification steps
  apply).
- The signature quotes are pinned from incidents whose full evidence lives
  outside git (CI run logs and project-memory rows); the quotes are now the
  in-repo source of truth for classification.
- The whole-job-freeze row describes the pre-#1021 presentation; since PR
  #1034's `--timeout-method=thread`, most in-suite wedges convert into the
  named crashed-worker signature instead, so a modern silent-freeze log is
  the disclosed residual GIL-held class.
