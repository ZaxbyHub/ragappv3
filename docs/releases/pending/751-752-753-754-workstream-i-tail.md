# Workstream I tail: shipped-lock CI truth, README dev-loop truth, loud operator scripts, LanceDB list_tables migration

Issues: #751, #752, #753, #754 (Workstream I PRs 4-7 of 7; collated by explicit
maintainer authorization — one PR, reviewable as the single "the build/CI/docs/scripts
surface tells the truth" theme).

## What changed

- **CI verifies the shipped lock (#751).** The docker-scope paths-filter now covers
  `backend/requirements.txt` and `backend/requirements-lock.txt` — a lock-only change
  is a docker-surface change because the image pip-installs the lock with
  `--require-hashes`. The nightly full-parser tier installs the SHIPPED lock
  (`--require-hashes -r requirements-lock.txt`) with dev/OTel tooling layered on top
  constrained by a `pip freeze` of the installed shipped env (plus `pip check`), so a
  green nightly proves the shipped set rather than an unlocked resolve. The
  verify-lockfiles step runs the documented `scripts/update-lock.sh` itself on ubuntu
  (executing its POSIX prologue on every PR) followed by an in-place fixed-point diff.
  The schemathesis tier is labelled as the latest-within-ranges canary.
- **Shell scripts are LF (#751).** `.gitattributes` pins `*.sh text eol=lf`;
  `scripts/update-lock.sh` was committed with CRLF, which aborts every POSIX bash at
  `set -euo pipefail` (`set: pipefail: invalid option name`) — renormalized to LF.
- **README Backend Development teaches the real loops (#752).** The stale block
  (deleted `docker-compose.override.yml`, never-existing `backend` service,
  in-container pytest the image cannot run) is replaced by the two real loops:
  container-only (`docker compose up -d` + `logs knowledgevault`) and native-backend
  (dependency services only — the app container would collide on host port 9090 —
  plus native uvicorn and native pytest). The stale node-version comment and unused
  `/data/knowledgevault` mkdir were already fixed by PRs #1049/#1102 and are pinned
  green by this PR's checks.
- **Operator scripts fail loudly (#753).** `start-services.ps1` probes ALL containers
  by exact anchored name and carries state: a stopped `harrier-embed` (host restart /
  `docker compose stop`, same container name in compose) is `docker start`ed instead
  of colliding on `docker run --name`; both docker invocations' exit codes are
  surfaced with the real cause (previously piped to `Out-Null` under script-wide
  `SilentlyContinue`), and the "may still be loading models" note is
  success-conditional. `scripts/update-lock.ps1` checks `$LASTEXITCODE` after every
  native invocation (PS 5.1's `$ErrorActionPreference` does not govern native exit
  codes), so failures stop the script instead of printing "Done." and exiting 0.
- **LanceDB `list_tables()` migration (#754).** All 17 production
  `AsyncConnection.table_names()` call sites (16 in vector_store.py + documents.py)
  migrate to a shared paginating `_list_table_names()` helper (stops on any falsy
  `page_token`, fails loudly on a repeated token). Operator scripts migrate too
  (sync twin for backup_set/cleanup_backups/migrate_embeddings; async for
  reconcile_lancedb_sqlite), test fakes/mocks expose response-shaped `list_tables`,
  and the two admin-guide examples migrate. `table_names()` is deprecated in the
  pinned lancedb (0.41.0, bounded `<1.0`); the risk being retired is the method's
  removal in a future 0.x release taking out every table-existence check at once.
  pyproject filterwarnings gains `error::DeprecationWarning:lancedb` —
  config-level only: lancedb raises with stacklevel=2 so warnings attribute to the
  caller module; the behavioral guards are the AST ban (C17) and the runtime
  warning capture (C18), which are attribution-proof.
- **Unwired operator surface removed (#754).** `scripts/validate_vite_env.mjs`
  deleted (no caller; both Dockerfiles' inlined copies are the sole authority —
  comments updated). `migrate_memories.py --retention` removed (registered, passed,
  never read — "not yet implemented" since introduction).
- **Python 3.12 floor decision recorded, not performed (#754 AC4).** ABSORB into
  #1109 Class B: comment
  https://github.com/ZaxbyHub/ragappv3/issues/1109#issuecomment-6961113809
  enumerates the 20 lock packages the 3.11 floor blocks (derived by re-resolving
  under `--python-version 3.12` and diffing), plus the numpy `<2.5` cap linkage.
  `ALLOWED_RUNTIME["python"]` stays "3.11"; every pin unchanged.

## Known boundaries

- AC13 (#752 loop half) stays HOST_ONLY, narrowly scoped: this host's engine cannot
  run the compose harrier-embed GPU service; assigned evidence = one-off local
  `docker build .` (executed this PR) + CI docker-smoke whenever the Dockerfile
  changes. AC7's compose invocations are validated by C26 (`config --quiet`).
- The `error::DeprecationWarning:lancedb` filter is configuration-level only
  (stacklevel=2 caller attribution) — disclosed above; C17/C18 are the real guards.
- Direct `table_names()` calls inside checkpoint-frozen test files
  (test_vector_store_async.py, test_fts_index_detection_integration.py) are pinned
  by the frozen-check byte-lock this PR cannot amend post-approval; dispositioned
  in the recurrence census with the next-file-touch re-evaluation trigger.
