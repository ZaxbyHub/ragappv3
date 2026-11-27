# Release fragment — lifecycle drain, shutdown budget and bounded exit (#854, #799)

Issues: #854 (stop() applies its timeout per drain phase), #799 (Workstream F
PR 6 of 6 — lifecycle drain and readiness; its audit provenance absorbs #854).

## What changed

**One aggregate shutdown budget everywhere (#854).**
`BackgroundProcessor.stop(timeout)` now computes a single deadline and grants
every awaited stage only the REMAINING budget — the pre-drain task gathers,
the four drain phases (queue, enrichment, atoms, lease settle), the
worker/sweep cancellations, the four lease-release/retry-reset maintenance
calls, and the trailing vector-store optimize (kept on the MAIN loop under
`wait_for`; a timed-out LanceDB optimize continues only on LanceDB's native
async runtime, which is never joined by CPython's interpreter-exit). The
composed worst case is the deadline itself, not a per-phase multiple of it.
The lifespan wraps its whole shutdown sequence (email, residency tasks, file
watcher, the four processor stops, admission, store/pool closes, the
upload-commit and turn-finalizer gates) in a 60 s aggregate budget
(`_SHUTDOWN_BUDGET_SECONDS`); only `BackgroundProcessor.stop` takes a timeout
argument — every no-argument stage runs through a bounded `_stop_stage`
helper, and the two synchronous closes run via `to_thread` under the same
budget.

**A bounded tail exit.** After every recovery-critical workflow completes
(deny-by-default `critical_shutdown_work` registry EMPTY, turn finalizers
drained, DB pool closed, upload-commit registry empty — read at gate time)
and logging is flushed, the lifespan's final step exits immediately instead
of joining whatever non-critical worker threads are alive (abandoned parses
past their enforced 300 s await, a curator call past its between-calls
shutdown flag, file-watcher scans). `RAGAPP_BOUNDED_EXIT=1` is set ONLY by
the Dockerfile runtime image and the native launchers; the test suite pops
it (conftest) so embedded lifespans never take this path. If critical work
is incomplete at the tail (a live-turn finalizer timeout keeps the pool open
per #1202's durability contract; upload compensation still in flight per
PRR-020's startup-recovery contract) the gate never fires and those
deliberate recovery contracts govern — disclosed as the data-safety
preference over a hard exit.

**Seven durability protocols registered.** user-delete (settlement,
rollback/restore AND both committed-finalization paths), document-upload
artifact cleanup and post-registration compensation, vault rollback/restore,
draft-promotion shielded compensation, memory-transaction commit/rollback,
turn finalization, and upload commits run inside the registry while active.

**Container/native envelope (#799 AC5/#854).** Dockerfile CMD gains
`--timeout-graceful-shutdown 20` (integer seconds — uvicorn rejects unit
suffixes) and `RAGAPP_BOUNDED_EXIT=1`; docker-compose moves to
`stop_grace_period: 130s` with the derivation comment (20 request drain +
60 lifespan aggregate + 30 SQLite-family join tail + 5 teardown = 115 s
natural worst < 125 s native stop wait < 130 s grace; the State-B
critical-incomplete path is named as the deliberate exception).

**Windows graceful stop (#799 AC4).** start-services.ps1 launches the
backend through `scripts/win_grouped_backend.py` (CREATE_NEW_PROCESS_GROUP
so uvicorn is a group leader; pid + approximate FILETIME start marker in
`.cache/backend.pid`). stop-services.ps1 (and start-services' own
Stop-ProcessOnPort) verify the marker (pid AND start time — a reused PID
fails the gate to the ungrouped fallback), then
`scripts/win_send_ctrl_break.py` — a sacrificial sender that detaches its
own console, attaches to the backend's, and delivers a TARGETED CTRL_BREAK
to the nonzero group — waits up to 125 s, and only then force-stops.
CPython maps CTRL_BREAK to SIGBREAK; uvicorn handles SIGBREAK. Host-verified
end to end: graceful exit in 0.4 s with the full lifespan shutdown sequence
(including the bounded tail exit) in the backend log; the sender and the
non-group wrapper parent survived.

**Lifecycle resilience fixes (#799).** The editorial client is constructed
inside `_prime_editorial`'s try (a misconfigured pair is contained with a
warning instead of escaping the task); the shutdown loop over residency
tasks absorbs any Exception (a task that died with a natural exception no
longer skips the processor stops, admission release, and store/pool
closes); `_create_llm_clients` degrades a URLBlocked factory (live-DNS SSRF
guard) on BOTH legs instead of failing startup; the same guard covers both
settings.py late-activation sites (an SSRF-rejected endpoint skips
activation instead of 500-ing the save); a draft_pipeline compile factory
call moved inside its owning try with a None-guarded finally.

**Readiness truth (#799).** A FAILED upload migration (the "continuing
anyway" path) now records `app.state.upload_migration_ok = False` and
`/api/healthz` reports 503 with "upload migration failed at startup";
absent attribute stays inert.

**Operator guidance and warning truth.** The FTS-missing error no longer
points at the nonexistent `VectorStore._ensure_fts_index()` — it names the
real remediation (admin-triggered full reindex recreates the FTS index, or
disable hybrid search). main.py's admin-token warning is rescoped to the
one reachable corner (USERS_ENABLED=True with the published bootstrap
credential, which config deliberately permits as the first-admin
bootstrap); the unreachable JWT-default warning is deleted (config refuses
to start in every state it described); the CORS wildcard warning states the
real exposure (Starlette reflects the Origin with allow_credentials)
instead of the false browser-rejection claim.

**Executor lifecycle ownership.** The curator offload executor gains a
recreate-on-demand accessor + a cooperative between-propose-calls shutdown
flag (set at the lifespan wiki stage, reset at startup); the parse executor
gains the same accessor; both shut down (wait=False, cancel_futures=True)
at shutdown so queued work never extends interpreter exit — mirroring the
existing auth/wiki-lint/redis conventions and keeping same-process lifespan
restarts working.

## Tests

- backend/tests/test_p03_lifespan_resilience.py (C1/C2/C3/C7/C8 checks)
- backend/tests/test_p03_upload_migration_readiness.py (C6 ast + behavioral + success/inert legs)
- backend/tests/test_p03_db_transaction_pin.py (C9 removal pin)
- backend/tests/test_lifecycle_drain_budget.py (C12 composed/drains/optimize/maintenance/fast)
- backend/tests/test_p03_lifecycle_behavioral.py (C13/C14/C15/C16 behavioral)
- backend/tests/test_p03_shutdown_ownership.py (registry, tail-gate matrices, executor recreation, env pop pin)
- scripts/check_p03_graceful_shutdown.py (C5 structural; wired into the quality-contracts family via this PR's CI list only if maintainers adopt it — runnable standalone)
