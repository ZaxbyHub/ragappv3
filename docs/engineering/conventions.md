# RAGAPPv3 Engineering Conventions

Authoritative engineering conventions for this repository. This is the source
of truth referenced by the `engineering-conventions` skill in every agent tree
(`.claude/`, `.agents/`, `.opencode/`) and by `AGENTS.md`.

Conventions here are **descriptive of what the codebase actually does** — match
the existing pattern in the file you are editing over anything written here, and
update this doc when a convention genuinely changes.

---

## 1. Stack & top-level layout

- **Backend** — Python 3.11, FastAPI + SQLite + LanceDB, under `backend/`.
- **Frontend** — React + TypeScript + Vite, Vitest, shadcn/ui + Tailwind, under `frontend/`.
- **Contract scripts** — `scripts/check_config_contract.py`, `scripts/check_pr_scope_drift.py` (run in CI).
- **CI** — `.github/workflows/ci.yml` (jobs: Backend, Frontend, Quality contracts). See `docs/engineering/testing.md` and the `ci-compatibility-audit` skill.
- **User-facing docs** — `docs/` (admin-guide, email-ingestion, release, etc.). Engineering docs live under `docs/engineering/`.

---

## 2. Backend conventions (`backend/app/`)

### Directory layout
| Path | Holds |
|---|---|
| `app/main.py` | App init, router registration (`include_router(..., prefix="/api")`), middleware, exception handlers |
| `app/api/routes/` | One module per domain; each exports `router = APIRouter()` |
| `app/api/deps.py` | Dependency-injection functions: `get_db`, `get_vector_store`, `get_current_active_user`, `get_evaluate_policy`, `require_*`, `UserRole` |
| `app/services/` | Business-logic classes (TagStore, WikiStore, VectorStore, EmbeddingService, …) |
| `app/models/database.py` | `SCHEMA` constant, `SQLiteConnectionPool`, `init_db`, `run_migrations`, `migrate_*` functions |
| `app/config.py` | Pydantic `Settings` (env-var backed); singleton `settings` |
| `app/migrations/` | Standalone migration scripts invoked from lifespan |
| `app/middleware/`, `app/utils/`, `app/security.py`, `app/lifespan.py` | Cross-cutting concerns |

### Routes
- Define `router = APIRouter()` per module; register in `app/main.py` with `app.include_router(router, prefix="/api")`.
- Inject everything via FastAPI `Depends(...)`: connection (`get_db`), services (`get_vector_store`), current user (`get_current_active_user`), policy (`get_evaluate_policy`).
- Request/response shapes are **Pydantic `BaseModel`** classes. Use `ConfigDict(from_attributes=True)` when serializing DB rows; use `@field_validator(..., mode="before")` for normalization.
- Errors: `raise HTTPException(status_code=..., detail="lowercase message")`. No error codes in the body.
- **Route ordering matters**: register specific/static paths (e.g. `GET /documents/stats`) before dynamic `GET /documents/{file_id}` so the int path param doesn't shadow them.
- Rate limiting via `slowapi` (`@limiter.limit("30/minute")`); CSRF via `csrf_protect` dependency on mutations.

### Database
- All connections come from `SQLiteConnectionPool` (`get_pool(path)`, cached per path). WAL mode, `busy_timeout=30000`, and **`PRAGMA foreign_keys = ON`** are set on every connection — rely on FK `ON DELETE CASCADE` rather than manual cleanup.
- Schema lives in the `SCHEMA` constant in `app/models/database.py`. Tables/indexes use `CREATE TABLE/INDEX IF NOT EXISTS`. FTS5 virtual tables (`*_fts`) have auto-sync triggers.
- **Migrations** are idempotent functions named `migrate_add_*(sqlite_path)`: open conn → check `PRAGMA table_info`/existence → `ALTER TABLE` if needed → commit. Register every new migration in `run_migrations`.
- Multi-statement atomic writes use an explicit transaction (`BEGIN IMMEDIATE` … `commit()`/`rollback()`). Before starting one, clear any dangling implicit transaction (`if conn.in_transaction: conn.rollback()`) — a prior best-effort call may have left one open.

### Services
- Service classes take a `db: sqlite3.Connection` (SQLite-backed, e.g. `TagStore(conn)`) or a `db_path: Path` (LanceDB-backed, e.g. `VectorStore`).
- Return rows as `@dataclass` records (e.g. `MemoryRecord`); convert to dicts at the API boundary with `dataclasses.asdict`.
- Scope every query that joins user data by vault. Defense-in-depth: join to `files` and require `tag.vault_id = file.vault_id` rather than trusting upstream checks alone.

### Async
- FastAPI handlers are `async`; SQLite is sync. Wrap blocking DB work in `await asyncio.to_thread(...)`. LanceDB/embedding services are natively async.

### RBAC / vault scoping
- `UserRole`: VIEWER(1) < MEMBER(2) < ADMIN(3) < SUPERADMIN(4). Vault permission levels: read(1) < write(2) < admin(3).
- Authorize through `evaluate = Depends(get_evaluate_policy)` then `await evaluate(user, "vault", vault_id, "read"|"write"|"admin")`. Resolution order: superadmin → app-admin baseline → explicit `vault_members` → `vault_group_access` → `visibility` (public/org).
- Use `require_vault_permission(...)` / `require_admin_role` / the "admin somewhere" pattern (`require_document_admin`) for endpoint gates. Dependencies resolve before body validation, so an unauthorized caller gets 403 even on a malformed body.

### Config, logging, naming
- Config via `app/config.py` `Settings` (UPPER_SNAKE env vars → snake_case attrs); `SecretStr` for secrets; insecure defaults rejected at startup. Keep `.env.example` in sync (the `config-env-contract-check` skill audits this).
- Per-module `logger = logging.getLogger(__name__)`; structured request logging with field scrubbing in middleware.
- snake_case for modules/functions/tables/routes, PascalCase for classes, `_private` prefix for internals, `*Error` for exceptions.

---

## 3. Frontend conventions (`frontend/src/`)

### Directory layout
| Path | Holds |
|---|---|
| `pages/` | Route-level page components, lazy-loaded in `App.tsx` |
| `components/<feature>/` | Feature components + their local hooks (e.g. `components/documents/useDocumentPolling.ts`) |
| `components/ui/` | shadcn/ui primitives |
| `components/shared/` | Reusable cross-feature components (StatusBadge, EmptyState) |
| `lib/` | `api.ts` (HTTP client), `utils.ts` (`cn()`), `formatters.ts`, `fileIcon.tsx`, `paths.ts` |
| `stores/` | Zustand stores (`useAuthStore`, `useVaultStore`, `useUploadStore`, …) |
| `hooks/` | Cross-feature custom hooks |

### API client (`lib/api.ts`)
- Single axios client, `baseURL = VITE_API_URL`. Bearer token attached via request interceptor (`setJwtAccessToken` from `useAuthStore`); CSRF token auto-attached to POST/PUT/PATCH/DELETE with 403 retry; 401 → silent refresh with backoff.
- Prefer **options-object signatures** for functions with optional params (e.g. `listDocuments(options: ListDocumentsOptions)`), not long positional lists.
- Export shared types/interfaces (`Document`, `Tag`, …) from `api.ts`. **IDs from the API are typed as declared there** — `Document.id` is a `string`; tag/vault ids are `number`. Match these exactly.

### Components, state, routing
- Pages compose **local hooks + feature components** (the DocumentsPage pattern: `useDocumentPolling`/`useBulkSelection` + `<DocumentTable/>`/`<TagFilter/>`). Keep pages as slim orchestrators.
- State via **Zustand** (`create<T>((set, get) => ({...}))`); `persist` only for durable state (auth token), never fast-changing streams.
- Routes are lazy (`lazy(() => import(...))`) under `<Suspense>`, wrapped in `ProtectedRoute` + `MainAppShell` in `App.tsx`.
- Styling: Tailwind classes + `cn()` from `lib/utils.ts`; use `formatFileSize`/`formatDate` from `lib/formatters.ts`.

### TypeScript & lint
- `tsconfig` is `strict` with `noUnusedLocals`/`noUnusedParameters`. Use `import type` for type-only imports.
- Lint is **zero-warning**: `eslint src --max-warnings 0`. `react-hooks/exhaustive-deps` is enforced.
- Scripts: `npm run typecheck` (`tsc --noEmit`), `npm run lint`, `npm test` (`vitest run`), `npm run build` (`tsc && vite build`).

---

## 4. Multi-agent skill layout

Three agent runners operate in this repo; each loads skills from its own tree:

| Runner | Skill dir | Entry doc |
|---|---|---|
| Claude Code | `.claude/skills/` | `CLAUDE.md` |
| Codex | `.agents/skills/` | `AGENTS.md` |
| opencode-swarm | `.opencode/skills/` | `AGENTS.md` |

Repo-specific skills worth knowing: `commit-pr` (branch/commit/PR protocol), `ci-compatibility-audit` (reproduce CI locally), `config-env-contract-check`, `review-finding-validator`, `engineering-conventions` (this doc), `writing-tests` (see `docs/engineering/testing.md`).

**When adding or changing a repo-specific skill, mirror it across all three trees** (or keep it a thin pointer to a canonical doc, as `engineering-conventions` does) so every runner stays consistent. Do not assume one tree's contents apply to another. Skills explicitly tied to *other* projects (e.g. an "opencode-swarm internals" skill) do not belong here.

---

## 5. Branch / commit / PR

Use the `commit-pr` skill. In short: default branch `master`; feature branch prefix matches the runner (`claude/`, `codex/`); conventional commit titles (`feat`/`fix`/`test`/`docs`/`refactor`/`chore`); draft PRs against `master` with `## Summary` + `## Test plan`; never `git push --force` (use `--force-with-lease`); keep session/IDE artifacts out of commits.

Before any push or PR, run `ci-compatibility-audit` so CI gates pass locally first.
