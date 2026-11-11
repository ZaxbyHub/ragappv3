# Restore fail-closed SAST baseline-growth gating (follow-up to #1093)

Follow-up from PR #1093 (the SAST env-line Considered row in that body asked
for exactly this decision). Supersedes the temporary
`SAST_ALLOW_BASELINE_EXPANSION=1` wiring that PR #1093 left in ci.yml.

## Summary

PR #1093 needed three constants-only B608 suppressions in
`bandit-baseline.json` and set the expansion env permanently on the
`check_sast_baseline` invocation to admit them. This change restores
fail-closed growth gating now that master's baseline contains those entries,
and adds the atomic re-anchor recipe the final-critic ordering discipline
called for.

One issue = one PR.

## Changes

- **ci.yml**: the Quality-contracts `SAST baseline integrity check` step runs
  `python scripts/check_sast_baseline.py` without the env — growth fails the
  job again (C-002), printing the newly-suppressed IDs.
- **justfile**: the `quality-contracts` recipe mirrors CI (plain invocation),
  and a new `sast-regate` recipe encodes the re-anchor ordering discipline
  (`--update-baseline` → gated scan → review the baseline diff → commit)
  so the stale-capture failures seen during #1093's review cannot recur.
- **Mirrors reverted**: CONTRIBUTING.md, both contributing skill copies, the
  ci-fix-monitor skill, and the ci-fixer agent go back to the plain
  invocation, matching CI.
- **backend/security/README.md**: the "Baseline expansion is CI-enforced"
  section is restored (with a note recording the brief #1093 override).

## Acceptance criteria

- [ ] AC1: WHEN a PR grows `bandit-baseline.json`'s finding count, THE SYSTEM
  SHALL fail the Quality-contracts job with C-002 naming the
  newly-suppressed IDs (no env set in ci.yml).
  - class: DISCRIMINATING
  - argv: bash -lc "cd backend && python ../scripts/check_sast_baseline.py; echo exit=$?"
  - expect: C-002
- [ ] AC2: WHEN `just sast-regate` runs on a tree with shifted baselined
  lines, THE SYSTEM SHALL regenerate the baseline, pass the gated scan, and
  print the baseline diff for commit.
  - class: PRESERVING
  - argv: bash -lc "just sast-regate"
  - expect: run_bandit: PASS
- [ ] AC3: WHEN a finding is absent from the committed baseline, THE SYSTEM
  SHALL fail the SAST job regardless of the env (run_bandit.py gate
  unchanged).
  - class: PRESERVING
  - argv: bash -lc "python scripts/run_bandit.py"
  - expect: no new findings

## Verification

- Local: `python scripts/check_sast_baseline.py` exits 1 (128 vs 125 on the
  pre-#1093 base — the +3 are the justified constants-only B608s already on
  master); with the env it exits 0. `python scripts/run_bandit.py` PASS.
- The three suppressed IDs remain the ones justified in PR #1093
  (constants-only SQL construction, no dynamic values); no new suppressions
  are added by this PR.

## Scope

In: ci.yml env revert, mirror reverts, sast-regate recipe, README restore,
release fragment. Out: the three B608 suppressions themselves (already
on master), the #1093 release fragment (historical record, unchanged).
