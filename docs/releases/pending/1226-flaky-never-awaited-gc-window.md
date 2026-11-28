# Never-awaited warning checks scoped to their own coroutines (#1226)

## What changed

- `backend/tests/test_c08_admission_rejected_cleanup.py::test_rejection_leaves_no_unawaited_embed_coroutines`
  now runs one `gc.collect()` inside a discard-only
  `warnings.catch_warnings(record=True)` block BEFORE entering the real
  capture window, and the assertion only counts "never awaited"
  RuntimeWarnings whose message names the engine's own embed coroutine
  (`_embed_one`, the #713 signal) instead of any "never awaited" warning.
- `backend/tests/test_issue558_c13_stub_query_chain.py::test_stub_query_chain_runs_on_builder_without_never_awaited_warning`
  and
  `backend/tests/test_issue558_c13_real_table_primary_query.py::test_real_table_primary_query_finds_sentinel_beyond_fallback_window`
  got the identical pre-flush, and their assertions now only count
  coroutines from their own chain: the stub test's doubles
  (`_StubTable.*` / `_RecordingBuilder.*`) and, for the real lancedb table,
  the qualnames the code under test can create on the CI-pinned lancedb
  0.41.0 (`AsyncTable.search`, `AsyncTable.head`,
  `AsyncQueryBase.to_list`).
- The "admission-cleanup load sensitivity" row of the #1101 flake
  signature table in `.agents/skills/ci-compatibility-audit/SKILL.md` now
  records the diagnosed mechanism (the GC-window attribution above, seen
  on PR #1139, PR #1172 and PR #1218) in place of "Mechanism undiagnosed";
  its signature and discriminator markers are unchanged, so the
  `backend/tests/test_1101_flake_signature_table.py` content pin still
  passes.

## Why

These three tests call `gc.collect()` INSIDE their
`warnings.catch_warnings(record=True)` block and then fail on ANY
"never awaited" RuntimeWarning. None of them uses `AsyncMock`, so the
`coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` warnings
seen in CI (Backend jobs of PR #1139, PR #1172, PR #1218 — all with
diffs that touch no application code) are un-awaited mock coroutines leaked
by EARLIER tests on the same xdist worker, held alive in reference cycles
and finalized by the victim test's in-window `gc.collect()`. That is
order-dependent cross-test pollution misattributed to a green-diff PR.
Reproduced deterministically with a seed test that plants four such cycles
(four `AsyncMockMixin._execute_mock_call` warnings, the exact CI failure
count) before each victim; all three go green with the fix while the
pollution is still present.

The discriminating power of all three tests is preserved: at the #713 base
commit 465f76d2 the c08 test still fails on
`coroutine 'RAGEngine.query.<locals>._embed_one' was never awaited`, and at
the #558 base commit 2a7732a1 both #558 tests still fail (`assert False is
True` via the fallback, with the base chain's abandoned `search()`
coroutine still matching the new filters). The session-wide #565
never-awaited gate in `backend/tests/conftest.py` is unchanged: the
pre-flush runs inside a recording block, so no warning escapes to pytest's
recorder, and real (non-mock) dropped coroutines still fail the suite.

## Migration steps

None — test-only and docs-only change; no runtime, config, API, or schema
surface is touched.

## Known caveats

- `backend/tests/test_safe_concurrent_ingestion.py::test_start_publication_failure_rolls_back_and_can_retry`
  has the same in-window `gc.collect()` + any-"never awaited" shape (it has
  not failed in CI: the coroutine it checks is one it creates itself). It
  is deliberately out of this change's scope per issue #1226 (which names
  the three failing tests); if it ever flakes the same way, apply the same
  pre-flush + own-qualname filter there.
- The real-table filter's qualnames are pinned to lancedb 0.41.0
  (`backend/requirements-lock-ci.txt`); a future lancedb bump that renames
  those coroutines would make that test's warning assertion vacuously
  green, while its primary discrimination (`assert result is True` via the
  primary query path) is unaffected.
