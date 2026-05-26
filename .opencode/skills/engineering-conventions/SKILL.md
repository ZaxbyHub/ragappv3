---
name: engineering-conventions
description: >
  RAGAPPv3 engineering conventions — backend (FastAPI + SQLite + LanceDB),
  frontend (React + TypeScript + Vite + Vitest), database/migration/RBAC
  patterns, and the multi-agent skill layout. Load before implementing or
  refactoring backend routes/services/migrations or frontend pages/components,
  or when you need to know "how does this repo do X".
---

# RAGAPPv3 Engineering Conventions

The authoritative, detailed conventions live in **`docs/engineering/conventions.md`**.
Read it before non-trivial backend or frontend work. Match the pattern in the
file you are editing over anything summarized here.

## Must-know invariants (summary)

**Backend (`backend/app/`)**
- Routes export `router = APIRouter()`, registered in `app/main.py` with `prefix="/api"`. Inject deps via `Depends(...)` (`get_db`, `get_vector_store`, `get_current_active_user`, `get_evaluate_policy`).
- Request/response shapes are Pydantic `BaseModel`. Errors via `raise HTTPException(status_code, detail)`. Register static routes before dynamic `{id}` routes to avoid shadowing.
- All connections come from `SQLiteConnectionPool` with **`PRAGMA foreign_keys = ON`** — rely on `ON DELETE CASCADE`. Schema is the `SCHEMA` constant; migrations are idempotent `migrate_add_*` functions registered in `run_migrations`.
- SQLite is sync; wrap blocking calls in `await asyncio.to_thread(...)`. Atomic multi-statement writes use `BEGIN IMMEDIATE` (clear any dangling tx with `if conn.in_transaction: conn.rollback()` first).
- Authorize via `await evaluate(user, "vault", vault_id, action)`; scope every user-data query by vault.

**Frontend (`frontend/src/`)**
- Single axios client in `lib/api.ts` (`VITE_API_URL`, Bearer + CSRF interceptors). Prefer options-object function signatures. IDs are typed as declared in `api.ts` (`Document.id` is a `string`).
- Pages are slim orchestrators composing local hooks + feature components (the DocumentsPage pattern). State via Zustand. Routes are lazy + `ProtectedRoute` + `MainAppShell`.
- `strict` TS, zero-warning lint (`eslint src --max-warnings 0`). Scripts: `typecheck`, `lint`, `test` (`vitest run`), `build`.

**Repo**
- Three agent runners, three skill trees (`.claude/`, `.agents/`, `.opencode/`). Mirror repo-specific skills across all three, or keep them thin pointers to canonical docs.
- Before push/PR: run `ci-compatibility-audit`. For tests: `writing-tests` / `docs/engineering/testing.md`. For commits/PRs: `commit-pr`.

See `docs/engineering/conventions.md` for the full detail and file references.
