---
name: subpath-deployment
description: >
  RAGAPPv3 subpath/reverse-proxy deployment system. Load before touching
  anything related to APP_ROOT_PATH, VITE_APP_BASENAME, VITE_API_URL, cookie
  paths, SPA catch-all routing, proxy configuration, or Docker build args for
  the frontend. Contains locked architectural decisions that must not be
  re-litigated.
---

# RAGAPPv3 Subpath Deployment

## Why this skill exists

The subpath deployment system has non-obvious constraints. Agents who have not
read this file have proposed runtime header detection, root-path routing,
hardcoded `/api` fallbacks, and TypeScript import shortcuts that create composite
project conflicts. Read this before implementing anything in this area.

---

## Locked architectural decisions

These were validated by 8 independent reviewers and must not be revisited:

### 1. Prefix-stripping proxy only

The reverse proxy strips the external prefix before forwarding. The backend
receives bare paths (`/api/`, `/assets/`, `/`). This is the only supported model.

```nginx
# NGINX — trailing slash on proxy_pass triggers stripping
location /meridian/ {
    proxy_pass http://backend:9090/;
}
```

```caddyfile
# Caddy — handle_path strips by design
handle_path /meridian/* {
    reverse_proxy backend:9090
}
```

The backend does NOT receive `/meridian/api/...`. If it does, that is a proxy
misconfiguration — the `is_unstripped_prefix()` guard in the SPA catch-all
returns 404 with a diagnostic message for exactly this case.

### 2. Backend is prefix-agnostic

All API routers use `prefix="/api"`. Static files mount at `/assets`. The SPA
catch-all handles `/{full_path:path}`. `APP_ROOT_PATH` is used ONLY for:
- cookie `Path` values (so the browser sends them on prefixed URLs)
- FastAPI `root_path` (for OpenAPI docs URL generation)

`APP_ROOT_PATH` is NEVER used for routing.

### 3. Frontend prefix is baked at Docker build time

`VITE_APP_BASENAME` and `VITE_API_URL` are Docker build args consumed by
`npm run build`. Vite replaces `import.meta.env.*` at build time. A root-built
image CANNOT be fixed at runtime. Rebuild the image when the prefix changes.

### 4. No runtime `X-Forwarded-Prefix` detection

The frontend must know its prefix at build time. `X-Forwarded-Prefix` is not
a primary mechanism and is not wired. Do not add it.

### 5. Cookie `Path` must match the browser-visible (prefixed) URL

The browser matches cookie `Path` against the URL bar path, not the backend
internal path. A refresh cookie scoped to `Path=/api/auth/refresh` will NOT be
sent on requests to `/meridian/api/auth/refresh`. All `set_cookie` and
`delete_cookie` calls must use `refresh_cookie_path()` and `csrf_cookie_path()`.

---

## The three-variable contract

| Variable | Where set | When consumed | Purpose |
|---|---|---|---|
| `APP_ROOT_PATH` | Backend runtime env | App startup | Cookie paths, FastAPI root_path, startup log |
| `VITE_APP_BASENAME` | Docker build arg → ENV | `npm run build` | Vite `base`, BrowserRouter `basename`, `appPath()` default |
| `VITE_API_URL` | Docker build arg → ENV (optional) | `npm run build` | API base URL; defaults to `appPath("/api")` when empty |

`VITE_API_URL` is intentionally optional. When omitted, `api.ts` derives it
from `appPath("/api")`, which uses `VITE_APP_BASENAME`. Setting `VITE_API_URL`
explicitly overrides this (e.g., for an external gateway).

**Critical Docker default:** Both `Dockerfile` and `docker-compose.yml` must
default `VITE_API_URL` to empty string (`ARG VITE_API_URL=` / `VITE_API_URL: ${VITE_API_URL:-}`).
If it defaults to `/api`, the `appPath` derivation never fires and subpath
deployments silently break — the exact bug this system was built to fix.

---

## Key files

| File | Role |
|---|---|
| `backend/app/utils/paths.py` | `normalize_root_path()`, `refresh_cookie_path()`, `csrf_cookie_path()`, `is_unstripped_prefix()` |
| `backend/app/config.py` | `app_root_path` field + `validate_app_root_path` validator (delegates to `normalize_root_path`) |
| `backend/app/main.py` | FastAPI `root_path=`, startup log, SPA catch-all with proxy guard |
| `frontend/src/lib/normalize-base-path.ts` | Shared `normalizeBasePath()` used by `paths.ts` |
| `frontend/src/lib/paths.ts` | `APP_BASENAME`, `appPath()`, `logSubpathConfig()` |
| `frontend/src/lib/api.ts` | `API_BASE_URL = VITE_API_URL \|\| appPath("/api")` |
| `frontend/vite.paths.ts` | `normalizeViteBase()`, `createApiProxy()`, `rewriteApiProxyPath()` |
| `frontend/vite.config.ts` | Sets Vite `base`, dev proxy |
| `scripts/validate_vite_env.mjs` | Build-time validation of `VITE_APP_BASENAME` |
| `scripts/check_config_contract.py` | CI contract check across all config surfaces |
| `frontend/.env.example` | Documents build-time env vars |
| `docs/admin-guide.md` | Full operator reference, prefix-change walkthrough, NGINX/Caddy examples |

---

## TypeScript tsconfig boundary constraint

`vite.config.ts` runs under `tsconfig.node.json`. `tsconfig.node.json` includes
ONLY `vite.config.ts`. Files in `src/` are covered by `tsconfig.json`, which
references `tsconfig.node.json` (composite project).

**You cannot import from `src/` in `vite.config.ts` or `vite.paths.ts`.**
Doing so causes: `Output file has not been built from source file` (TypeScript
composite project conflict) because both tsconfigs would include the same file.

The solution: `vite.paths.ts` keeps its own inline copy of `normalizeBasePath`
with a cross-reference comment pointing to `src/lib/normalize-base-path.ts`.
If you change the normalization logic, update BOTH files.

---

## `is_unstripped_prefix()` behavior

```python
# backend/app/utils/paths.py
def is_unstripped_prefix(full_path: str, root_path: str) -> bool:
```

- Returns `True` when `full_path` still carries the external prefix
  (indicates proxy did not strip it)
- Boundary-safe: `"meridianx/foo"` does NOT match `/meridian`
- Case-sensitive — `/Meridian/api` does not match `/meridian`
- Called in the SPA catch-all to return 404 with diagnostic instead of
  serving `index.html`

---

## Dev proxy parity (N-6)

When `VITE_APP_BASENAME` is set, `createApiProxy()` returns ONLY the prefixed
entry (e.g., `/knowledgevault/api`). It does NOT also return `/api`. This
forces developers to use the prefix in dev, matching production behavior and
catching prefix bugs during development rather than at deploy time.

---

## Changing the prefix

To change from `/knowledgevault` to `/meridian`:

1. Rebuild the frontend Docker image:
   ```
   docker build --build-arg VITE_APP_BASENAME=/meridian [--build-arg VITE_API_URL=/meridian/api] -t ragapp-frontend .
   ```
2. Update backend env: `APP_ROOT_PATH=/meridian`
3. Update the reverse proxy to strip `/meridian/` instead of `/knowledgevault/`
4. Update `CORS_ORIGINS` if the origin changes

No code changes are required. See `docs/admin-guide.md` for full walkthrough.

---

## Streaming endpoint requirement

All `StreamingResponse` instances in chat and wiki routes must include:
```python
headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
```
Without this, NGINX's default `proxy_buffering on` batches chunks and breaks
the streaming UX. See deployment docs for the corresponding NGINX directives.

---

## SSE streaming auth — DO NOT use EventSource for authenticated endpoints

**Locked decision (N-7):** Any SSE endpoint guarded by `get_current_active_user`
must be consumed via `fetch`, not the browser `EventSource` API.

**Why:** This app's auth is Bearer-token-in-memory. The access JWT lives in a JS
module variable (`_jwtAccessToken` in `frontend/src/lib/api.ts`) and is attached
to requests by an axios interceptor. No `access_token` cookie is ever set
(only `refresh_token` scoped to `/api/auth/refresh` and the CSRF cookie are
issued). The browser `EventSource` API cannot set custom headers, so it arrives
at the backend with no usable credential and always gets `401 Not Authenticated`.
The browser's native EventSource auto-reconnect then floods the console with 401s.

**The established pattern** is `frontend/src/hooks/useWikiEventStream.ts`:
- `fetch(url, { method: "GET", headers: { Authorization: "Bearer <token>" }, signal })`
- Read `response.body.getReader()` and parse SSE lines with a dedicated dispatcher
  (split on `\n`, strip `data:`, dispatch on `data.type`; do NOT reuse `parseSSEStream`
  from `api.ts` — it is chat-specific and uses `parsed.type` to gate content forwarding,
  not `data.type` as the terminal-event key)
- On `401 token_expired`: call `refreshAccessToken()` and retry
- On `401 token_invalid` / `user_inactive`: stop; do not reconnect
- Return `'clean' | 'error' | 'stop'` from the connection function so the outer
  reconnect loop can reset backoff to the base delay after a clean server close

**Do not reach for EventSource** when adding new SSE endpoints to this codebase,
regardless of whether the deployment is at root or under a subpath prefix.

---

## Contract atomicity rule

When changing any env var default across this system, all of these must be
updated in the same commit:
- `.env.example`
- `docker-compose.yml`
- `Dockerfile` (root)
- `frontend/Dockerfile`
- `scripts/check_config_contract.py`

Missing any one surface fails the CI contract gate with an error that is hard
to diagnose without knowing which surface was missed.
