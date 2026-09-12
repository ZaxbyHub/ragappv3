# Backend lockfiles — regeneration, platform rule, rollback

ENH-001 (#258): both backend lockfiles were already 100% hash-pinned, but the
regeneration procedure and the platform trap were undocumented in-repo. This
page is that procedure.

## The four files

| File | Role |
|------|------|
| `backend/requirements.txt` | Production source spec (edited by humans/dependabot) |
| `backend/requirements-ci.txt` | CI source spec — excludes heavy packages stubbed by `conftest.py` |
| `backend/requirements-lock.txt` | **Generated** from `requirements.txt` — every entry `--hash=sha256`-pinned |
| `backend/requirements-lock-ci.txt` | **Generated** from `requirements-ci.txt` — every entry `--hash=sha256`-pinned |

Never edit a lockfile by hand. Edit the source spec, then regenerate.
The 100%-hashed invariant is asserted mechanically by
`backend/tests/test_issue258_build_contracts.py::test_ac19_lockfile_entries_all_hash_pinned`.

## CI verification (the contract to stay green)

The Backend CI job (`.github/workflows/ci.yml`, `working-directory: backend`)
verifies version-set consistency on every push:

```bash
pip install pip-tools
pip-compile --dry-run --no-header --allow-unsafe requirements.txt --output-file requirements-lock.txt
```

CI installs from `requirements-lock-ci.txt` + `requirements-dev.txt`, so a
lockfile that is out of sync with its source spec fails the job before any
test runs.

## Regeneration procedure — Linux/CI platform ONLY

Regenerate **only on Linux** (native, WSL, a Linux container, or a CI runner).
Run from the repo root with Python 3.11 (the resolution target —
`scripts/update-lock.sh` refuses to run otherwise):

```bash
python3.11 -m pip install --upgrade pip-tools

# Generate production lockfile
python3.11 -m piptools compile backend/requirements.txt \
    --output-file backend/requirements-lock.txt \
    --generate-hashes --no-header --allow-unsafe

# Generate CI lockfile
python3.11 -m piptools compile backend/requirements-ci.txt \
    --output-file backend/requirements-lock-ci.txt \
    --generate-hashes --no-header --allow-unsafe

# Verify exactly what CI verifies (dry-run must report no changes)
cd backend && pip-compile --dry-run --no-header --allow-unsafe \
    requirements.txt --output-file requirements-lock.txt
```

`scripts/update-lock.sh` (repo root) runs the two generation commands for you.

### The Windows trap (why the platform rule exists)

`pip-compile` resolves against the platform it runs on. The production
dependency chain `unstructured → unstructured-inference → cuda-toolkit →
nvidia-cublas / nvidia-cudnn-cu13 / cuda-bindings / …` resolves through
**Linux-only wheels**: those packages publish `manylinux` wheels and no
Windows wheels. Regenerating on Windows produces a platform-specific lockfile
that drops or rewrites the CUDA family — and then fails to install (or
resolves different versions) on the Linux CI runner and in the
`python:3.11-slim` Docker image.

This is not hypothetical — it is project history: the lockfiles were first
generated from a Windows machine and had to be redone "on python 3.11/linux
to avoid windows-only packages" (commit `da0afc4`, the #353 lockfile-pinning
work). Hence the rule: **committed lockfiles are Linux-resolved.**

`scripts/update-lock.ps1` exists for Windows convenience (local installs),
but its output must NOT be committed. If you develop on Windows and need to
bump a dependency, regenerate inside WSL or a Linux container — or let CI /
dependabot (whose `pip-compile` runs execute on `ubuntu-latest`) produce the
change.

## Update procedure (bumping a dependency)

1. Edit the **source spec** (`backend/requirements.txt` or
   `backend/requirements-ci.txt`) — both files mirror each other's bounds
   (issue #404/#391), so check whether the sibling file needs the same edit.
2. Regenerate **both** lockfiles on Linux (procedure above).
3. Verify the dry-run reports no changes, then install and test from the
   locks:
   ```bash
   cd backend
   pip install -r requirements-lock-ci.txt -r requirements-dev.txt
   pytest --tb=short -q tests/
   ```
4. Commit source spec + both lockfiles together in one commit (a lockfile
   without its source edit — or vice versa — is an inconsistent state that
   the CI dry-run will reject).

## Rollback procedure

A bad lockfile change is reverted like any other git change — never
hand-edited back:

```bash
# Whole change (source spec + locks) — preferred
git revert <commit-that-bumped-the-dependency>

# Lockfiles only (e.g. a regeneration gone wrong, source spec is fine)
git checkout <last-known-good-sha> -- backend/requirements-lock.txt backend/requirements-lock-ci.txt
# then confirm consistency the same way CI does:
cd backend && pip-compile --dry-run --no-header --allow-unsafe \
    requirements.txt --output-file requirements-lock.txt
```

Because every entry is hash-pinned, `pip install -r requirements-lock*.txt`
after a rollback reproduces the exact previous dependency set — no resolver
surprises.
