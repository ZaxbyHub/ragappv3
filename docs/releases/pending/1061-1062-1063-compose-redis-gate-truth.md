# Compose Redis hardening, enum-forward crash fix, data-dir doc truth, gate closures (#1061, #1062, #1063)

## What changed

- **Bundled compose Redis now requires authentication and is loopback-bound** (#1061): `--requirepass ${REDIS_PASSWORD:-knowledgevault-local-redis}` with the same interpolated credential in the healthcheck and the compose `REDIS_URL` default; the host port publish is `127.0.0.1:6379:6379` only. `.env.example` documents `REDIS_PASSWORD` (URL-safe alphabet, override semantics, admission-store note) and drops the stale "NO password" text. `INSTALLATION.md`'s compose excerpt is synchronized, and the release runbook's Redis connection test uses `redis-cli -u` with a credential-aware fallback plus a native-vs-compose default qualification and the `allkeys-lru` CSRF-eviction caveat.
- **`DOCUMENT_PARSING_STRATEGY` forwards in the short pass-through form** (#1061): the `${VAR:-}` empty-default form crash-looped the backend container (the enum-strict validator rejects `""`) while every CI gate stayed green. An unset key is now omitted entirely so Settings keeps its `auto` code default; an explicit value still forwards. The g06 parsing-strategy missing-entry FAIL rationale was refreshed (it cited the removed empty-forward). New shipped guard `backend/tests/test_1061_compose_enum_forward_guard.py`: every backend `${VAR:-}` empty-default forward must permit `Settings` construction (any exception fails), plus a short-form pin on `DOCUMENT_PARSING_STRATEGY`.
- **Origins deep-JSON fail-closed** (#1061): `parse_backend_cors_origins` and `parse_allowed_model_origins` wrap `json.loads` with the same `RecursionError → "nested too deeply"` ValidationError their three sibling validators already carry; a pathologically deep env array no longer escapes as an unhandled `RecursionError` at import.
- **Data-directory docs describe the real layout** (#1061, widened per the PR #1081 closeout): `/data/knowledgevault` references across README, docs/release.md, docs/admin-guide.md, docs/email-ingestion.md, and scripts/migrate_embeddings.py now use the correct form for their execution context — host-shell commands `./data/...`, `docker compose exec` commands `/app/data/...` — matching `DATA_DIR=/app/data` under compose and `./data` native. The `Dockerfile` now creates `/app/data` (retired directory removed; the runtime `chown` scopes to `/app`).
- **The `IMAP_ALLOWED_MIME_TYPES` example discloses its effect** (#1061): the 4-type example states it overrides/narrows the shipped 17-type default.
- **g06 `contributing-env-path` fails closed on names-neither** (#1062): an `**Environment:**` paragraph naming neither `.env.example` nor `backend/.env` now FAILs the mode (previously passed silently); additive branch with a mode-level negative test.
- **ci-fix-monitor quality-contracts table lists all eighteen scripts** (#1063) in CI order with one-line purposes; `check_runtime_contract.py`'s docs-only enforcement scope is intentionally unchanged.

## Why

Collated follow-ups from PR #1059's closeout (user-authorized multi-issue collation). The empty enum forward was a deploy-blocking crash with a green CI signal; the Redis posture was an unauthenticated host-published store holding raw CSRF tokens; the doc/gate drift class repeatedly misled operators and automated reviewers (a PR #1081 reviewer was misled by the stale paths).

## Migration steps

- **Deployments whose `.env` pins `REDIS_URL` at the bundled Redis** (the value base `.env.example` recommended for Docker): the bundled Redis now requires the `REDIS_PASSWORD` credential. Either remove the pin (the compose default carries `REDIS_PASSWORD` automatically) or set both `REDIS_PASSWORD=<your password>` and `REDIS_URL=redis://:<your password>@redis:6379/0`. A credential-free pin silently loses the shared CSRF store, rate-limit counters, and embedding cache (each falls back to a local/per-process mode).
- **`ADMISSION_STORE_URL` pointed at the bundled instance** needs the same credentials (see the `REDIS_PASSWORD` note in `.env.example`).
- **Cross-host consumers of the bundled Redis**: the host port publish is loopback-only (`127.0.0.1:6379`). Use an SSH tunnel for remote access, or override the publish — changing it away from loopback is a deliberate, reviewed decision.
- **The release runbook's Redis probe** now authenticates via `REDISCLI_AUTH` (the credential never appears in argv) and exits nonzero on failure.

## Known limitations

- The four remaining #750-owned empty-default forwards (`JWT_ALGORITHM`, `ADMIN_RATE_LIMIT`, `VECTOR_METRIC`, `IMAP_MAILBOX`) pass Settings construction but have consumer-level breakage; they stay with #750 (this PR fixes only `DOCUMENT_PARSING_STRATEGY`, one of its five keys).
- The shipped Redis default password is public knowledge in this repository; deployments on untrusted networks must override `REDIS_PASSWORD` (documented). CI does not boot the compose stack — the authenticated/healthcheck behavior is evidenced by a one-time live probe in the PR, not a standing gate.
- The ci-fix-monitor skill's "four CI jobs" framing (:6, :26-27) and `.claude` adapter copy are the #759/#666 agent-skills-vs-CI truth class, deliberately untouched here (#1063 scope: the quality-contracts row only).
