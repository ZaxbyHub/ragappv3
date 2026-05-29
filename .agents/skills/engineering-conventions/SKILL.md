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

**Hugeicons + lucide mixed icon discrimination**
- The navigation rail (`NavigationRail.tsx`) mixes `@hugeicons/core-free-icons` icons (type `IconSvgElement` — a readonly array) with `lucide-react` icons (type `React.ComponentType`, rendered as `forwardRef` objects). When adding new nav items or any component that accepts a `ComponentType | IconSvgElement` union, discriminate with `Array.isArray`, NOT `typeof === "function"`. Lucide `forwardRef` components have `typeof === "object"`, not `"function"` — the wrong guard routes them to `HugeiconsIcon`, which spreads its `icon` prop (`[...icon]`) and throws `TypeError: currentIcon is not iterable` at render time on every authenticated page.
- Canonical pattern: `function isHugeicon(icon): icon is IconSvgElement { return Array.isArray(icon); }` — use this as a JSX type guard.
- `HugeiconsIcon` is safe for any prop that resolves to an actual hugeicons array export. Never pass a React component type to it, even if it looks like a valid JSX element.

**TypeScript tsconfig boundary (Vite context)**
- `tsconfig.node.json` covers only `vite.config.ts`. `tsconfig.json` covers `src/` and references `tsconfig.node.json` as a composite project.
- Do NOT add files from `src/` to `tsconfig.node.json`'s `include` array and do NOT import `src/` files from `vite.config.ts` or `vite.paths.ts`. Doing so produces `Output file has not been built from source file` — a TypeScript composite conflict because both tsconfigs would include the same file.
- When logic must be shared between `vite.paths.ts` and `src/lib/`, keep two copies with cross-reference comments. The subpath deployment helpers (`normalizeBasePath`) follow this pattern: canonical source in `src/lib/normalize-base-path.ts`, inline duplicate in `vite.paths.ts`.

See `docs/engineering/conventions.md` for the full detail and file references.
