# Container lifecycle and operator startup paths (#748, #749)

Issues: #748 (Workstream I PR 1 of 7) and #749 (Workstream I PR 2 of 7),
collated per maintainer authorization. Frontier-audit provenance (run
20260923T174456Z); claims re-verified against the pre-change master.

## What changed

### #748 — container stop now runs the graceful shutdown; startup waits for the embedder

- **Dockerfile CMD** now runs `exec uvicorn ...` so uvicorn replaces `sh` as
  PID 1: `docker stop`/`compose down` deliver SIGTERM and the documented
  shutdown path (queue drain, admission release, optional final optimize)
  actually runs instead of being SIGKILLed at the grace-period boundary
  (T1-04-S-01). Previously `sh` was PID 1 and never forwarded the signal.
- **Dockerfile CMD pins `--workers 1`** (T1-04-S2-10): uvicorn reads the
  `WEB_CONCURRENCY` env only when `--workers` is absent, so an operator-set
  `WEB_CONCURRENCY>1` can no longer silently start an unsupported
  multi-worker topology over one data directory. The forwarded env stays
  inert; `FORWARDED_ALLOW_IPS` expansion semantics are unchanged.
- **`knowledgevault` now `depends_on: harrier-embed: service_healthy`**
  (T1-04-S2-07): on a fresh `up` (or after an embedding-model change) the
  strict TEI model validation now probes a READY service instead of
  silently self-skipping via its tolerant except arm. The embedder's
  healthcheck `start_period` (120 s) grace-marks early probe failures but is
  not a wait deadline — an embedder that never reports healthy leaves the
  app waiting (documented in README Troubleshooting; CPU-only hosts use the
  override below).
- `stop_grace_period: 90s` already shipped via issue #700's follow-up; the
  frozen check pins it as a regression guard rather than re-proving it.

### #749 — operator startup paths and the migration tool refuse instead of failing silently

- **Data-directory preflight** (TQ-differential-e-03): startup now checks the
  data directory's write AND search permission BEFORE any database work and,
  when the existing directory lacks either (the root-owned bind mount the
  daemon creates), fails with one actionable message naming the directory,
  the current uid, and the owner uid, and describing the HOST-side fix in
  prose (the bind-mount source compose maps to the reported container path;
  `mkdir -p` + `chown -R 1000:1000`, quoting the host path) — replacing the
  bare sqlite/LanceDB tracebacks this state previously produced. A
  nonexistent directory still passes (fresh starts create it as before).
  Disclosed: this takes the acceptance criterion's explicit
  "fail with a message naming the directory/owner/uid" arm; under
  `restart: unless-stopped` the container restart-loops with that message
  until ownership is fixed. The lifespan remains fail-soft everywhere else.
- **Persisted-settings load is guarded** like the migration step: a settings
  database that cannot be opened no longer aborts startup with a bare
  traceback — it routes into the existing `migrations_ok=False` degraded
  signal so `/api/healthz` reports 503 with the failure logged.
- **`scripts/migrate_embeddings.py` refuses wrong paths** (T1-29-K-02):
  - exit **7** when neither `--lancedb-path` nor `--sqlite-path` exists
    (previously a silent "Nothing to migrate" exit 0 told the operator the
    migration had run);
  - exit **8** when the application database is unusable for a reset/wipe
    (dry runs included): missing, no `files` table, a `files` table without
    the reset contract's core columns (`id`/`status`), an obvious
    backup copy such as `app.db.bak` (rotated and `.copy` names like
    `app.db.bak.1`, and a file directly inside a `backups/` directory,
    included), or vector-claiming rows that share no
    file id with the index's chunks — previously the status reset silently
    skipped (or hit the wrong database) and the LanceDB wipe STILL ran,
    destroying vectors with no usable rollback source (audit probe reproduced
    the loss). The checks are heuristics, not proof of identity: a
    byte-identical copy of the live database under a live-looking name, or
    another deployment's database whose autoincrement file ids overlap the
    index's, is not detectable;
  - `--dry-run` and `--force` are subject to both refusals (wrong paths are
    wrong paths; `--force` still overrides only exits 3/4); lock and
    corruption semantics (exits 5/6) are unchanged, with the
    `OperationalError`-before-`DatabaseError` ordering the sibling probes
    use — writer locks still get stop-the-app advice, while other open
    failures (permission/read-only) now get a distinct not-a-lock diagnostic
    instead of the lock advice.
- **The stale-embeddings health hint is now runnable** (T1-29-K-02): it
  names the actual command — `python scripts/migrate_embeddings.py
  --lancedb-path ./data/lancedb --sqlite-path ./data/app.db` (the real
  default subpaths; adjust for a custom `HOST_DATA_DIR`) — instead of a bare
  script path that is neither in the image nor runnable from the host
  without flags.
- **Docs tell the truth for a native-Linux first run** (TQ-differential-e-03,
  RT-S3-03): README and INSTALLATION now show creating the data directory
  and `chown -R 1000:1000` (default and custom-path forms, the appuser is
  uid 1000); the Quick Start prerequisites name the NVIDIA GPU the bundled
  containers reserve; README's migration commands pass explicit host-side
  paths and its exit-code list documents 7 and 8; INSTALLATION's compose
  excerpt matches the new dependency shape.
- **New `docker-compose.cpu.yml`** (RT-S3-03): a CPU-only host override —
  `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d` —
  swapping `harrier-embed`/`reranker` to the CPU TEI build and removing the
  GPU reservations via `deploy.resources.reservations.devices: !reset []`
  (compose merge retains inherited devices under a plain empty list; the
  override documents the digest-pinning step for production use).
- **Email "today" stats anchor to UTC** (T1-01-S2-05):
  `GET /email/status` now computes the day window with
  `datetime.now(timezone.utc).date()` — the same UTC day `files.created_at`
  is stamped in — instead of the host-local `date.today()`, so a host whose
  local date differs from UTC no longer counts the wrong day.

## Verification

- 14 frozen acceptance checks (anchor receipt published on both issues
  pre-implementation): the 9 discriminating checks flipped RED→GREEN; the
  5 preserving pins stayed green (stop-grace period, TEI strict-check
  contract, compose forwarding/build contracts, migrate no-op contracts,
  health-readiness contract).
- New non-frozen suites: `test_i02b_operator_paths_semantics.py` (per-document
  ownership steps, exit-7/8 shapes incl. force/dry-run and the lock-vs-
  corruption taxonomy, runnable-hint command form, CPU override structure +
  live compose-merge leg) and `test_i02c_degraded_start_signal.py` (real
  lifespan: unwritable-dir named failure, fresh-start pass-through,
  settings-only failure → `migrations_ok=False` + `/api/healthz` 503).
- Host-only legs (docker stop exit 0; rootful-Linux fresh bind; no-GPU CPU
  start) remain documented verification recipes — no Docker daemon runs in
  CI or this sandbox.
