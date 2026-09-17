# build: universal lockfiles, justfile, devcontainer, and documented Windows seams (#567)

## What changed

- **Backend lockfiles are universal** (issue #567 / C15+E10): both
  `requirements-lock.txt` and `requirements-lock-ci.txt` are regenerated with
  `uv pip compile --universal --generate-hashes`, so a single lock per source
  spec installs on Linux (CI, Docker) **and** Windows. `uvloop` and the
  `nvidia-*` CUDA family now carry `sys_platform` markers; `tzdata` and
  `colorama` arrive as win32-conditional entries. No pinned version changed
  and nothing was dropped (per-lock diff: 0 changed / 0 dropped / 3 added,
  all platform-conditional); existing `--hash` sets are identical. The CI
  lock also normalizes from mixed CRLF to LF.
- **CI verification moved from pip-compile to uv**: the Backend job installs
  `uv==0.12.15` and verifies **both** locks by seeded regeneration +
  `git diff --no-index --exit-code` (the CI lock previously had no freshness
  gate). `scripts/update-lock.sh` / `.ps1`, `docs/engineering/lockfiles.md`,
  and `backend/README.md` document the same procedure.
- **Base-path validation errors now name the received value** in all three
  copies (`vite.paths.ts`, `src/lib/normalize-base-path.ts`,
  `frontend/Dockerfile`), so Git Bash's MSYS rewrite of
  `VITE_APP_BASENAME=/knowledgevault` into `C:/Program Files/Git/...` is
  visible in the error instead of a bare "unsafe characters".
- **Windows seams documented**: `MSYS_NO_PATHCONV=1` note in INSTALLATION.md,
  `docs/engineering/testing.md`, and the `ci-compatibility-audit` skill (all
  three mirrored trees); CONTRIBUTING.md states Windows (Git Bash, CRLF) is a
  supported development environment with the seams listed (the §11 Q8
  assumption from the issue — maintainer may veto).
- **Additive tooling**: a root `justfile` mirrors every ci.yml step
  (`just ci` runs backend + frontend + quality contracts + SAST; subpath
  recipes set `MSYS_NO_PATHCONV=1` automatically), and
  `.devcontainer/devcontainer.json` pins CI's runtimes (Python 3.11, Node
  22.14, ubuntu-24.04) — now a required surface in
  `scripts/check_runtime_contract.py`, with tests in
  `backend/tests/test_devcontainer_contract.py`.
- **New/strengthened tests**: `test_lockfile_install.py` parseability tests now
  pass `--require-hashes`; `TestUniversalLockfiles` asserts the markers
  (regenerating with a Linux-only resolver turns it red);
  `frontend/src/base-path-message.test.ts` asserts value interpolation for
  both TS validators and the Dockerfile copy.

## Why

A Windows developer could not `pip install --require-hashes` either lock
(two distinct failures: uvloop source-build error in the CI lock;
no win32 distribution for `nvidia-cufile==1.15.1.6` in the prod lock), and
the documented subpath build failed under Git Bash with an undiagnosable
error. The proving tests failed on any Windows host and passed on Linux CI,
which is why the class survived unnoticed (audit findings C15/E10).

## Known limitations

- `uv` is pinned (`0.12.15`) in CI; version bumps should regenerate both
  locks with the new uv and re-verify byte-identity (the gate does this).
- The MSYS rewrite itself is a Git-for-Windows behavior this repo cannot
  change; the fix makes it diagnosable and the justfile/skill/docs carry the
  `MSYS_NO_PATHCONV=1` workaround. Plain-Git-Bash subpath builds still fail
  by design when the variable is not set — now with the received value in
  the message.
- Rolling back the lock change restores the Windows install failures.
