# ci: make check_test_collection_scope.py gitignore-aware, then harden it per swarm review

## What changed

`scripts/check_test_collection_scope.py` (issue #656) derived its candidate set from git's
view of the repository (`git ls-files -z` plus `git ls-files --others --exclude-standard -z`)
instead of a raw filesystem walk, so gitignored local run artifacts — pytest-xdist leftovers
under the ignored `backend/data/` — stop reddening the Quality-contracts gate on developer
machines while CI stays green; the original walk is preserved byte-for-byte as the degraded
fallback when git cannot answer, announced with one stderr line.

`backend/tests/test_check_test_collection_scope.py` (new) pins the contract: gitignored
artifacts are not violations, untracked and tracked offenders still fail and are named,
degraded mode announces itself and keeps flagging real files, the documented hidden-tree
carve-out stays exempt, tracked-but-deleted files are not on-disk violations, NUL tokens are
never stripped, `PRUNE_DIRS` is degraded-mode-only, and the gate is cwd-independent.

## Follow-up hardening (PR #664 swarm-review findings)

After review, two MEDIUM findings were fixed in the same change: non-UTF-8 bytes in git
output no longer crash the gate instead of degrading (`errors="surrogateescape"` plus a
widened except clause and a `stdout` guard, with tests), and the degraded walk's `PRUNE_DIRS`
pruning is now pinned by a test. The remaining review items are documentation-level and are
resolved in-code: comments record the deliberate conservative degradation when either of the
two git calls fails and the 2 × 60s aggregate worst case, the stale "Run from the repository
root" tail is corrected, and a nonexistent root returns "no violations" rather than a
traceback.

## Why

The pre-fix gate conflated "on disk" with "in the repository": a filesystem walk sees
gitignored artifacts and misses tracked hidden-tree files, while the enforced contract (#563 /
C11) is about files a contributor could commit and that `pytest tests/` would silently never
collect. A guard that fails daily on artifacts that cannot be committed trains contributors
to ignore it.

## Migration

None. The CI and justfile invocations (`python scripts/check_test_collection_scope.py` from
the repo root) are unchanged.

## Caveats

- One tracked, never-collected test file remains exempt under the documented hidden-tree
  carve-out (`.opencode/skills/codebase-review-swarm/tests/test_skill_helpers.py`); whether to
  move, sanction, or delete it is a #563 contract decision, out of scope here and flagged for
  a follow-up.
- In git mode a *tracked* `venv/` or `node_modules/` test file is now flagged (the hardcoded
  prune list is degraded-mode-only) — committing a venv is itself the violation.
