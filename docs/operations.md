# Operations Guide (Workstream E3, issue #518)

Operator-facing reference for runtime topology, admission control,
telemetry, outage behavior, recovery and scheduled backups. Hardware facts
below are from a read-only inventory taken 2026-09-11; they are measured
roles, not capacity claims.

## Worker topology

- The backend runs as one uvicorn process per container
  (`Dockerfile` CMD; `WEB_CONCURRENCY` defaults to 1). All background work —
  ingestion workers, enrichment, reindex, wiki/draft/kms compile processors,
  email ingestion, file watcher — runs as asyncio tasks INSIDE that process.
- SQLite (`app.db`, WAL mode, shared by the main pool and MemoryStore) and
  LanceDB (`data/lancedb`) are file-backed. Multiple replicas can read
  concurrently, but writes rely on SQLite's single-writer serialization and
  on each replica's own in-process queues; scale-out write topology is NOT
  enabled by default.
- Redis (already deployed for the slowapi rate limiter and the embedding L2
  cache) is the cross-process coordination point. Set
  `ADMISSION_STORE_URL=redis://...` to share inference admission budgets
  across uvicorn workers/replicas; with the default (empty) URL budgets are
  process-local — the documented single-process boundary.
- Deployed inference hardware (read-only inventory 2026-09-11): host
  `172.16.50.159` has 2x RTX 2000E Ada (16 GB each) + 1x RTX 1000 (8 GB),
  running TEI 1.9.3 serving `microsoft/harrier-oss-v1-0.6b` (fp16) for
  embeddings on :8080 and `BAAI/bge-reranker-v2-m3` (fp16) for reranking on
  :8081. These are the measured roles of each card today; GPU placement and
  model-default changes are tracked separately (#570, L1). No claim is made
  here about how many concurrent users any single card can or cannot host —
  that requires measured footprints (F2 owns benchmark conclusions).

## Lease and admission backend

- `app/services/admission.py` admits inference work against per-device
  budgets (chat, instant, embedding, reranking, vision, background; see
  `ADMISSION_*` in `.env.example`). Defaults are deliberately generous —
  at or above the per-process semaphores they complement — so enabling
  admission does not change current throughput; tighten only after baseline
  observation via `GET /metrics`.
- Budget semantics, honestly stated (swarm review F-010/F-011, #827 review
  F-003): embedding and vision budgets are set EQUAL to the per-process
  semaphores that still bind underneath — raising only those
  `ADMISSION_*_BUDGET` values does NOT raise those ceilings;
  raise both together. BACKGROUND no longer has its own budget key (see the device
  mapping below), and `ingestion_worker_count` bounds only the ingestion
  worker loop — no worker-count setting bounds the enrichment, draft, KMS
  and wiki-compile background work (each runs as a single serial consumer,
  so the shared llm budget is the only cross-category cap on it). Chat and
  instant had NO prior bound: their admission budgets are NEW caps, on by default
  (`ADMISSION_ENABLED=true`); a deployment that previously ran more than 8
  concurrent chat streams (or 4 instant) will start queueing at those
  numbers with zero `.env` changes.
- Device mapping (#687): CHAT and BACKGROUND share ONE LLM-device budget
  key sized `max(ADMISSION_CHAT_BUDGET, ADMISSION_BACKGROUND_BUDGET)`
  (defaults: `max(8, 2) = 8`), because both contend for the same thinking
  LLM. `ADMISSION_BACKGROUND_BUDGET` is therefore NOT an independent cap:
  any value up to the chat budget has no effect on the shared budget, and a
  value above it raises the chat cap with it. Instant, embedding,
  reranking and vision keep their own measured-endpoint keys. Two counting
  caveats: evicted-but-running background holders are uncounted by design,
  so REAL thinking-LLM concurrency can briefly reach chat budget + evicted
  background (at defaults 8 + up to 8 = 16, vs 10 before #687); and chat's
  queue bound is reduced by background holders on the shared key (see the
  overload paragraph).
- Foreground preference (now effective in default deployments, #687): when
  interactive chat work is blocked and only background holders occupy the
  llm budget, one local (same-process) background holder is logically
  evicted (its task keeps running; its later release is a no-op). Work is
  never cancelled and background is never starved — queued background
  items all complete once interactive pressure subsides. Under sustained
  full chat load, background admits QUEUE (with
  `ADMISSION_DEADLINE_SECONDS=None` that queueing is unbounded in time).
- Overload is bounded: the enforced bound is `ADMISSION_QUEUE_MAX_SIZE`
  counted as in-flight holders for the budget key PLUS queued waiters for
  the class; at or over the bound the request is rejected immediately (SSE
  `ADMISSION_REJECTED` error + done on the stream path, HTTP 503 on the
  non-stream path). Because chat and background share one key (#687), full
  chat occupancy tightens the background queue bound from 64 to
  `ADMISSION_QUEUE_MAX_SIZE − chat holders` (with 8 chat holders, the 57th
  concurrent background request is `queue_full`-rejected where the
  pre-#687 dedicated key accepted through #64). A queued request whose
  deadline (`ADMISSION_DEADLINE_SECONDS`) expires is rejected, never
  executed.
- Leases: a live holder renews (heartbeats every ttl/3), so a long
  thinking-mode generation NEVER loses its slot to the lease TTL (a FIXED
  30 s — not operator-configurable); the TTL exists solely to reap holders
  whose process died without releasing, and runs on the next acquire. With
  a shared store, expiry authority is the REDIS server clock (TIME-based
  Lua; Redis >= 6 required in practice — effects replication is the
  default since Redis 5, but the pinned redis-py speaks RESP3/`HELLO`,
  which Redis 5 rejects, so a Redis 5 store ends up `degraded`) — host
  clock skew cannot reap a live holder. ROLLING UPGRADES (#827 review
  F-002): pre-#687 replicas enforce `admission:chat`/`admission:background`
  while post-#687 replicas enforce `admission:llm` — mixed versions admit
  on DISJOINT keys, so the budget is unshared (defaults worst case ~18
  concurrent holders across old and new replicas, versus the 10 the
  pre-split configuration intended: 8 chat + 2 background) until the last
  old replica restarts; the
  TTL window does NOT bound that window, because live holders renew for as
  long as their requests run. Upgrade all replicas, then optionally
  `DEL admission:chat admission:background` (the orphaned old hashes are
  inert). Clock skew is a separate, narrower residual: the unchanged
  instant/embedding/reranking/vision keys are written by BOTH versions, and
  a mixed pair stamping epochs from different clocks can mis-sweep those
  keys under skew — the same upgrade-within-one-TTL-window guidance applies
  to them.
  On graceful shutdown the controller releases
  all in-flight slots, cancels renewal heartbeats, closes a Redis store's
  connection pool and rejects new admits (wired in `app/lifespan.py`
  AFTER background workers drain).
- Optional Redis cache isolation (#687): the embedding/query-transform
  caches' sync Redis calls run on a small dedicated bounded thread pool
  (`app/services/redis_io.py`, 8 workers) with socket timeouts derived
  from `REDIS_IO_TIMEOUT_SECONDS`, so a hung Redis can never consume the
  shared default executor that unrelated request work uses. Migrating
  those caches to redis-py asyncio (no threads at all) is future
  hardening, not part of #687.
- Degradation: if the shared store (Redis) is unreachable, admission fails
  OPEN — requests proceed — and the controller exposes `.degraded`; the
  operator-visible signal is the absence of admission metrics progression
  plus a logged warning. Lease-shaped job ownership (draft job heartbeats,
  ingestion stage states) remains SQLite-based as before; admission leases
  are TTL entries in the admission store only.

## Outage behavior

- Provider outage: circuit breakers (E1, issue #494) trip per provider;
  the chat engine fails over to the next configured client. Streams that
  fail before content emit an SSE error event and a terminal done whose
  `llm_metrics` identifies the failing attempt (request-local metrics,
  OBS-002) — per-response diagnostics never report the previous query.
- Embedding/reranker outage: reranking degrades to distance scores and the
  embedding path surfaces `EMBEDDING_ERROR`; admission rejection of these
  stages degrades the same way (skip, log, distance scores).
- Redis outage: rate limiting and the embedding L2 cache degrade as before
  (E1/E2 behavior); admission fails open with `.degraded` set.
- Telemetry outage: telemetry is best-effort and never on the request
  critical path; `TELEMETRY_ENABLED=false` makes it fully inert.

## Maintenance windows

- `MaintenanceMiddleware` blocks only mutating methods (POST/PUT/PATCH/DELETE).
  GET/HEAD/OPTIONS requests never consult the maintenance flag at all, so
  DB-free routes (`/health`, `/api/health`, `/api/healthz`) stay fast and
  pool-free even while the pool is exhausted (issue #549 C02).
- Exempt POST routes: the admin toggle (`/api/admin/maintenance`) and the
  auth routes (`/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout`) —
  a window longer than the access-token lifetime no longer locks every user
  out (issue #549 C01). Registration stays blocked: a window is a data freeze
  for account creation too.
- The flag read on the request path is served from a short in-process TTL
  cache (5 s) filled off the event loop. A `set_flag` toggle is visible to
  the very next request from the same process; a toggle made in another
  worker becomes visible within the 5 s TTL. The admin toggle route's
  response always reads the flag fresh from SQLite.
- If the flag read itself fails (pool exhausted, corrupted `system_flags`
  row), the middleware logs a WARNING and fails open — the request proceeds
  instead of returning 500. A mutating request whose cache is cold while the
  pool is exhausted stalls up to the pool wait budget (~15 s at defaults) in
  a worker thread, then proceeds.

## Operator recovery actions

- Overloaded chat (queue_full rejections): raise the relevant
  `ADMISSION_*_BUDGET` — for embedding/vision/background also raise the
  matching per-process semaphore (the budget alone cannot exceed it; see
  the budget-semantics note above). `ADMISSION_ENABLED` is read once at
  controller construction: setting it in `.env` requires a RESTART to
  apply (there is no runtime toggle).
- Stuck budgets after a crash: holders expire via TTL automatically
  (`ADMISSION_*` defaults use a 30 s lease TTL; live holders renew, so a
  TTL expiry only happens for a process that died). Restarting the backend
  also releases all local slots on shutdown.
- Restore from backup: stop the stack, run `python scripts/restore.py
  <backup-set-dir> --dest <data-dir>` (verifies every digest and checks out
  the tagged LanceDB generations), then restart. See Scheduled backups.
- Corrupt/missing metrics: telemetry shards are rebuildable caches — delete
  the registry dir contents and restart; no user data is involved.

## Scheduled backups

- One-shot consistent set (safe while the app runs — WAL-safe SQLite
  snapshot via the sqlite3 backup API, LanceDB tables version-tagged
  `backup-*` BEFORE the copy, manifest with sha256 digests binding app.db +
  lancedb + vaults + draft-room):
  `python scripts/backup_set.py --output backups/set-$(date +%Y%m%d-%H%M%S)`
- **Encryption scope, stated plainly (swarm review F-009): only `app.db` is
  encrypted** (AES-GCM, key from the `AES_KEY`/`AES_KEY_V1` environment
  variable via SecretManager — set it before backup AND restore, or the
  sqlite artifact cannot decrypt). The lancedb, vault and draft-room trees
  in a backup set are PLAINTEXT copies of your documents: store sets on
  restricted, operator-controlled storage (the separate host below), not a
  broadly writable share. Set files are written owner-only (0600/0700)
  where the OS supports it.
- Restore + verify drill: `python scripts/restore.py backups/set-...
  --dest ./data-restored` then compare. The CLI verifies every manifest
  digest, rejects paths that escape the set or destination, and checks out
  the tagged LanceDB generations (restores vaults under `<dest>/vaults/`,
  matching live layout). The CI restore drill is
  `backend/tests/test_518_restore.py` (runs in the backend job).
- Schedule under the existing deployment support (no new scheduler
  service): Linux cron `0 2 * * * cd /path/to/ragappv3 && python
  scripts/backup_set.py --output /backup-host/ragappv3/set-$(date
  +\%Y\%m\%d)` ; Windows Task Scheduler runs the same command. Prune with
  `scripts/cleanup_backups.py` (retention). The legacy single-file
  `scripts/backup_sqlite.py` remains for the memories migration path and is
  now WAL-safe too.

## Telemetry surface

- `GET /metrics` (same port as the app, internal-network default): counts
  and durations only — no user content, no secrets. **It is unauthenticated
  and served on the SAME port as the rest of the API** — "no extra port
  published" is a deployment fact, not an access control; restrict scraping
  at your ingress. Metric families: `ragapp_chat_turns_total`,
  `ragapp_queue_depth`, `ragapp_queue_wait_seconds`,
  `ragapp_provider_calls_total`, `ragapp_embedding_cache_hits_total`.
  Multiprocess aggregation: set `TELEMETRY_REGISTRY_DIR` to one shared
  writable directory (all workers/replicas) and the counter/gauge families
  above are summed across shards; empty = single-process counters. Note:
  the chat route records queue depth snapshots only where the engine's
  admission API exposes them; the queue-wait gauge is the primary
  saturation signal. **Stage durations and first-useful-content latencies
  are per-process** (kept in the instance snapshot via
  `Telemetry.snapshot()`); a multi-replica deployment reads stage latency
  from each replica's own telemetry instance, not the shared `/metrics`
  sum.
- Correlation: every chat turn carries a `turn_id` (the inbound
  `X-Request-ID` when present, or the client-generated `turn_id` from the
  `POST /chat/stream` body when the client opts into server-side durable
  turns — issue #553) on the SSE done event, including error terminal
  paths; outbound provider
  calls carry W3C `traceparent` + the same `X-Request-ID`. Log lines carry
  the request id via the RequestIdFilter registered in the logging setup
  (app/lifespan.py).
- Optional OTel export: the OpenTelemetry packages are NOT base
  dependencies — air-gapped installs are unaffected. To enable OTLP export
  of the ragapp_* counters, install the PINNED optional extra
  (`pip install -r requirements.txt -r requirements-otel.txt` inside
  backend/ — api/sdk/otlp-http exporter all ==1.29.0) and set
  `OTEL_EXPORTER_OTLP_ENDPOINT`; `init_telemetry()` then bridges the
  counters to an OTLP MetricExporter (import-guarded: without the packages
  or the endpoint, nothing OTel-related loads and the app is unchanged).
  The GenAI semantic conventions are Development status as of 2026-09
  (open-telemetry/semantic-conventions-genai has no stable release) — the
  pins above are the version boundary; bump them consciously and do not
  describe the conventions as stable. An optional observability stack
  overlay (Alloy collector) is provided in
  `docker-compose.observability.yml`.

## Tracing (OpenTelemetry spans)

- Spans ride the SAME optional extra as the OTLP metric bridge
  (`backend/requirements-otel.txt`). With the extra installed and
  telemetry enabled, spans are REAL: one FastAPI server span per request
  (SERVER kind, `http.*` attributes, no user content) and one `gen_ai`
  client span per LLM / embeddings / rerank provider call
  (`gen_ai.operation.name`, `gen_ai.request.model`); outbound W3C
  `traceparent` values derive from the ACTIVE span context instead of
  the deterministic per-turn synthesis (the synthesis remains the
  fallback when tracing is off). Export: set
  `OTEL_EXPORTER_OTLP_ENDPOINT` and spans ship to it via a batch
  processor; without an endpoint the spans are recorded and measured but
  dropped at the end of each request/call (add an exporter or collector
  to retain them).
- The OTLP bridge additionally exports a
  `gen_ai.client.operation.duration` counter by
  `gen_ai.operation.name`, fed by the per-turn stage recorder
  (planning / searching / reading / generation). The GenAI semantic
  conventions are Development status — pinned in the extra, never
  described as stable.
- Without the extra (the default, air-gapped install) the tracer is a
  no-op shim: nothing imports `opentelemetry`, the span middleware is
  never registered, and the request path is unchanged.
