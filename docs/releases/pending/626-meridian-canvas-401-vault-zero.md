# 626 — Meridian Canvas auth and vault-selection safeguards

## What changed

- Creating a vault now selects it (store state and the persisted
  `kv_active_vault_id`), so uploads and chat that snapshot the active vault
  target the vault the user just created instead of the previously-active
  one. Selection is best-effort: a storage failure cannot fail the create.
- `POST /auth/refresh` failures under a subpath deployment now surface an
  operator-facing `console.error` naming both halves of the subpath contract
  (`APP_ROOT_PATH` and `VITE_APP_BASENAME`) instead of being silently
  swallowed. The diagnostic fires only for authentication-shaped rejections
  (401, or 403 with `x-csrf-error: true`) — the two signatures a cookie-path
  mismatch produces — at most once per failure burst. Bursts reset on a
  successful refresh and at session boundaries (login/register/logout).

## Why

A frontend built with `VITE_APP_BASENAME=/meridian` behind a backend running
without `APP_ROOT_PATH=/meridian` never receives its refresh cookie (the
cookie `Path` no longer matches the prefixed URL), so silent token refresh
failed forever with no observable signal: canvas would not open and every
authenticated request 401'd after the access token expired. Separately, a
vault created and then immediately used for uploads silently targeted the
previous vault.

## Migration steps

None. Subpath operators changing either prefix must keep `APP_ROOT_PATH` and
`VITE_APP_BASENAME` equal; `VITE_APP_BASENAME` is baked into the frontend
image at build time, so changing it requires
`docker compose build --no-cache && docker compose up -d`.
