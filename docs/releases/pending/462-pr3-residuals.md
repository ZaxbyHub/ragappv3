# Workstream B3 residuals: truthful vision dedup trace counts and test-flag hygiene (Issue #462, PR 3 of 3)

## What changed

### Backend
- `VisionEvidenceService.run()` (`backend/app/services/vision_evidence.py`) now
  assigns `VisionEvidenceResult.deduped = eligible - unique-selected` BEFORE the
  independent per-batch cap is applied. Previously the field was declared,
  mapped into `RAGTrace.vision_deduped`, and serialized on the wire — but never
  assigned, so every trace reported `vision_deduped=0` even when duplicate
  artifact sources had been removed (OBS-003). The assignment sits before the
  feature-off early return, so the counters stay truthful with query-time
  vision disabled (the default); counting is pure and performs no I/O.

### Tests
- `backend/tests/test_vision_evidence.py` (TEST-004 residue): removed the
  class-wide `setUp` True-pin on `settings.multimodal_query_vision_enabled`;
  every feature-flag mutation in `TestRunDegradation` is now a scoped
  `patch.object(settings, ...)` that restores the incoming value. The
  no-eligible scenario explicitly enables the feature for its own scope, so it
  passes alone in a fresh process (production default is off). No assertions
  changed; all 36 pre-existing tests still pass.
- New `backend/tests/test_vision_trace_dedup.py`: pins the OBS-003 numbers
  (3 eligible / 1 duplicate / cap 1 → eligible=3, selected=2, deduped=1,
  capped=1; unique-only control deduped=0), the feature-off counter
  truthfulness, the `RAGTrace.to_dict()["vision_deduped"]` wire emission, and
  a source-inspection pin on the rag_engine trace-mapping line.
- New `backend/tests/test_vision_evidence_scenarios.py`: registers the
  issue-required scenarios that had no coverage — capped winner set, mixed
  partial-availability batch (used / provider outage / empty response in one
  run), cancellation (CancelledError propagates; shared client closed exactly
  once), concurrent batches (within-batch semaphore honored, counters
  independent), pre-multimodal source-JSON round-trip, artifact-field
  save/load/fork survival through the FastAPI route layer, stream vs
  non-stream artifact-source parity, text-only feature-off non-regression, and
  the equal-proxies identity contract (`source_dedup_key` vs
  `RAGSource.artifact_identity_key` rebuild-key consistency).
- New `backend/tests/test_settings_leak_guardrail.py` + autouse fixture
  `_guard_multimodal_vision_flag` in `backend/tests/conftest.py`: any backend
  test that leaves `settings.multimodal_query_vision_enabled` mutated now fails
  loudly (the TEST-004 defect class: raw settings-singleton assignments leaking
  feature state across tests).

### Docs
- This note (new file); the pre-existing
  `docs/releases/pending/462-multimodal-query-vision.md` is untouched.

## Why
Issue #462 (workstream B3) assigns two residuals to this slot: OBS-003 (vision
traces underreport deduplication, making selected/capped counts misleading when
troubleshooting repeated artifact sources) and TEST-004's mandated repair form
(scoped patches restoring the original setting for each feature-off test). The
dedup counter was wired end-to-end from `RAGTrace` to the wire but fed a
constant 0; the trace now reports the issue's exact required semantics
(`deduped = eligible − unique-selected` before the independent cap). The flag
hygiene removes the last raw settings-singleton assignments in the vision test
file and installs a repo-wide regression guard for the class. Verification
scope (audit table, browser journey) is recorded in the PR.

## Rollout / rollback
No config, wire-schema, or persistence change: `vision_deduped` was already
serialized (always 0) and now reports the truthful count. Rollback = revert the
single producer line and the test files.
