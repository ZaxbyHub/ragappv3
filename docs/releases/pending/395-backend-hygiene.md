# Backend hygiene + doc-drift cluster (Issues #395, #409)

## What changed

Resolves the v8-review (#211/#257) backend-hygiene + doc-drift residual cluster.
All 8 acceptance criteria of #395 are addressed.

### `thinking_max_tokens` now configurable (DD-rag-005)
- New `thinking_max_tokens` setting (default `32768`, preserving the prior
  hardcoded literal) governs the thinking (high-quality) chat-mode max output
  token budget.
- Wired through `config.py` (field + positive-int validator), the
  `/api/settings` GET/PUT surface (SettingsUpdate model, PUT validator,
  ALLOWED_FIELDS persist list, SettingsResponse model, GET dict), the
  persisted-runtime-reload allowlist in `lifespan.py`, `rag_engine.py` (the
  thinking branch reads `settings.thinking_max_tokens`), and `.env.example`.
- Mirrors the existing `instant_max_tokens` knob exactly.

### Username 3-char floor documented (LOW-4)
- Added rationale comments at both validation sites (`auth.py` registration,
  `users.py` admin path). The 3-char minimum is kept; the manual HTTP 400
  response is intentionally preserved (vs a generic Pydantic 422). Raising the
  floor is out of scope (breaking change for existing accounts).

### Log filename drift reconciled (LOW-10)
- The application logs structured JSON to **stdout** only — there is no log
  file on disk. Docker captures stdout via the default `json-file` driver.
- Reconciled 16 fictional `app.log` / `knowledgevault.log` references across
  README, admin-guide, email-ingestion, and release docs to
  `docker compose logs knowledgevault` / stdout.

### Mock-only vector-store test documented (F-slop-009)
- Documented why `test_rag_engine_hybrid_status.py` mocks the vector_store:
  the logic under test is interface orchestration (hybrid_status computation,
  FTS-exception propagation), not LanceDB storage behavior, and `lancedb` is
  globally stubbed in `backend/conftest.py` plus per-file inline stubs.

### CHANGELOG count-drift note (F-slop-012/013/014)
- The historical FR-4 "3934 backend tests" count (issue #209) is a point-in-time
  snapshot. The authoritative current total is `pytest --co -q tests/` run from
  `backend/` (currently 5379).

### Already-satisfied ACs (verified, no edit)
- Memory-backfill `create_task` retains a strong reference and is cancelled in
  shutdown (LOW-11).
- Main DB pool `max_size` is configurable via `db_pool_max_size` (LOW-12).
- Orphan `=0.9.0` / `=6.0.0` files are gone (`.gitignore` guards `=[0-9]*`);
  `aioimaplib` / `bleach` pins are upper-bounded (LOW-9).

## Why
Closes the LOW/INFO-tier backend-hygiene + documentation-drift cluster carried
over from the codebase review v8. The only functional change is making the
thinking-mode token budget configurable (default = prior behavior); the rest is
documentation accuracy, rationale capture, and a verified-already-satisfied
checklist.

## Migration steps
- None required. `thinking_max_tokens` defaults to `32768` (the prior hardcoded
  value), so behavior is unchanged unless an operator explicitly sets
  `THINKING_MAX_TOKENS` (env) or updates it via `PUT /api/settings`.
- Operators who relied on the documented log-file path (`app.log` /
  `knowledgevault.log`) should switch to `docker compose logs knowledgevault`
  (or their Docker logging driver). No application config change is needed.

## Breaking changes
- None. All changes are additive (new optional setting) or documentation-only.

## Known caveats
- The frontend Settings UI exposes `instant_max_tokens` but does not yet render a
  control for `thinking_max_tokens`; operators set it via env or the settings API.
  This is informational, not required by the AC.
- Bandit baseline (`backend/security/bandit-baseline.json`) was regenerated: the
  comment additions to `auth.py` / `users.py` shifted pre-existing baselined
  findings to new lines (138 → 138 findings, pure line drift, no new debt).
