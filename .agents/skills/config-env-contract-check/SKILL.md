---
name: config-env-contract-check
description: Audit environment and configuration contracts across backend settings, frontend env usage, Docker, Compose, CI, docs, and examples. Use when changing config, deployment, root paths, CORS, settings schemas, env examples, or Docker files.
effort: medium
---

# Config Env Contract Check

Use this skill when a change touches environment variables, deployment paths, Docker, Compose, settings APIs, or docs that describe configuration.

## Contract Surfaces

Check these files when present:

- `.env.example`
- `docker-compose.yml`
- `Dockerfile`
- `frontend/Dockerfile`
- `backend/app/config.py`
- `backend/app/api/routes/settings.py`
- `frontend/src/lib/api.ts`
- `frontend/src/lib/paths.ts`
- `frontend/vite.config.ts`
- `frontend/vite.paths.ts`
- `frontend/src/stores/useSettingsStore.ts`
- `scripts/check_config_contract.py` ← CI contract enforcer; must be updated atomically with env var defaults
- `Dockerfile` + `frontend/Dockerfile` ← Build-time `VITE_APP_BASENAME` validation is inlined in both; the standalone `scripts/validate_vite_env.mjs` was removed in #754 and no longer exists
- `README.md`
- `INSTALLATION.md`
- `docs/admin-guide.md`
- `.github/workflows/ci.yml`

## Required Checks

- Defaults match between runtime config and deployment examples.
- Docker Compose defaults do not narrow application defaults unexpectedly.
- Frontend build-time env values and backend runtime env values are coordinated.
- Public path prefixes keep `APP_ROOT_PATH`, `VITE_APP_BASENAME`, and `VITE_API_URL` aligned.
- Security-sensitive placeholders are documented and rejected or validated by runtime config.
- Docs examples match the actual env names consumed by code.
- Tests cover parser edge cases for new config formats.

## Atomicity rule for env var default changes

When changing a default value for any env var that appears across the deployment
stack, all of the following surfaces MUST be updated in the same commit:

1. `.env.example` — example/documentation value
2. `docker-compose.yml` — Compose default
3. `Dockerfile` (root) — build-stage default
4. `frontend/Dockerfile` — frontend build-stage default
5. `scripts/check_config_contract.py` — CI contract assertion

Missing any one of these causes `scripts/check_config_contract.py` to fail in
CI with a mismatch error that is hard to diagnose without knowing all five
surfaces must agree. The script itself is part of the contract, not just an
observer of it.

Additionally: the inlined `VITE_APP_BASENAME` validation blocks in the root
`Dockerfile` and `frontend/Dockerfile` must implement identical validation
rules. If you add or relax a check in one, update the other. There is no
standalone `scripts/validate_vite_env.mjs` (removed in #754); both Dockerfiles
are the sole authorities.

## Automation

Run the repo contract check when relevant:

```bash
python scripts/check_config_contract.py
```

If the script fails, treat it as a config contract regression unless the script itself is stale and the intended contract has changed.

## Output

Report findings as:

```text
Contract:
Owner:
Consumers:
Status: CONFIRMED / DISPROVED / UNVERIFIED
Evidence:
Fix:
```
