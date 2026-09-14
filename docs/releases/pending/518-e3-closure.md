# 518 — Workstream E3 closure: tracing spans, per-stage durations, admission recovery, promote gating, real-LanceDB restore drill, generation-bound manifests

## What changed

- **OTel spans (tracing half of the amendment)**: `telemetry.py` gains an
  optional-import tracer layer (`get_tracer`/`start_span`/`register_span_middleware`)
  plus a new `TelemetrySpanMiddleware` (one `http.*` server span per request),
  `gen_ai` client spans at the LLM / embeddings / rerank chokepoints
  (`gen_ai.operation.name`, `gen_ai.request.model`), and outbound `traceparent`
  now derived from the ACTIVE span context when tracing is live (deterministic
  per-turn synthesis remains the fallback). The OTLP bridge also exports a
  `gen_ai.client.operation.duration` counter by operation name. All of it rides
  the pinned optional extra (`requirements-otel.txt`); without the extra the
  tracer is a no-op shim and nothing imports `opentelemetry`.
- **Per-stage durations**: the engine records `planning`, `searching`,
  `reading` (new) alongside the existing `generation` — literal stage names on
  the request-local recorder (function-local timers; no shared-state races).
- **Admission recovery**: a successful store operation (occupancy/acquire or
  lease refresh) clears the degraded flag with a log-once recovery line —
  `backend unavailable/recovered` is now a real transition, not a sticky flag.
- **Promote-memory gating**: the interactive `POST /api/wiki/promote-memory`
  route (curator LLM inference) acquires the shared BACKGROUND admission
  budget like the compile processors (route-level; no double-acquire).
  Saturation surfaces HTTP 503.
- **Real-LanceDB restore drill**: `test_518_restore_drill_real.py` runs the
  full backup→mutate→restore→query cycle against real lancedb — WAL-pinned
  SQLite snapshot under an open transaction, post-tag writes, tag checkout,
  row-identity verification, and post-tag writes absent from the restored
  generation. This drill caught and fixed a real bug: `backup_set.py` called
  `table.create_tag(...)`, which does not exist on real lancedb ≥0.36 (the
  shipped tests used fakes); it now uses the real `table.tags.create` API
  (fake-compatible path retained).
- **Generation-bound manifests**: `backup_set.py` reads the LATEST
  `index_generation` journal row FROM THE SNAPSHOT BYTES and records it in the
  lancedb manifest item; `restore.py` verifies the restored SQLite contains
  that exact journal row (named `RestoreError` on mismatch — an
  interleaved-write set can no longer silently restore an incoherent
  sqlite/vector pair). Legacy manifests without the field restore with a
  WARNING.
- **Docs**: admin-guide pointer to the operations guide
  (admission/telemetry/maintenance); operations.md tracing section.

## Migration

- No schema, key, or API-shape changes. Manifest schema gains an optional
  `generation` field on lancedb items (reader-tolerant).

## Breaking changes

- None. `POST /api/wiki/promote-memory` can now return 503 under admission
  saturation (previously it always ran ungated).

## Rollback

- Revert the diff; the bandit baseline regeneration in this PR is pure line
  drift (137→137 findings, verified signature-identical).

## Known caveats

- Span attributes and gen_ai metric names follow the Development-status
  GenAI semantic conventions (pinned extra; do not treat as stable).
- The generation binding is read from the snapshot at backup time; a set
  created by pre-closure drafts of `backup_set.py` — or any snapshot taken
  before an index generation has ever been published (fresh installs) —
  restores with a WARNING (no coherence check). No released set exists.
  The restore-side comparison is a structural sanity check between the
  manifest binding and the restored journal (latest id), not tamper
  proofing: a fully doctored set could rewrite both sides.
