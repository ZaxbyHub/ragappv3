# Backend SAST (bandit)

This directory holds the committed bandit SAST baseline and documents how the
backend static-security gate works.

## Why a baseline

A naive "fail on any finding" SAST gate is useless on a codebase with pre-existing
findings: it floods and gets disabled. Instead, this repo runs a **baseline gate**
that fails CI only on *new* findings. Pre-existing findings are recorded in the
committed baseline ([`bandit-baseline.json`](./bandit-baseline.json)) and
suppressed; the gate surfaces regressions and newly-introduced issues, not legacy
debt.

## How to run

From the repository root:

```bash
# The gated scan CI runs (exits non-zero on NEW findings only)
python scripts/run_bandit.py

# Regenerate the committed baseline after intentionally accepting new findings
python scripts/run_bandit.py --update-baseline
```

`bandit` is declared in [`backend/requirements-dev.txt`](../requirements-dev.txt).

## What the baseline currently suppresses

Snapshot at the time this gate was introduced (issue #400 / #298):

| Severity | Count |
|----------|-------|
| HIGH     | 9     |
| MEDIUM   | 59    |
| LOW      | 72    |
| **Total**| **140** |

across 35 files under `backend/app`. The dominant finding families are:

| Bandit ID | Meaning | Count |
|-----------|---------|-------|
| B608 | Hardcoded SQL expressions (string-built queries) | 59 |
| B105 | Hardcoded password / secret string | 33 |
| B110 | `try/except/pass` (errors swallowed) | 33 |
| B324 | Weak hashlib import/use | 9 |
| others | misc | 6 |

**These are tracked pre-existing debt, not accepted-as-safe findings.** Most B608
hits are parameterized queries whose dynamic *placeholder string* trips the
heuristic (the values are still bound, not interpolated); each should still be
individually confirmed. Remediation is intentionally out of scope for the process
PR that introduced this gate — file follow-up issues to whittle the counts down.

## Regenerating the baseline (reviewer guidance)

`python scripts/run_bandit.py --update-baseline` rewrites
[`bandit-baseline.json`](./bandit-baseline.json) and prints a diff of the finding
IDs that were **added** or **removed** relative to the previous baseline.

When a PR regenerates the baseline, the PR description MUST list every
newly-suppressed finding ID (`test_id` + `file:line`) with a one-line
justification. Suppressing a real new vulnerability silently defeats the gate.

Because the suppressed set is reviewable in this committed file and the
regeneration prints its diff, baseline "rot" is auditable: every bump is a
reviewable change to `bandit-baseline.json`.

### Baseline expansion is CI-enforced

Baseline growth *suppresses* findings, so it is gated, not just documented.
`scripts/check_sast_baseline.py` (run in the **Quality contracts** CI job) fails
the build when `bandit-baseline.json`'s finding count **increases** on a PR,
printing the newly-suppressed IDs. To accept an intentional expansion (e.g.
recording new pre-existing debt), set `SAST_ALLOW_BASELINE_EXPANSION=1` and
justify each newly-suppressed ID in the PR. The same check also asserts the
`sast` CI job still runs `run_bandit.py` and that the scan scope (`TARGET`) is
still `backend/app`, so the gate cannot be silently weakened by editing one
constant or replacing one command.

## Cross-platform note

The committed baseline stores paths with forward slashes (e.g.
`backend/app/services/wiki_store.py`) so it is stable across platforms.
`scripts/run_bandit.py` normalizes path separators when comparing, so the gate
behaves identically on Linux CI and Windows/macOS local runs.

## Known limitations (residual risks)

These are inherent to a committed-baseline SAST gate and are accepted as the
v1 tradeoff:

- **`[skip ci]` bypass (low):** a commit message containing `[skip ci]` skips
  the entire workflow, including the SAST job. This is standard GitHub Actions
  behavior that applies to every job equally; it is not specific to this gate.
  Mitigation belongs to branch-protection / required-status-check policy
  (configurable on `master`), not to the workflow itself.
- **Scope is `backend/app` only (low):** `backend/embedding_server/` and
  `backend/scripts/` are not bandit-scanned. They remain under `ruff check .`
  (the whole `backend/` tree), and `embedding_server` is not in the production
  deployment (`docker-compose.yml` runs HuggingFace TEI). Widening the scope is a
  reasonable follow-up; it was scoped to `backend/app` for the v1 gate.
- **Line-based keying coincidence (low):** two findings sharing an identical
  `(test_id, file, line_number)` collapse to one key. Bandit rarely emits two
  distinct findings on the same line/test, so this is theoretical. Line-based
  keying is required to avoid the false-negative hole where a new finding of an
  existing `test_id` in the same file is silently suppressed.
- **SAST runs on every PR (low):** the `sast` job has no `paths-ignore` filter,
  so it runs even on frontend-only changes (~14s). A paths filter risks blocking
  merges if the job later becomes a required status check, so the cheap runtime
  is preferred to the merge-blocking footgun.

