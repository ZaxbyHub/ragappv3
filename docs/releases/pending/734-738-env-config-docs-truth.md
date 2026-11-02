# Env-config boot truth: Settings() constructs from every shipped/documented env value, and the setup template/docs match the code (Issues #734, #738)

## What changed

### The backend no longer fails to import on shipped list/set env values (`backend/app/config.py`)

- `allowed_hosts`, `allowed_extensions` and `imap_allowed_mime_types` are now
  `Annotated[..., NoDecode]` fields. Previously pydantic-settings JSON-decoded
  the raw env/.env string for these complex-typed fields BEFORE any
  `mode="before"` validator ran, so every documented non-JSON form raised
  `SettingsError` at `import app.config` (module-level `settings = Settings()`):
  the empty `ALLOWED_HOSTS=` line the template ships
  (`.env.example`, with a comma `ALLOWED_EXTENSIONS=` list alongside it), the
  docker-compose `${ALLOWED_HOSTS:-}`/`${ALLOWED_EXTENSIONS:-}` empty-string
  forwards (docker-compose.yml), and the comma-separated
  `IMAP_ALLOWED_MIME_TYPES` documented in `docs/email-ingestion.md`.
- `allowed_extensions` and `imap_allowed_mime_types` gained
  `parse_allowed_extensions` / `parse_imap_allowed_mime_types` before-validators
  (same convention as `parse_allowed_hosts` / the CORS origins family): empty →
  the field's code default, `[...]` → JSON list, otherwise comma-split.
- Semantics pinned by tests (`backend/tests/test_g01_settings_env_decoding.py`,
  `TestEnvDecoding734` in `backend/tests/test_config_alignment.py`):
  - a raw empty value selects the CODE DEFAULT (mapping it to an empty set
    would reject every upload on default compose deployments, which forward
    `""` when the key is unset);
  - the deliberate empty set/list remains the JSON form `[]` — distinct from a
    raw empty value;
  - supported env forms are: empty, comma-separated, and JSON list (`[...]`).
    A `[`-prefixed value is always parsed as JSON and raises on malformed
    JSON, non-string elements, or pathological nesting (fail closed); any
    other non-empty string is treated as a comma list (a quoted JSON string
    such as `"a.example.com"` becomes the literal value including quotes) —
    the same convention the existing `backend_cors_origins`/origins fields
    already use;
  - JSON-list env forms keep working for all three fields; init-kwarg behavior
    is unchanged (`TestAllowedHostsJSONParsing` stays green).

### `.env.example` stops shipping values that break or contradict the deployment

- `REDIS_URL` is now commented out. The previously active
  `redis://localhost:6379/0` overrode docker-compose's service-hostname default
  (`redis://redis:6379/0`) in every copied template, so the bundled Redis
  container sat unused while CSRF token storage and the embedding cache
  silently fell back. Compose deployments with a fresh template copy now use
  the bundled Redis; native runs keep the code default (localhost) by leaving
  the key unset or pointing it explicitly.
- `DATA_DIR` is now commented out. Compose pins `DATA_DIR=/app/data` for the
  container regardless of the template value, so the active
  `/data/knowledgevault` line was never used under compose and contradicted the
  README. Native runs with a fresh template copy change from
  `/data/knowledgevault` to the code default `./data` — operators who want the
  old path should set `DATA_DIR=/data/knowledgevault` explicitly.
- `DOCUMENT_PARSING_STRATEGY` changed `fast` → `auto` (the code default; the
  comment now says so). Copied-template deployments that did not override the
  key switch from fast to auto parsing — the template no longer diverges from
  the shipped default silently.
- `ALLOWED_EXTENSIONS` now includes `.pptx`, matching the code default set.
  Before #734's fix the template's env value never took effect (it crashed at
  import); now that it parses, shipping the full default set prevents the
  template from silently narrowing uploads.

### Setup docs stop aborting and misdirecting (`INSTALLATION.md`, `CONTRIBUTING.md`)

- `INSTALLATION.md`'s `set -e` quick-reference block no longer runs
  `cp backend/.env.example backend/.env` (the source path was never a tracked
  file); it copies the tracked root `.env.example` to `backend/.env`.
- `CONTRIBUTING.md`'s environment step now names the target `backend/.env`,
  explains that the backend reads `.env` relative to its working directory, and
  runs BEFORE the backend launch commands — the previous directory-less
  instruction came after the `uvicorn` line and wrote secrets where the backend
  never read them.

### New CI gate: `scripts/check_g06_setup_docs.py` (Issues #738 guard)

- Six modes (`redis-url`, `cp-sources`, `allowed-extensions`, `data-dir`,
  `parsing-strategy`, `contributing-env-path`) fail CI when the template/docs
  drift from the code and compose again — the guard that should have caught
  #738's four findings. Wired into the `quality-contracts` CI job, the
  `justfile` recipe, both engineering-doc inventories, `AGENTS.md`,
  `CONTRIBUTING.md`, and the `ci-compatibility-audit` skill mirror; pinned by
  `backend/tests/test_g06_gate_wiring.py` (including the digit-bearing-name
  inventory-regex check).

## Who changes behavior (operator disclosure)

- Existing deployments that already copied `.env` are NOT touched — the
  template edit only affects new/refreshed copies.
- Compose + fresh template copy: Redis switches localhost → bundled service
  hostname (the fix); effective container `DATA_DIR` stays `/app/data`
  (unchanged); parsing `fast` → `auto` if the key was not overridden.
- Native + fresh template copy: `DATA_DIR` `/data/knowledgevault` → `./data`;
  parsing `fast` → `auto`; Redis unchanged (code default localhost).
- Any deployment that sets `ALLOWED_HOSTS` / `ALLOWED_EXTENSIONS` /
  `IMAP_ALLOWED_MIME_TYPES` as empty or comma-separated env values: these now
  construct (previously the backend failed to import); raw empty
  `ALLOWED_EXTENSIONS`/`IMAP_ALLOWED_MIME_TYPES` select the code defaults.
- Explicit JSON-list configurations keep working unchanged.

## Verification

- `backend/tests/test_g01_settings_env_decoding.py` (6 subprocess-isolated
  env/dotenv legs incl. `.env.example` copied verbatim) and
  `TestEnvDecoding734` in `backend/tests/test_config_alignment.py`.
- `python scripts/check_g06_setup_docs.py --check all` (CI `quality-contracts`
  job + justfile).
- `docker compose config` render with the fixed template copied to `.env`
  yields `REDIS_URL=redis://redis:6379/0` (service hostname).
