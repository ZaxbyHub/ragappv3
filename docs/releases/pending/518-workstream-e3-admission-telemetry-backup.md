# 518 — Workstream E3: shared inference admission, durable operations and useful telemetry

## What changed

- **OBS-002 fix (request-local metrics)**: failed chat streams no longer
  report the previous successful query's metrics. Generation metrics and
  distillation provenance are bound per request (contextvars) in
  `backend/app/services/rag_engine.py`; failing attempts are captured before
  the terminal `done`; the legacy `engine._last_*` attributes remain as
  success-only mirrors for direct-call consumers. Regression:
  `backend/tests/test_518_failed_turn_metrics.py` (RED on master).
- **Shared admission control** (`backend/app/services/admission.py`): per
  -device budgets (chat/instant/embedding/reranking/vision/background) with
  bounded queues, foreground preference that evicts (never cancels) local
  background holders, deadline rejection, cancellation cleanup, TTL sweeps,
  shutdown release, fail-open degradation, and a Redis-backed cross-process
  store behind `ADMISSION_STORE_URL` (process-local by default). Wired at
  the chat route, the engine's generation/embedding/rerank/vision phases,
  the ingestion/enrichment workers and the wiki/draft/kms compile
  processors; lifespan shutdown releases slots. 11 conservative
  `ADMISSION_*`/`TELEMETRY_ENABLED` settings documented in `.env.example`.
- **Telemetry** (`backend/app/services/telemetry.py` + wiring): per-turn
  correlation ids on chat SSE `done` events, W3C `traceparent` +
  `X-Request-ID` on every outbound provider call, measured stage durations,
  queue waits/depth, first-useful-content latency, provider outcomes
  (ok/partial/unavailable/empty), embedding cache hits, and a
  multiprocess-aggregated `GET /metrics` Prometheus exposition.
  `TELEMETRY_ENABLED=false` is fully inert; OTel export stays an optional
  extra. LLMClient now lazily starts if used without explicit `start()`.
- **Durable backup/restore**: `scripts/backup_sqlite.py` copies via the
  sqlite3 backup API (WAL-safe; the old `read_bytes()` lost committed
  transactions still in the `-wal` file); new `scripts/backup_set.py`
  creates consistent sets (encrypted WAL-safe app.db, LanceDB tables
  version-tagged `backup-*` before copying, vault + draft-room trees, sha256
  manifest `backup_manifest.json`); new `scripts/restore.py` verifies every
  digest, restores and checks out the tagged table generations; the
  migrate_memories fallback copy is WAL-safe too. CI restore drill:
  `backend/tests/test_518_restore.py`.
- **Operator docs**: new `docs/operations.md` (worker topology, lease and
  admission backend, outage behavior, operator recovery actions, scheduled
  backups, telemetry surface incl. the measured hardware inventory for
  172.16.50.159: 2x RTX 2000E Ada 16 GB + 1x RTX 1000, TEI 1.9.3,
  harrier-oss-v1-0.6b :8080, bge-reranker-v2-m3 :8081) and an optional
  `docker-compose.observability.yml` overlay.
