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
