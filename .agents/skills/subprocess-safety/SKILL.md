---
name: subprocess-safety
description: >
  Safe Python subprocess patterns for RAGAPPv3. Load before adding, modifying,
  or reviewing any code that calls subprocess.run / subprocess.Popen /
  asyncio.create_subprocess_exec. Covers list-form args (no shell=True with
  untrusted input), timeout, cwd, bounded stdout/stderr capture, returncode
  checks, cleanup, and the async variant. This repo's backend is Python only —
  ignore any Bun / Node child_process / Windows .cmd guidance.
---

# Subprocess Safety (Python)

RAGAPPv3 is Python/FastAPI + npm. The only subprocesses are Python-side and
light — e.g. the git calls in `scripts/check_pr_scope_drift.py`. There is **no**
Bun, Node `child_process`, or `.cmd` shim concern here. Keep it simple and safe.

## When to use this skill

You are adding, modifying, or reviewing any call to:
`subprocess.run`, `subprocess.Popen`, `subprocess.check_output`,
`asyncio.create_subprocess_exec`, or `asyncio.create_subprocess_shell`.

## Canonical shape (sync)

This mirrors the existing in-repo pattern (`scripts/check_pr_scope_drift.py`):

```python
import subprocess

proc = subprocess.run(
    ["git", "diff", "--name-only", f"{base}..HEAD"],  # list form, NOT a string
    cwd=ROOT,                 # explicit working directory, never inherited cwd
    check=False,              # inspect returncode yourself (or check=True to raise)
    text=True,                # decode stdout/stderr as str
    capture_output=True,      # bound + capture both streams (no unattended pipes)
    timeout=30,               # always set a timeout — nothing is "always fast"
)
if proc.returncode != 0:
    # handle the failure explicitly; do not assume success
    ...
```

## Six rules

| Rule | Why |
|------|-----|
| **List-form args** (`["git", "diff", …]`) | No shell parsing, no quoting bugs, no injection from interpolated values. |
| **Never `shell=True` with untrusted input** | A single attacker-controlled string becomes arbitrary command execution. If you think you need a shell, you almost certainly don't — pass a list. |
| **`timeout=<seconds>`** | A hung child otherwise blocks the request/worker forever. On timeout `subprocess.run` raises `TimeoutExpired` after killing the child. |
| **Explicit `cwd=`** | Never depend on the inherited process cwd — it differs between CLI, tests, and the server. |
| **Capture + bound stdout/stderr** | Use `capture_output=True` (or `stdout=PIPE, stderr=PIPE`). For untrusted/unbounded output, cap it (e.g. slice `proc.stdout[:LIMIT]`) so a chatty child can't exhaust memory. |
| **Check `returncode`** | Either `check=True` (raises `CalledProcessError`) or test `proc.returncode` explicitly. A non-zero exit is not success. |

## `shell=True` — the one trap that matters

```python
# WRONG — untrusted `name` is interpolated into a shell command string
subprocess.run(f"grep {name} file.txt", shell=True)   # injection: name='x; rm -rf /'

# CORRECT — list form, no shell, value passed as a single argv element
subprocess.run(["grep", name, "file.txt"], cwd=ROOT, timeout=30, capture_output=True)
```

If a constant, fully-internal command genuinely needs shell features (pipes,
globbing), keep the whole command literal and interpolate **nothing** from
request data, DB rows, or filenames.

## Async variant

For async code (FastAPI handlers, async services), use
`asyncio.create_subprocess_exec` (list form — the `_exec` variant never uses a
shell) and wrap the wait in `asyncio.wait_for` for the timeout:

```python
import asyncio

proc = await asyncio.create_subprocess_exec(
    "git", "rev-parse", "--show-toplevel",
    cwd=ROOT,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
try:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
except asyncio.TimeoutError:
    proc.kill()                 # terminate the orphaned child
    await proc.wait()           # reap it so it doesn't linger as a zombie
    raise
if proc.returncode != 0:
    ...
```

- Prefer `create_subprocess_exec` (no shell) over `create_subprocess_shell`.
- `asyncio` subprocesses have **no** `timeout=` kwarg — enforce it with
  `asyncio.wait_for`, then `kill()` + `await proc.wait()` on timeout.
- Always `await proc.wait()` (or `communicate()`) so the child is reaped.

## Review checklist

For every subprocess call in the diff, confirm:

1. Args are **list form** (or, if `shell=True`, the command is a fixed literal
   with no interpolated untrusted data).
2. A **timeout** is set (`timeout=` for sync; `asyncio.wait_for` for async).
3. **`cwd=`** is explicit.
4. stdout/stderr are **captured and bounded**, not left as unattended pipes.
5. **`returncode`** is checked (or `check=True`).
6. On timeout/error the child is **killed and reaped** (async) — `subprocess.run`
   handles this for you on `TimeoutExpired`.

Verification grep after editing:

```bash
grep -rn "subprocess\.\|create_subprocess" backend/ scripts/
```
