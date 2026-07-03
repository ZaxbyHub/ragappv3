---
name: auth-timestamp-invalidation
description: Document the discipline of propagating timestamp fields through token invalidation flows. Use when implementing access-token refresh reuse, password change epoch, or any token revocation flow that must invalidate outstanding tokens.
disable-model-invocation: true
generated_at: 2026-07-02T22:30:00Z
---

# auth-timestamp-invalidation

When a refresh token is reused (theft signal) or a user changes their password,
outstanding access tokens must be invalidated. The mechanism is a per-user
`password_changed_at` (or `refresh_reuse_detected_at`) epoch column:

- Token issuance: `iat = password_changed_at` (or now)
- Token validation: `if iat < password_changed_at: reject`

The pattern requires that the token's `iat` (issued-at) is checked against the
user's stored epoch. This is the same mechanism used by Stripe and many other
auth systems.

## The pattern (this repo's FR-007)

```sql
-- Add the column
ALTER TABLE users ADD COLUMN password_changed_at REAL NOT NULL DEFAULT 0;
```

```python
# On password change
def on_password_change(user_id: int, db: sqlite3.Connection) -> None:
    new_epoch = datetime.now(timezone.utc).timestamp()
    db.execute(
        "UPDATE users SET password_changed_at = ? WHERE id = ?",
        (new_epoch, user_id),
    )
    # Also invalidate refresh-token sessions
    db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    # Bust the active-user cache so the next request sees the new epoch
    invalidate_active_user_cache(user_id)
```

```python
# In token validation
def validate_access_token(token: str, db: sqlite3.Connection) -> dict:
    payload = decode_access_token(token)
    user = get_user_by_id(payload["sub"], db)
    pwd_epoch = user.get("password_changed_at", 0) or 0
    if pwd_epoch > 0 and payload.get("iat", 0) < pwd_epoch:
        # Bust cache, return 401
        invalidate_active_user_cache(user["id"])
        raise HTTPException(
            status_code=401,
            detail="Token invalidated by password change",
        )
    return payload
```

## When to apply

- After implementing password change endpoint (`POST /auth/change-password`).
- After implementing admin password reset (`POST /users/{id}/reset-password`).
- After implementing refresh token reuse detection (`get_rotate_refresh_token_block`).
- After any user-event that should invalidate outstanding access tokens.

## Anti-patterns to avoid

- **Storing the epoch in a separate table** — joins are slow. Use a column on
  the `users` table directly.
- **Using a separate token for the epoch** — defeats the purpose. The
  `iat` claim in the existing JWT is the right place.
- **Forgetting to invalidate the active-user cache** — a stale cached user
  bypasses the new epoch.
- **Catching `BaseException`** to "be safe" — swallows `KeyboardInterrupt`
  and `SystemExit`. Only catch `Exception`.

## Diagnostic commands

```bash
# Find places where the epoch is set
grep -rn "password_changed_at" backend/app/ --include="*.py"

# Find places where tokens are validated
grep -rn "decode_access_token" backend/app/ --include="*.py"
```

## Acceptance criteria

- The `password_changed_at` column is added to the `users` table via
  a migration.
- The migration is idempotent (uses `PRAGMA table_info`).
- After password change, outstanding access tokens issued before
  `password_changed_at` are rejected with 401.
- The active-user cache is invalidated on password change and on the
  epoch-check rejection.
- A test that issues a token, changes the password, then presents the old
  token, gets 401.

## Connection to other skills

- `authz-bridging-exceptions` — the override branch in the auth dep
  must not mask the 401 from the epoch check.
- `test-isolation-patterns` — the test patch on the dep override must
  not interfere with the epoch check.
- `module-test-isolation` — the dep override is module-level state.
