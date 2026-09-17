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
and the universal-marker invariant by
`backend/tests/test_lockfile_install.py::test_locks_are_universal`.

## CI verification (the contract to stay green)

The Backend CI job (`.github/workflows/ci.yml`, `working-directory: backend`)
verifies BOTH lockfiles on every push: each lock is seeded into a scratch
file, re-resolved with the same `uv pip compile --universal` invocation used
for regeneration, and byte-compared (`git diff --no-index --exit-code`) to
the committed lock. A lockfile that is out of sync with its source spec (or
was regenerated with a different procedure) fails the job before any test
runs. CI installs from `requirements-lock-ci.txt` (with `--require-hashes` —
the hash gate is enforced at the real consumer) + `requirements-dev.txt` on
Linux, and the root Dockerfile installs `requirements-lock.txt` with
`--require-hashes`; resolution parseability on any host is asserted by
`backend/tests/test_lockfile_install.py`.

## Regeneration procedure (any platform)

Universal locks resolve for all platforms at once, so regeneration no longer
requires Linux. Run from the repo root with uv (0.12.x; `pip install uv` or
the standalone binary):

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
3. Verify exactly what CI verifies (seed + re-resolve + byte-diff), then
   install and test from the locks:
   ```bash
   cd backend
   pip install -r requirements-lock-ci.txt -r requirements-dev.txt
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
# then confirm consistency the same way CI does (seed + re-resolve + byte-diff)
```

Because every entry is hash-pinned, `pip install -r requirements-lock*.txt`
after a rollback reproduces the exact previous dependency set — no resolver
surprises. Rolling back past the #567 change restores Linux-only locks and
re-breaks Windows installs (`uvloop` build failure, `nvidia-cufile`
resolution failure) — call that out if you revert it.
