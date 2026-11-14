"""Implementation mutation legs for the widened a04 gate (issue #1119 AC1).

The frozen spec (``test_a04_variable_status_echo.py``) pins the two acceptance
legs: a planted variable-status 5xx echo must fail the gate, and the REAL
audited modules must stay unflagged. This file pins the eight analyzer
mutation shapes the plan-critic ladder executed as counterexamples (rounds 2-5,
probe v5 legs L3-L10) so the widened predicate cannot silently regress:

  R2-F3  unguarded module-level mixed mapping + 500 default  -> FLAG
  R3-F2a the same violation with a FUNCTION-LOCAL mapping    -> FLAG
  R3-F2b status rebound to 500 after a dominating guard      -> FLAG
  R4-F1a call BEFORE rebinding (500+str(exc), later 404)     -> FLAG
  R4-F1b ASYNC function, literal 500 + str(exc)             -> FLAG
  R4-F1c MODULE-LEVEL literal 500 + str(exc)                -> FLAG
  R5-F1  STRICT > 500 guard leaves 500 reachable            -> FLAG
  ctrl   guarded function-local mapping                      -> NOT flagged

Harness mirrors the frozen spec's planted-tree idiom (gate copied into a
synthetic tree, run as a subprocess, draft_room.py replaced).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SCRIPT_REL = Path("scripts") / "check_a04_http500_detail_hygiene.py"
SCRIPT = REPO / SCRIPT_REL
ROUTE_REL = {
    name: Path("backend") / "app" / "api" / "routes" / name
    for name in ("memories.py", "vaults.py", "admin.py", "draft_room.py")
}
DIAGNOSTIC_LIMIT = 12_000

HEADER = """class DraftRoomHTTPError(Exception):
    pass
"""

MIXED_MODULE_LEVEL = (
    HEADER
    + '''

_CODES = {"conflict": 409}


def leak(exc):
    status_code = _CODES.get(exc.code, 500)
    raise DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

MIXED_FUNCTION_LOCAL = (
    HEADER
    + '''

def leak(exc):
    codes = {"conflict": 409}
    status_code = codes.get(exc.code, 500)
    raise DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

REBIND_AFTER_GUARD = (
    HEADER
    + '''

def leak(exc):
    status_code = 404
    if status_code >= 500:
        raise DraftRoomHTTPError(status_code, "fixed", "x")
    status_code = 500
    raise DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

CALL_BEFORE_REBIND = (
    HEADER
    + '''

def call_before_rebind(exc):
    status_code = 500
    err = DraftRoomHTTPError(status_code, str(exc), "mapper")
    status_code = 404
    return err
'''
)

ASYNC_LITERAL_500 = (
    HEADER
    + '''

async def async_handler(exc):
    raise DraftRoomHTTPError(500, str(exc), "internal_error")
'''
)

MODULE_LEVEL_LITERAL_500 = (
    HEADER
    + '''

raise DraftRoomHTTPError(500, str(exc_var), "internal_error")
'''
)

STRICT_GT_GUARD = (
    HEADER
    + '''

def strict_gt_guard(exc):
    status_code = 500
    if status_code > 500:
        return DraftRoomHTTPError(status_code, "fixed", "x")
    return DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

GUARDED_FUNCTION_LOCAL_CONTROL = (
    HEADER
    + '''

def guarded(exc):
    codes = {"conflict": 409}
    status_code = codes.get(exc.code, 500)
    if status_code >= 500:
        return DraftRoomHTTPError(status_code, "fixed detail", "x")
    return DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)


def _run_gate(tmp_path: Path, draft_room_content: str) -> subprocess.CompletedProcess:
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    for name, rel in ROUTE_REL.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            draft_room_content
            if name == "draft_room.py"
            else (REPO / rel).read_text(encoding="utf-8")
        )
        target.write_text(content, encoding="utf-8", newline="\n")
    return subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )


def _flagged(result: subprocess.CompletedProcess) -> list[int]:
    return sorted(
        int(line.rsplit(":", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("RAW-EXCEPTION-DETAIL: backend/app/api/routes/draft_room.py:")
    )


def _echo_lines(source: str) -> list[int]:
    return [
        i + 1
        for i, line in enumerate(source.splitlines())
        if "DraftRoomHTTPError(status_code, str(exc)" in line
        or "DraftRoomHTTPError(status_code, f\"" in line
        or "DraftRoomHTTPError(500, str(" in line
    ]


def _assert_flagged(tmp_path, source: str, label: str) -> None:
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == _echo_lines(source), (
        f"{label}: the widened gate must flag exactly the 5xx-capable echo "
        f"call(s); exit={result.returncode} flagged={_flagged(result)} "
        f"expected={_echo_lines(source)}; stdout: {result.stdout}; "
        f"stderr: {result.stderr}"[:DIAGNOSTIC_LIMIT]
    )


def _assert_clean(tmp_path, source: str, label: str) -> None:
    result = _run_gate(tmp_path, source)
    assert result.returncode == 0 and _flagged(result) == [], (
        f"{label}: the guarded control must stay unflagged; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_r2f3_unguarded_module_level_mixed_mapping_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, MIXED_MODULE_LEVEL, "R2-F3 mixed module-level")


def test_r3f2a_function_local_mapping_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, MIXED_FUNCTION_LOCAL, "R3-F2a function-local mapping")


def test_r3f2b_rebind_after_guard_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, REBIND_AFTER_GUARD, "R3-F2b rebind-after-guard")


def test_r4f1a_call_before_rebinding_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, CALL_BEFORE_REBIND, "R4-F1a call-before-rebind")


def test_r4f1b_async_literal_500_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, ASYNC_LITERAL_500, "R4-F1b async literal-500")


def test_r4f1c_module_level_literal_500_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, MODULE_LEVEL_LITERAL_500, "R4-F1c module-level literal")


def test_r5f1_strict_gt_guard_leaves_500_reachable(tmp_path) -> None:
    _assert_flagged(tmp_path, STRICT_GT_GUARD, "R5-F1 strict-gt guard")


def test_r45f1_try_else_literal_500_flags(tmp_path) -> None:
    """Implementation-review round-1 F2: the try/except/ELSE arm must stay
    covered (the base gate's ast.walk visited every node; the ordered
    visitor must too)."""
    source = (
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        "def handler(exc):\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception as e:  # noqa: BLE001\n"
        "        pass\n"
        "    else:\n"
        '        raise HTTPException(status_code=500, detail=f"boom {exc}")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [10], (
        "the try/except/ELSE arm must stay covered by the ordered visitor "
        f"(base gate's ast.walk visited it); exit={result.returncode} "
        f"flagged={_flagged(result)}; stdout: {result.stdout}; "
        f"stderr: {result.stderr}"[:DIAGNOSTIC_LIMIT]
    )


def test_fc1a_branch_merged_dict_union_flags_both_orders(tmp_path) -> None:
    """Final-critic round-1 F1a: a mapping bound on both branches of an if
    with different values — the union must be taken regardless of which
    branch holds the 5xx value (order-independence)."""
    for order in ("400-first", "503-first"):
        a, b = ('{"a": 400}', '{"a": 503}') if order == "400-first" else ('{"a": 503}', '{"a": 400}')
        source = (
            "class DraftRoomHTTPError(Exception):\n"
            "    pass\n"
            "\n"
            "\n"
            "def f(exc, flag):\n"
            "    if flag:\n"
            f"        m = {a}\n"
            "    else:\n"
            f"        m = {b}\n"
            '    status = m.get("a", 400)\n'
            '    raise DraftRoomHTTPError(status, str(exc), "x")\n'
        )
        result = _run_gate(tmp_path, source)
        assert result.returncode == 1 and _flagged(result) == [11], (
            f"FC1a ({order}): the merged dict must union both branches' "
            f"values; exit={result.returncode} flagged={_flagged(result)}; "
            f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
        )


def test_fc1b_for_loop_binding_flags(tmp_path) -> None:
    """Final-critic round-1 F1b: a status assigned inside a for body is a
    POSSIBLE binding (zero or more iterations) — the echo must flag."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc, xs):\n"
        "    status = 400\n"
        "    for x in xs:\n"
        "        status = 503\n"
        '    raise DraftRoomHTTPError(status, str(exc), "x")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [9], (
        f"FC1b: loop-assigned statuses must not be treated as their pre-loop "
        f"value alone; exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fc1c_with_block_binding_flags(tmp_path) -> None:
    """Final-critic round-1 F1c: assignments inside a with block bind for
    the code after it — the echo must flag."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc, lk):\n"
        "    with lk:\n"
        "        status = 503\n"
        '    raise DraftRoomHTTPError(status, str(exc), "x")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [8], (
        f"FC1c: with-block assignments must bind; exit={result.returncode} "
        f"flagged={_flagged(result)}; stdout: {result.stdout}"[
            :DIAGNOSTIC_LIMIT
        ]
    )


def test_fc1d_annassign_binding_flags(tmp_path) -> None:
    """Final-critic round-1 F1d: `status: int = 503` binds exactly like a
    plain assignment — the echo must flag."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc):\n"
        "    status: int = 503\n"
        '    raise DraftRoomHTTPError(status, str(exc), "x")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [7], (
        f"FC1d: AnnAssign must bind; exit={result.returncode} "
        f"flagged={_flagged(result)}; stdout: {result.stdout}"[
            :DIAGNOSTIC_LIMIT
        ]
    )


def test_r3f1_async_with_binding_flags(tmp_path) -> None:
    """Implementation-review round-3 F1: `async with` is the audited FastAPI
    routes' natural idiom — assignments inside it must bind exactly like a
    sync `with` (the async-with false negative)."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "async def f(exc, lk):\n"
        "    async with lk:\n"
        "        status = 503\n"
        '    raise DraftRoomHTTPError(status, str(exc), "x")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [8], (
        f"R3-F1: async-with assignments must bind; exit={result.returncode} "
        f"flagged={_flagged(result)}; stdout: {result.stdout}"[
            :DIAGNOSTIC_LIMIT
        ]
    )


def test_fc2r2_loop_carried_binding_flags(tmp_path) -> None:
    """Final-critic round-2: a call EARLY in a loop body sees the binding a
    LATER line sets on the NEXT pass (loop-carried)."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc, xs):\n"
        "    status = 400\n"
        "    for x in xs:\n"
        "        if x:\n"
        '            raise DraftRoomHTTPError(status, str(exc), "x")\n'
        "        status = 503\n"
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [9], (
        f"FC2r2: loop-carried bindings must reach earlier call sites on the "
        f"next pass; exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fc3r3_var_chain_inside_loop_flags(tmp_path) -> None:
    """Final-critic round-3: a 5xx value reaching the status through a CHAIN
    of var-to-var copies inside a loop body needs one pass per hop — the
    loop analysis runs to the state fixpoint, and var copies resolve."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc, xs):\n"
        "    status = 400\n"
        "    s2 = 400\n"
        "    for x in xs:\n"
        "        if x:\n"
        '            raise DraftRoomHTTPError(status, str(exc), "x")\n'
        "        status = s2\n"
        "        s2 = 503\n"
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [10], (
        f"FC3r3: var chains inside loops must converge (one pass per hop); "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_control_guarded_function_local_stays_clean(tmp_path) -> None:
    _assert_clean(tmp_path, GUARDED_FUNCTION_LOCAL_CONTROL, "guarded control")


# ---------------------------------------------------------------------------
# Feedback round (FB-002/FB-003/FB-009) legs
# ---------------------------------------------------------------------------


def test_fb002_class_body_literal_flags(tmp_path) -> None:
    """FB-002: the whole-tree literal pass restores class-body coverage
    (the scoped traversal skips ClassDef statements)."""
    source = (
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        "class H:\n"
        '    x = HTTPException(status_code=500, detail=str("boom"))\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [5], (
        f"FB-002 class-body: literal-500 echo must flag; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fb002_decorator_arg_literal_flags(tmp_path) -> None:
    """FB-002: decorator-argument placements are covered by the literal
    pass."""
    source = (
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        "def deco(x):\n"
        "    return x\n"
        "\n"
        "\n"
        '@deco(HTTPException(500, str("boom")))\n'
        "def f():\n"
        "    return 1\n"
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [8], (
        f"FB-002 decorator: literal-500 echo must flag; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fb002_default_arg_literal_flags(tmp_path) -> None:
    """FB-002: default-argument placements are covered by the literal
    pass."""
    source = (
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        'def g(e=HTTPException(500, detail=str("boom"))):\n'
        "    return e\n"
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [4], (
        f"FB-002 default-arg: literal-500 echo must flag; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fb003_try_rebind_handler_echo_flags(tmp_path) -> None:
    """FB-003: a status rebound inside the try body is visible to the
    except-handler echo (handler base merges the end-of-body state)."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc, flag):\n"
        "    status = 404\n"
        "    try:\n"
        "        status = 503\n"
        "        risky()\n"
        "    except Exception:\n"
        '        raise DraftRoomHTTPError(status, str(exc), "x")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [11], (
        f"FB-003 try-rebind-handler: the handler echo must flag; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fb003_guarded_handler_control_stays_clean(tmp_path) -> None:
    """FB-003 control: a dominating >= 500 guard inside the handler keeps
    the refined fall-through clean even after the body merge."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc, flag):\n"
        "    status = 400\n"
        "    try:\n"
        "        status = 503\n"
        "        risky()\n"
        "    except Exception:\n"
        "        if status >= 500:\n"
        '            raise DraftRoomHTTPError(status, "fixed detail", "x")\n'
        '        return DraftRoomHTTPError(status, str(exc), "mapper")\n'
    )
    _assert_clean(tmp_path, source, "FB-003 guarded-handler control")


def test_fb009_dict_rebound_to_int_flags(tmp_path) -> None:
    """FB-009: a dict-bound name rebound to an int resolves the NEW value
    (the stale dicts entry is cleared on non-dict rebind)."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc):\n"
        '    codes = {"conflict": 409}\n'
        "    codes = 503\n"
        '    raise DraftRoomHTTPError(codes, str(exc), "x")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [8], (
        f"FB-009 dict-rebind: the int rebind must resolve; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_fb009_dict_rebind_below_500_stays_clean(tmp_path) -> None:
    """FB-009 control: the same rebind to a sub-500 value must NOT flag
    (the stale 5xx dict values are gone after the rebind)."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc):\n"
        '    codes = {"boom": 503}\n'
        "    codes = 404\n"
        '    raise DraftRoomHTTPError(codes, str(exc), "x")\n'
    )
    _assert_clean(tmp_path, source, "FB-009 dict-rebind-404 control")


def test_fb003_suppress_direction_rebind_stays_clean(tmp_path) -> None:
    """TF-003: REPLACE semantics in the suppress direction — a 500 binding
    refined away, then rebound DOWN to 404 before the echo, stays
    unflagged (an order-insensitive union mutant fails this leg)."""
    source = (
        "class DraftRoomHTTPError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def f(exc):\n"
        "    status = 500\n"
        "    if status >= 500:\n"
        '        raise DraftRoomHTTPError(status, "fixed", "x")\n'
        "    status = 404\n"
        '    raise DraftRoomHTTPError(status, str(exc), "mapper")\n'
    )
    _assert_clean(tmp_path, source, "TF-003 suppress-direction rebind")
