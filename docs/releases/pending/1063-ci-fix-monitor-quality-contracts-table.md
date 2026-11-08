# ci-fix-monitor quality-contracts table refreshed to the eighteen-script set (#1063)

Issue: #1063 (follow-up from PR #1059, ASK-2 — stale skill surface).

## What changed

The quality-contracts row of the CI-surface table in
`.agents/skills/ci-fix-monitor/SKILL.md` now names all eighteen contract
scripts the `quality-contracts` job of `.github/workflows/ci.yml` runs,
in CI order, each with a one-line purpose taken from the job's step
names. The row previously named a stale five-script subset
(`check_config_contract.py`, `check_pr_scope_drift.py`,
`check_sast_baseline.py`, `check_secretscan.py`,
`check_test_collection_scope.py`), omitting thirteen gates including
`check_runtime_contract.py` (the first step) and the newest gate
`check_g06_setup_docs.py` (issue #738). The thin Claude Code adapter
(`.claude/skills/ci-fix-monitor/SKILL.md`) is a pointer and needed no
change; `.agents/skills/` is the canonical home per
`docs/engineering/skill-conventions.md`.

The refresh is pinned by a new regression test,
`backend/tests/test_1063_ci_fix_monitor_gate_table.py`: it asserts that
every `scripts/check_*.py` name appearing in `.github/workflows/ci.yml`
also appears in the canonical skill (the #1063 AC1 discriminating
predicate), plus a guard that the `| **Quality contracts** |` table row
itself exists. The test fails at the pre-fix skill content (13 gates
missing) and passes at head.

## Why

The skill is what an operator loads when triaging red CI on a PR. A table
that lists 5 of 18 scripts hides most of the gate set, so a red
quality-contracts step could fall outside the documented surface with no
diagnosis guidance. This was unenforced staleness:
`scripts/check_runtime_contract.py` validates the script inventory only
against `docs/engineering/testing.md` and
`docs/engineering/conventions.md`, never against skill tables, so nothing
flagged the drift when gates were added.

## Migration steps

None — no runtime behavior, config, or CI workflow surfaces changed. The
only added code is a file-reading regression test under
`backend/tests/`.

## Known caveats

- The enforcement scope of `scripts/check_runtime_contract.py` is
  intentionally unchanged: it still governs only
  `docs/engineering/testing.md` and `docs/engineering/conventions.md`.
  Extending inventory enforcement to skill tables is a separate product
  decision (out of scope per #1063). The new regression test closes the
  specific ci-fix-monitor drift gap that decision left open: adding a
  contract script to ci.yml without refreshing the skill table now
  fails `tests/test_1063_ci_fix_monitor_gate_table.py` in the Backend
  job.
- The one-line purposes are mirrors of the ci.yml step names, not
  full operator runbooks; `ci-compatibility-audit` remains the local
  reproduction path for each gate.
