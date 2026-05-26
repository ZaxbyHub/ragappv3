# Contributing to KnowledgeVault (RAGAPPv3)

Thanks for your interest in contributing! KnowledgeVault is a self-hosted RAG
service for ingesting technical documents and chatting with them using local
LLMs. This guide covers how to set up, make changes, and open a good PR.

> **AI coding agents** (Codex, Claude Code, opencode-swarm) should start with
> [`AGENTS.md`](AGENTS.md) and the `engineering-conventions` / `commit-pr`
> skills. This file is the human-facing version of the same process.

## Project layout

- `backend/` — Python 3.11, FastAPI + SQLite + LanceDB
- `frontend/` — React + TypeScript + Vite (Vitest, shadcn/ui + Tailwind)
- `scripts/` — repo contract checks and maintenance scripts
- `docs/` — user and engineering docs

Engineering conventions and the testing policy are documented in
[`docs/engineering/conventions.md`](docs/engineering/conventions.md) and
[`docs/engineering/testing.md`](docs/engineering/testing.md). Please read them
before non-trivial changes — match the existing pattern in the file you're
editing.

## Prerequisites

- **Python 3.11** (CI pins 3.11 — see the caveat below)
- **Node.js ≥ 20.19.0** (`frontend/package.json` `engines`)
- Local LLM/embedding services (Ollama + embedding container) or Docker — see
  [`README.md`](README.md) and [`INSTALLATION.md`](INSTALLATION.md)

## Local setup

Full setup (including LLM/embedding services and Docker) is documented in
[`INSTALLATION.md`](INSTALLATION.md) and [`README.md`](README.md). The essentials:

**Backend** (from `backend/`):

```bash
python -m venv venv
# Windows: venv\Scripts\activate   |   macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, pytest-cov, pytest-asyncio, requests
python -m uvicorn app.main:app --host 0.0.0.0 --port 9090
```

**Frontend** (from `frontend/`):

```bash
npm ci          # or: npm install
npm run dev
```

**Environment:** copy `.env.example` to `.env` and fill in the required
secrets. At minimum set `JWT_SECRET_KEY` and `ADMIN_SECRET_TOKEN` — generate
each with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

On Windows, `start-services.ps1` / `stop-services.ps1` orchestrate the backend,
frontend, and embedding/LLM services together.

## Branch, commit, and PR conventions

- **Base branch:** `master`. Branch from the latest `origin/master`.
- **Branch names:** short, prefixed slugs (e.g. `fix/tag-cascade`, `feat/bulk-export`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) —
  `type(scope): lowercase description`, where `type` is one of
  `feat`, `fix`, `test`, `docs`, `refactor`, `chore`.
- **Scope your commits:** stage explicit paths; don't commit IDE files, local
  settings, `.env`, or generated artifacts.
- **PRs target `master`** and should include:
  - `## Summary` — 1–3 bullets on what changed and why.
  - `## Test plan` — a checklist of the commands you ran and their results.
- Never `git push --force` to a shared branch; use `--force-with-lease` if you
  must rewrite your own PR branch.

## Before you push — run the CI gates locally

CI (`.github/workflows/ci.yml`) has three required jobs. Reproduce them locally
so your PR goes green on the first try:

**Backend** (from `backend/`):

```bash
ruff check .
# CI-targeted subset:
pytest --tb=short -q tests/test_path_prefix.py tests/test_auth_routes.py tests/test_main_catchall.py
# ...and the tests for the area you changed, e.g.:
pytest -q tests/test_tags_routes.py
```

**Frontend** (from `frontend/`):

```bash
npm run typecheck
npm run lint        # zero-warning gate: eslint src --max-warnings 0
npm test            # vitest run
npm run build
```

**Quality contracts** (from repo root):

```bash
python scripts/check_config_contract.py
python scripts/check_pr_scope_drift.py
```

## Testing expectations

New behavior ships with tests, and tests assert real behavior (status **and**
body **and** state for the backend; callback args / DOM for the frontend), not
just that something rendered. Cover the negative paths. See
[`docs/engineering/testing.md`](docs/engineering/testing.md) for the test
harness patterns and exemplars.

> **Python version caveat:** CI pins **Python 3.11**. On a newer local
> interpreter (e.g. 3.14) some tests fail with
> `RuntimeError: There is no current event loop` — this is a local-interpreter
> artifact, **not a regression**. Prefer a 3.11 virtualenv. Details in
> `docs/engineering/testing.md`.

## Reporting issues

Open a GitHub issue with: what you expected, what happened, steps to reproduce,
and your environment (OS, Python/Node versions). For security-sensitive
reports, please avoid filing public details until maintainers can respond.

## License

This repository does **not** currently include a `LICENSE` file. Until one is
added, contribution terms are at the maintainers' discretion — please open an
issue if you need clarity before contributing substantial work.
