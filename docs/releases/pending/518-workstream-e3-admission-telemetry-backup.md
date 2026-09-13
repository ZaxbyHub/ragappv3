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

## Migration

- No database or data migration; all changes are additive (new modules,
  settings, scripts, docs).
- New environment keys (`ADMISSION_*`, `TELEMETRY_ENABLED`,
  `TELEMETRY_REGISTRY_DIR`) are documented in `.env.example` and forwarded
  by `docker-compose.yml`. `ADMISSION_DEADLINE_SECONDS` uses the compose
  SHORT form and is commented out in `.env.example` — an empty value is
  coerced to unset by a `mode="before"` validator, so `docker compose up`
  and `cp .env.example .env` both start cleanly (swarm review F-001).
- Backup sets now record vault artifacts under `vaults/<id>` manifest
  paths, matching the live `<data_dir>/vaults/<id>` layout; restore places
  them back there (Copilot review). Sets created by pre-release drafts of
  these scripts are not supported — no released set exists.

## Breaking changes

- Chat and instant generation now have admission caps (8 and 4 concurrent
  by default) where none existed before, ON by default. Deployments that
  ran more concurrent streams will begin queueing/rejecting at those
  numbers; raise `ADMISSION_CHAT_BUDGET`/`ADMISSION_INSTANT_BUDGET` or set
  `ADMISSION_ENABLED=false` (restart required) to restore prior behavior.
- The non-stream chat route can now return HTTP 503 (`chat admission
  rejected`) under saturation; the stream path emits the SSE error code
  `ADMISSION_REJECTED` before `done`.

## Known caveats

- Only `app.db` is encrypted in a backup set; the lancedb/vault/draft-room
  trees are plaintext document copies — store sets on restricted storage
  and set `AES_KEY`/`AES_KEY_V1` before backup AND restore (F-009).
- Embedding/vision/background admission budgets equal their underlying
  per-process semaphores: relieving saturation requires raising BOTH, not
  just the budget (F-010).
- `GET /metrics` is unauthenticated on the same port as the API — restrict
  at ingress (F-012).
- Admission live-holder leases renew every ttl/3 (30 s default), so long
  generations keep their slots; the TTL only reaps dead processes (F-003).
  `ADMISSION_ENABLED` is read at process start — there is no runtime
  toggle (F-006/F-011).
- The OTLP export path is an optional pinned extra; with
  `OTEL_EXPORTER_OTLP_ENDPOINT` set on EVERY replica sharing one
  `TELEMETRY_REGISTRY_DIR`, per-replica pushes over-count — enable export
  on one replica per shared dir.
