# Backend lockfiles — regeneration, platform rule, rollback

Both backend lockfiles are **universal**: one lock per source spec that
resolves and installs on Linux (CI, Docker) **and** Windows (developer
workstations). Platform-specific packages carry environment markers
(`uvloop` and the `nvidia-*` CUDA family are `sys_platform`-gated;
`tzdata`/`colorama` arrive win32-marked), produced by the resolver rather
than by hand. Issue #567 (C15/E10) replaced the earlier Linux-only
pip-compile locks; the historical "Windows trap" is documented at the end.

## The four files

| File | Role |
|------|------|
| `backend/requirements.txt` | Production source spec (edited by humans/dependabot) |
| `backend/requirements-ci.txt` | CI source spec — excludes heavy packages stubbed by `conftest.py` |
| `backend/requirements-lock.txt` | **Generated** from `requirements.txt` — every entry `--hash=sha256`-pinned, cross-platform via markers |
| `backend/requirements-lock-ci.txt` | **Generated** from `requirements-ci.txt` — every entry `--hash=sha256`-pinned, cross-platform via markers |

Never edit a lockfile by hand. Edit the source spec, then regenerate.
The 100%-hashed invariant is asserted mechanically by
`backend/tests/test_issue258_build_contracts.py::test_ac19_lockfile_entries_all_hash_pinned`,
and the universal-marker invariant by the `TestUniversalLockfiles` tests in
`backend/tests/test_lockfile_install.py`.

## CI verification (the contract to stay green)

The Backend CI job (`.github/workflows/ci.yml`, repository root) verifies BOTH
lockfiles on every push by running the documented regeneration script itself
on ubuntu and asserting a fixed point:

```bash
bash scripts/update-lock.sh
git diff --exit-code -- backend/requirements-lock.txt backend/requirements-lock-ci.txt
```

This proves both that the committed locks are a regeneration fixed point and
that `scripts/update-lock.sh` executes past its POSIX prologue (the CRLF defect
class from #751, pinned by `.gitattributes`). A lockfile out of sync with its
source spec — or regenerated with a different procedure or uv — fails before
any test runs. CI pins `uv==0.12.15` and the update scripts install the same
version for byte-diff parity. CI installs from `requirements-lock-ci.txt` with
`--require-hashes` plus `requirements-dev.txt` on Linux; the scheduled nightly
full-parser tier installs the **shipped** `requirements-lock.txt` with
`--require-hashes` and layers dev/OTel tooling on top under a `pip freeze`
constraint from that shipped environment plus `pip check`. The root Dockerfile
installs `requirements-lock.txt` with `--require-hashes`; resolution
parseability on any host is asserted by `backend/tests/test_lockfile_install.py`.
A lock-only push also triggers docker-smoke because the docker-scope path
filter includes both `backend/requirements.txt` and
`backend/requirements-lock.txt`.

## Regeneration procedure (any platform)

Universal locks resolve for all platforms at once, so regeneration no longer
requires Linux. Run from the repo root with uv (CI pins `uv==0.12.15`; the
update scripts install that version if uv is missing):

```bash
# Generate production lockfile (seeding -o with the existing lock keeps every
# pinned version; only markers and platform-conditional additions change)
uv pip compile --universal --generate-hashes --no-strip-extras --no-header \
    --python-version 3.11 backend/requirements.txt \
    -o backend/requirements-lock.txt

# Generate CI lockfile
uv pip compile --universal --generate-hashes --no-strip-extras --no-header \
    --python-version 3.11 backend/requirements-ci.txt \
    -o backend/requirements-lock-ci.txt
```

`scripts/update-lock.sh` (bash) and `scripts/update-lock.ps1` (PowerShell)
run the two commands for you. Version bumps do NOT ride along with unrelated
regenerations: uv prefers the pins already present in the output file unless
you pass `--upgrade` (or edit the source spec bounds, which is the intended
way to move a version).

### Historical note: the pre-#567 Windows trap

The locks were previously generated with `pip-compile` **on Linux only**:
pip-compile resolves for the platform it runs on and strips markers, so
Linux-only wheels (`uvloop`, the `nvidia-*` CUDA family via
`unstructured[all-docs] → unstructured-inference → cuda-toolkit`) were
pinned without `sys_platform` markers and win32-only transitives (`tzdata`)
were dropped entirely. A Windows `pip install --require-hashes` then failed
— twice, for two distinct reasons (uvloop source-build error; no win32
distribution for `nvidia-cufile`). Commit da0afc4 (#353) had to redo the
locks "on python 3.11/linux" for the same reason. The universal procedure
above closes that class: regenerate anywhere, install anywhere.

## Update procedure (bumping a dependency)

1. Edit the **source spec** (`backend/requirements.txt` or
   `backend/requirements-ci.txt`) — both files mirror each other's bounds
   (issue #404/#391), so check whether the sibling file needs the same edit.
2. Regenerate **both** lockfiles (procedure above); diff and justify every
   delta beyond the edit you intended (marker/format-only churn is expected
   when the procedure changes, not when a version moves).
3. Verify the regeneration is a fixed point. **During a deliberate relock**,
   snapshot the intended regenerated locks, run the script a second time, and
   compare against that snapshot — *not* against HEAD, which still holds the
   old locks and would show your intended changes as drift:
   ```bash
   cp backend/requirements-lock.txt backend/requirements-lock.txt.intended
   cp backend/requirements-lock-ci.txt backend/requirements-lock-ci.txt.intended
   bash scripts/update-lock.sh
   diff -u backend/requirements-lock.txt.intended backend/requirements-lock.txt
   diff -u backend/requirements-lock-ci.txt.intended backend/requirements-lock-ci.txt
   rm backend/requirements-lock.txt.intended backend/requirements-lock-ci.txt.intended
   ```
   (`git diff --exit-code` against HEAD is CI's check on the clean committed
   tree, not the relock procedure.) Then install and test from the locks:
   ```bash
   cd backend
   # Hash-checking applies to EVERY requirement in the command, so the lock
   # installs separately from the unlocked dev tooling (as CI does).
   pip install --require-hashes -r requirements-lock-ci.txt
   pip install -r requirements-dev.txt
   pip install pytest-xdist
   pytest --tb=short -q tests/
   ```
4. Commit source spec + both lockfiles together in one commit (a lockfile
   without its source edit — or vice versa — is an inconsistent state that
   the CI byte-diff will reject).

## Rollback procedure

A bad lockfile change is reverted like any other git change — never
hand-edited back:

```bash
# Whole change (source spec + locks) — preferred
git revert <commit-that-bumped-the-dependency>

# Lockfiles only (e.g. a regeneration gone wrong, source spec is fine)
git checkout <last-known-good-sha> -- backend/requirements-lock.txt backend/requirements-lock-ci.txt
# then re-run the fixed-point verification from step 3 against the restored
# locks. Snapshot them first: HEAD still holds the bad locks, so comparing
# against HEAD would report the intended rollback as drift.
```

Because every entry is hash-pinned, `pip install -r requirements-lock*.txt`
after a rollback reproduces the exact previous dependency set — no resolver
surprises. Rolling back past the #567 change restores Linux-only locks and
re-breaks Windows installs (`uvloop` build failure, `nvidia-cufile`
resolution failure) — call that out if you revert it.
