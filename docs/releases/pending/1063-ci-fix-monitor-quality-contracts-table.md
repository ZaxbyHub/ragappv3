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

None — documentation-only change. No behavior, config, or CI workflow
surfaces changed.

## Known caveats

- The enforcement scope of `scripts/check_runtime_contract.py` is
  intentionally unchanged: it still governs only
  `docs/engineering/testing.md` and `docs/engineering/conventions.md`.
  Extending inventory enforcement to skill tables is a separate product
  decision (out of scope per #1063), so the refreshed table can drift
  again the next time a contract script is added — re-run the #1063
  discriminating check (diff of `scripts/check_*.py` mentions between
  `.github/workflows/ci.yml` and the SKILL.md) when adding a gate.
- The one-line purposes are mirrors of the ci.yml step names, not
  full operator runbooks; `ci-compatibility-audit` remains the local
  reproduction path for each gate.
