# Nightly quality gates actually run, can fail, and are covered by the runtime contract; gate scripts fail closed and are tested

Issues: #739, #740 (Workstream H PRs 1-2 of 10, collated)

## What changed

### Nightly tiers (.github/workflows/nightly.yml, nightly-quality-gates.yml)

- `nightly.yml`'s upload-artifact step had `if-no-files-found: warn` indented at
  STEP level — an invalid Actions step key, so GitHub rejected the whole
  workflow file and the nightly tier silently never ran (since 2026-09-12).
  The key now sits under `with:` where it belongs.
- The mutation score step read `<module> <path>` scope lines into ONE
  variable, so the path word-split into extra positional argv and
  `scripts/mutation_score.py` exited 2 (argparse usage error) every night
  before computing any score. It now reads both tokens and passes a quoted
  argv array.
- The mutmut step wrote `backend/`-prefixed `source_paths` while its working
  directory was already `backend/`, so mutmut looked for
  `backend/backend/...` and found nothing. The prefix is stripped at use.
  The mutmut exit code is now recorded per module (`<module>.rc`) and the
  score step hard-fails on a nonzero rc — a crashed run can no longer
  masquerade as a passing score through partial stats.
- `stryker-frontend`'s `timeout-minutes: 120` equalled its observed real
  duration (106-120 min; 4 of 6 scheduled runs were cancelled by their own
  timeout). Raised to 180.
- The Schemathesis step captured its exit code with `|| true` +
  `PIPESTATUS`, which under `shell: bash` (pipefail) recorded 0 for a
  crashed run. The capture is now `set +e` / pipeline / immediate
  `PIPESTATUS[0]` / `set -e` — pipefail-independent — and the step comment
  no longer claims GitHub's default bash has pipefail on (it is
  `bash -e {0}`).

### New: gate liveness (scripts/check_gate_liveness.py, .github/workflows/gate-liveness.yml)

A daily workflow fetches the repository's scheduled run history (`gh api`,
default token, `actions: read` + `contents: read` only) and runs a
stdlib-only checker that fails naming every scheduled workflow whose last
successful scheduled run is older than 48h. The checker excludes itself
(its current invocation is the execution evidence) and fails closed on
undecidable `on:` blocks. A scheduled tier that silently stops scheduling
is no longer invisible — that is exactly how nightly.yml stayed dead for
two weeks.

### Runtime contract (scripts/check_runtime_contract.py)

- Runtime pins are now checked in EVERY `.github/workflows/*.yml`, not only
  `ci.yml` (nightly.yml, nightly-quality-gates.yml and closure-evidence.yml
  each pin runtimes independently).
- Dockerfile `@sha256:` digests are now COMPARED against a recorded
  `ALLOWED_DIGESTS` table for tracked runtime images (node/python) — a
  tag bump with a stale digest no longer passes. Non-runtime base images
  (nginx, ollama) stay outside the contract, as before.
- The dead `CONTRIBUTING_NODE_RE` module constant is gone:
  `check_contributing` now uses it (previously it compiled its own local
  copy and editing the constant changed nothing).

### Gate scripts fail closed and are tested (#740)

- NEW shared `scripts/_gate_git.py`: bounded (60s), OSError-tolerant,
  NUL-delimited git plumbing distilled from check_test_collection_scope.
  Adopted by check_sast_baseline, check_secretscan and check_pr_scope_drift:
  a missing git binary now degrades to a gate message instead of a
  traceback, a hung git cannot stall the Quality job, and non-ASCII tracked
  paths arrive raw instead of `core.quotePath` C-quoted (a quoted path
  could never match a glob or prefix).
- `check_sast_baseline` C-005 binds the `run_bandit.py` run step to the
  `sast` job's own block — previously ANY `run:` line anywhere in ci.yml
  satisfied it.
- `run_bandit.py` fails when bandit's JSON `errors` array names unparseable
  files (they were silently unscanned and the gate printed PASS).
- `check_secretscan` prunes `node_modules`/venv trees from its on-disk walk
  (was ~36s on a dev checkout) and validates every `.secretscanignore`
  glob at parse time — a malformed glob (e.g. `[z-a]`) is now a
  C-SSECRETSCAN-1 finding and exit 1 instead of an uncaught traceback, and
  the invalid pattern can never re-raise from a later pass.
- `check_pr_scope_drift`'s auth-runtime list names the REAL modules:
  `backend/app/api/deps.py` added; the phantom `backend/app/deps.py` and
  `backend/app/models/auth` removed; `backend/app/services/auth` retained
  (a live prefix covering auth_service.py and authz_policy.py).
- `check_test_collection_scope` derives pytest's collection patterns from
  `backend/pyproject.toml` (`python_files = ["test_*.py"]`): a test-shaped
  file pytest would never collect — e.g. `backend/tests/foo_test.py` — is
  now flagged wherever it lives, not silently skipped. Missing/unparseable
  pyproject keeps the legacy behavior.
- Four previously untested gate scripts are pinned by new test modules
  (test_check_config_contract / test_check_pr_scope_drift /
  test_check_sast_baseline / test_check_batch_mock_seq), each with a happy
  path and a seeded violation; check_batch_mock_seq had also lost all test
  references (drift beyond the issue's census of three).

## Upgrade notes

Nothing to do: all changes are CI/gate-side. Behavior visible to operators:
the nightly tiers can now actually fail (that is the point), the new
"Gate liveness" scheduled workflow goes red naming stale scheduled
workflows, and the SAST gate fails instead of passing silently when bandit
cannot parse a file.
