---
name: authz-bridging-exceptions
description: Document two anti-patterns in the auth override branch: (a) masking non-401 HTTPExceptions when falling back to an alternative auth mechanism, and (b) using inspect.isawaitable() instead of inspect.iscoroutine() on the evaluate closure, which yields None for async def overrides. Critical for auth dependencies with JWT + service-account (SA) fallback (get_chat_stream_auth_context, get_wiki_events_auth_context, and any override branch calling get_evaluate_policy).
disable-model-invocation: true
generated_at: 2026-07-02T22:30:00Z
---

# authz-bridging-exceptions

When a FastAPI dependency wraps JWT auth + service-account (SA) auth
(get_evaluate_policy), the override branch must only fall through on
401 (genuine "no credentials / invalid credentials"). It must NOT mask 403
(e.g. `must_change_password`) or any other 4xx/5xx — those are
authorization or server-state signals that must propagate to the client
unchanged.

## The anti-pattern (this repo had it before fix)

```python
# WRONG — masks 403 from a downstream must_change_password check
overrides = getattr(request.app, "dependency_overrides", {})
user_override = overrides.get(get_current_active_user)
evaluate_override = overrides.get(get_evaluate_policy)
if user_override is not None:
    user = user_override()
    if inspect.isawaitable(user):
        user = await user
    if evaluate_override is not None:
        evaluate = evaluate_override()
        if inspect.isawaitable(evaluate):
            evaluate = await evaluate
    else:
        async def evaluate(*_args, **_kwargs):
            return False

    if body.vault_id is not None:
        result = evaluate(user, "vault", body.vault_id, "read")
        if inspect.iscoroutine(result):
            result = await result
        if not result:
            raise HTTPException(
                status_code=403,
                detail="No read access to this vault",
            )
    return user
```

The masking issue: `if user_override is not None` falls through to the SA
path. The SA path's `evaluate_override()` may call a function that swallows
exceptions. The override may set `evaluate` to a coroutine that the
`inspect.isawaitable(evaluate)` check passes. After all that, the
`if body.vault_id is not None` check uses `evaluate` — which might be a
mocked coroutine returning `True` even if the user has no real access.

The specific failure mode observed: tests patched `chat_routes.get_evaluate_policy`
with a function returning `True`. Combined with `app.dependency_overrides[get_evaluate_policy]`
being patched, the override branch's eval returned `True` even for users
who should have been denied. The route returned 200 instead of 403.

## The correct pattern

```python
# CORRECT — only fall through on 401 (true lack of credentials)
overrides = getattr(request.app, "dependency_overrides", {})
user_override = overrides.get(get_current_active_user)
evaluate_override = overrides.get(get_evaluate_policy)
if user_override is not None:
    user = user_override()
    if inspect.isawaitable(user):
        user = await user
    if evaluate_override is not None:
        evaluate = evaluate_override()
        if inspect.iscoroutine(evaluate):
            evaluate = await evaluate
    else:
        async def evaluate(*_args, **_kwargs):
            return False

    if body.vault_id is not None:
        result = evaluate(user, "vault", body.vault_id, "read")
        if inspect.iscoroutine(result):
            result = await result
        if not result:
            raise HTTPException(
                status_code=403,
                detail="No read access to this vault",
            )
    return user
```

The fix: `if inspect.isawaitable(evaluate): evaluate = await evaluate` becomes
`if inspect.iscoroutine(evaluate): evaluate = await evaluate`. The check uses
`iscoroutine` (true only for coroutine objects, not for coroutine functions).
This avoids the case where `evaluate` is an `async def` function — calling it
returns a coroutine. Without the `iscoroutine` distinction, `inspect.isawaitable(async_function)`
is `True`, and `await async_function` returns `None` (not the function's return
value). Then `evaluate = None` causes `result = None(user, ...)` to raise
`TypeError: 'NoneType' object is not callable`.

## When to apply

- The override branch in `get_chat_stream_auth_context` (chat.py).
- The override branch in `get_wiki_events_auth_context` (wiki.py).
- Any other FastAPI dependency that supports multiple auth mechanisms and
  uses a `if X_override is not None:` fallback pattern.

## Diagnostic check

If the override branch in a dep uses `inspect.isawaitable(evaluate)` followed
by `evaluate = await evaluate`, it is the anti-pattern. Replace with
`inspect.iscoroutine(evaluate)`.

```bash
grep -rn "if inspect.isawaitable" backend/app/ --include="*.py"
```

If the matches are inside an `if X_override is not None:` block (the override
fallback pattern), refactor each to use `iscoroutine`.

## Acceptance criteria

- All auth override branches in this repo use `inspect.iscoroutine` (NOT
  `isawaitable`) for the post-await check.
- A test that sets `app.dependency_overrides[get_evaluate_policy]` to a sync
  function returning `True` passes (correct fallback).
- A test that sets it to an `async def` function that calls another `async def`
  function does not break the dependency.
- The audit event for `auth.refresh_reuse_detected` is recorded with the
  proxy-aware IP from `security_audit._request_ip`, not `request.client.host`
  directly.

## Connection to other skills

- `auth-timestamp-invalidation` — covers the timestamp propagation pattern
  for token invalidation.
- `module-test-isolation` — when the override breaks tests in combined
  sessions, the test setup itself is the pollution source.
- `test-isolation-patterns` — the parent skill.
