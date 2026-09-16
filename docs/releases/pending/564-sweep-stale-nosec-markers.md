# 564 — SAST gate fails on unused `# nosec` suppressions; marker population audited

**Issue:** #564 (Workstream K, PR 2 of 7; finding C20)
**Rebaseline:** 2026-09-11 · **Roadmap index:** #574

## Outcome

The bandit SAST gate (`python scripts/run_bandit.py`, CI job `sast`) now fails
when a `# nosec` marker suppresses nothing, in addition to the existing
new-finding baseline diff. A marker placed on a line where the flagged test
does not fire could previously sit forever and silently mask a genuinely new
finding introduced later on the same physical line — the gate's baseline diff
could never see it, because a suppressed finding never enters the diff.

## What changed

- `scripts/run_bandit.py`: `gated_scan()` cross-checks bandit's
  `nosec encountered ..., but no failed test` warnings (the only surface where
  bandit reports marker usage) against a follow-up `--ignore-nosec` scan of the
  same targets. A warned site is a true unused suppression — gate failure with
  the site named — only when the un-suppressed scan has no finding of that test
  id at that exact line. The cross-check is required because bandit 1.9.x emits
  the warning per *non-firing context*, so markers that suppress live findings
  also warn (verified against bandit 1.9.4's `core/tester.py` and empirically:
  39 warnings on this tree, all cross-checked live). The extra scan runs only
  when warnings exist. `--update-baseline` prints an advisory naming warning
  sites; both failure paths are reported together in one run.
- Marker audit across the scanned targets (`backend/app`, `scripts/backup_set.py`,
  `scripts/restore.py`): 42 markers at base. Empirical disposition (delete and
  re-scan): 35 markers suppress live B608 findings on safe, parameterized SQL —
  retained; 17 sites gained explicit per-site rationale comments in this change
  (14 in `draft_store.py`, 3 in `draft_pipeline.py`), while the rest already
  carried trailing or adjacent rationale (the six bare `# nosec B608` markers
  in `draft_store.py` sit directly beneath existing "clause is built only from
  literal fragments" comments) — the issue's original
  "every marker suppresses nothing" claim is refuted by the live run, as the
  maintainer comment anticipated. 3 markers suppressed nothing
  (`backend/app/models/database.py:1648`, `:2659`, `backend/app/services/draft_store.py:2864`)
  and are removed with their safety rationale kept as plain comments.
  The 4 live B110/B311 markers (best-effort `except` drains, seeded eval RNG)
  are retained with justifications. Markers under `backend/tests/` are outside
  the scan scope (`backend/.bandit` excludes `tests`) and are untouched.
- Tests: `backend/tests/test_run_bandit_nosec_gate.py` (net-new) proves the
  dead-marker failure, the live-marker pass (warning cross-check), the intact
  new-finding path, both failure paths reported together, the warning parser's
  dedup/normalization, and the regen advisory.
- Docs: `CONTRIBUTING.md` and `AGENTS.md` gate descriptions updated.

## Known limitations

- Bandit's warning stream names candidate dead sites only for lines some
  context reaches; a marker on a line no context ever touches (e.g. a bare
  `# nosec B608` on a closing parenthesis with no warned sibling) is not
  reported by bandit and therefore not gated — the three removed here were
  caught by this PR's manual audit, not by the gate.
- Each warned-line scan pays a second bandit run (`--ignore-nosec`); the clean
  no-warning steady state pays nothing extra.

## Rollback

Revert this PR's single commit; restoring any removed marker and re-running
`--update-baseline` reverts cleanly.
