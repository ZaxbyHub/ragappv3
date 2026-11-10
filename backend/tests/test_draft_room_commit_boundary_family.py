"""Issue #1068 AC4: the admitted draft_room.py commit-then-audit tails keep
their security-audit row across a cancellation delivered exactly at the
commit boundary, and the per-site census classification is pinned.

Part one — the census (a PRESERVING classification pin, green at base and
green after the fix): the #1068 explorer census at d00bc160 admits 8 of the
10 cited draft_room sites and excludes 2 as no-applied-mutation paths. The
classification is expressed structurally over the module AST:

- the audit-event map binds each admitted event type to its NAMED handler
  (``draft_deleted`` / ``draft_job_cancelled`` / ``draft_job_retried`` /
  ``draft_compile_requested`` (created branch) / ``draft_ready_marked`` /
  ``draft_exported`` / ``draft_promoted`` / ``draft_finding_dismiss``), plus
  the excluded ``draft_promotion_cleanup_failed`` helper;
- every admitted handler has a mutation-bearing store call lexically
  BEFORE its audit tail (the commit-then-audit shape the defect lives in);
- NAP exclusion 1 (compile replay): ``compile_draft`` carries exactly two
  ``draft_compile_requested`` audits — the replay one sits in the ``else``
  arm of ``if created:`` — and every ``created=False`` exit of
  ``_sync_enqueue_compile`` is preceded by a rollback in its branch (no
  applied mutation precedes the replay audit);
- NAP exclusion 2 (promotion cleanup failure): every call of
  ``_record_promotion_cleanup_failure`` sits inside an ``except`` arm (an
  error path, not a commit-then-tail shape).

Part two — per-admitted-site behavioral legs (RED at base, GREEN after the
fix): each leg drives the REAL handler coroutine with a deterministic
commit-boundary schedule. The driven connection is a proxy whose FIRST
transaction end (``conn.commit()`` — every DraftStore /
transaction_context / DraftDeletionService commit closes its transaction
exactly that way) runs the REAL commit and, once it has completed,
schedules the driving task's cancellation onto the event loop from the
worker thread — queued before the ``to_thread`` completion callback can
resume the route's store-call await, so the CancelledError lands exactly
there, after the commit work completed. At base that await is followed by
the audit coroutine creation on the handler, so the row is lost; after the
fix the store call and the audit tail form one drained unit, the
cancellation is absorbed until the row is written, and only then re-raised.
Two admitted sites carry multiple behavioral arms each, and each arm gets
its own leg: ``dispose_draft_finding`` (dismiss + apply + waive — the apply
arm splices a manual revision in ``_sync_apply_finding``'s one ``BEGIN
IMMEDIATE`` transaction, the waive arm commits ``DraftStore.waive_finding``'s
guarded disposition transaction) and ``promote_draft_source`` (revision +
input source — both arms commit the files-row registration on a POOL
connection; each promotion leg also reads the promoted document's ``files``
row back out of the vault after the cancellation, pinning that the
registration the route produced is durable, not just its audit row).

Contract asserted at BOTH ends per site: the commit hook fired
(non-vacuity), the handler task ends cancelled (the #1067 outcome
contract), and the site's security-audit row exists in
``security_audit_log``.
"""

import ast
import asyncio
import os
from types import SimpleNamespace

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["ADMIN_SECRET_TOKEN"] = (
    "test-admin-secret-0123456789abcdef0123456789abcdef0123456789abcdef"
)

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app.api.routes import draft_room as draft_room_mod  # noqa: E402
from app.api.routes.draft_room import (  # noqa: E402
    CompileRequest,
    FindingDispositionRequest,
    PromoteRequest,
    ReadyRequest,
    cancel_draft_job,
    compile_draft,
    delete_draft,
    dispose_draft_finding,
    export_draft_revision,
    mark_revision_ready,
    promote_draft_source,
    retry_draft_job,
)
from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    _pool_cache,
    init_db,
    run_migrations,
)
from app.services.draft_input_storage import DraftInputStorage  # noqa: E402
from app.services.draft_store import DraftStore, sha256_text  # noqa: E402

OWNER_ID = 2
VAULT_ID = 9001

_OWNER = {
    "id": OWNER_ID,
    "username": "owner",
    "full_name": "Owner",
    "role": "member",
    "is_active": True,
    "must_change_password": False,
}

_MANUSCRIPT = "Hello world. This is the manuscript body."


async def _allow_all(user, resource, resource_id, action) -> bool:  # noqa: ANN001
    return True


class _CancelAtCommitConn:
    """Delegating sqlite3 connection proxy that arms the deterministic
    cancellation at the FIRST transaction end on the driven connection.

    The real commit completes FIRST, then the driving route task's
    cancellation is scheduled onto the loop from the worker thread via
    ``loop.call_soon_threadsafe`` — deterministically ahead of the
    to_thread-future completion callback, so the CancelledError is delivered
    exactly at the commit-bearing store-call await, after the commit work
    completed. Only fires once (the security-audit write's own commit must
    not re-arm)."""

    def __init__(self, real: sqlite3.Connection, arm) -> None:  # noqa: ANN001
        self._real = real
        self._arm = arm

    def commit(self) -> None:
        self._real.commit()
        self._arm()

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202, ANN003
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


class _PoolFacade:
    """Pool facade handing the promotion service the hooked connection
    (``db_pool.get_connection()`` is synchronous there); the handler's
    release is swallowed and the real connection is returned to the real
    pool by the world's close."""

    def __init__(self) -> None:
        self.hooked: _CancelAtCommitConn | None = None

    def get_connection(self):  # noqa: ANN202
        return self.hooked

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self.hooked

    def release_connection(self, conn) -> None:  # noqa: ANN001
        try:
            if getattr(conn, "in_transaction", False):
                conn.rollback()
        except sqlite3.Error:
            pass


class _DraftWorld:
    """Per-test world: temp DB (base schema + migrations), real pool, the
    draft-room and provider-origin settings the mutating routes gate on, and
    direct-SQLite ledger seeding in the shape the compile-routes acceptance
    suite uses (no real pipeline run)."""

    def __init__(self, tmp_path: Path, db_name: str) -> None:
        _pool_cache.clear()
        self._saved = (
            settings.data_dir,
            settings.draft_room_enabled,
            settings.ollama_chat_url,
            settings.instant_chat_url,
            settings.draft_allowed_model_origins,
            os.environ.get("ALLOW_LOCAL_SERVICES"),
        )
        settings.data_dir = tmp_path
        settings.draft_room_enabled = True
        # Compile enqueue enforces the provider-origin allowlist before the
        # job is created — point it at a loopback origin so the compile and
        # retry legs get past that gate without a network call.
        settings.ollama_chat_url = "http://127.0.0.1:11434"
        settings.instant_chat_url = "http://127.0.0.1:11434"
        settings.draft_allowed_model_origins = ["http://127.0.0.1:11434"]
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"

        self.db_path = str(tmp_path / db_name)
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        self.real_conn = self.pool.get_connection()
        self.pool_facade = _PoolFacade()
        self.storage = DraftInputStorage(tmp_path / "draft-room")

        seed = self.pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, 'owner', 'abc123', 'member', 1)",
                (OWNER_ID,),
            )
            seed.execute(
                "INSERT INTO vaults (id, name) VALUES (?, 'CensusVault')",
                (VAULT_ID,),
            )
            seed.commit()
        finally:
            self.pool.release_connection(seed)

    # -- seeding helpers (direct SQLite, mirroring the compile-routes suite)

    def _seed_conn(self):  # noqa: ANN202
        return self.pool.get_connection()

    def create_draft(self, title: str = "Boundary Draft") -> int:
        conn = self._seed_conn()
        try:
            draft = DraftStore(conn).create_draft(
                vault_id=VAULT_ID,
                created_by=OWNER_ID,
                title=title,
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            conn.commit()
            return int(draft.id)
        finally:
            self.pool.release_connection(conn)

    def lock_version(self, draft_id: int) -> int:
        return int(
            self.scalar("SELECT lock_version FROM drafts WHERE id = ?", (draft_id,))
        )

    def seed_ready_input(self, draft_id: int) -> None:
        """One manuscript input moved straight to parse_status='ready' (the
        compile gate's "every input must finish parsing" precondition)."""
        conn = self._seed_conn()
        try:
            conn.execute(
                "INSERT INTO draft_inputs (draft_id, role, authority, original_name, "
                "stored_name, extension, media_type, size_bytes, content_sha256, "
                "storage_relpath, parsed_text, parsed_text_sha256, parsed_char_count, "
                "parse_status) VALUES (?, 'manuscript', 'primary', 'manuscript.txt', "
                "'manuscript.txt', '.txt', 'text/plain', ?, ?, ?, ?, ?, ?, 'ready')",
                (
                    draft_id,
                    len(_MANUSCRIPT.encode()),
                    sha256_text(_MANUSCRIPT),
                    f"seed/{draft_id}/manuscript.txt",
                    _MANUSCRIPT,
                    sha256_text(_MANUSCRIPT),
                    len(_MANUSCRIPT),
                ),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def seed_compile_job(
        self, draft_id: int, status: str = "completed", active_stage: str = "assemble"
    ) -> int:
        conn = self._seed_conn()
        try:
            cur = conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, active_stage, max_model_calls, timeout_seconds, "
                "prompt_bundle_version) VALUES (?, ?, ?, 'compile', ?, ?, 40, 1800, 'test')",
                (draft_id, VAULT_ID, OWNER_ID, status, active_stage),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self.pool.release_connection(conn)

    def seed_parse_job(self, draft_id: int) -> int:
        conn = self._seed_conn()
        try:
            cur = conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'parse_input', 'pending', 40, 1800)",
                (draft_id, VAULT_ID, OWNER_ID),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self.pool.release_connection(conn)

    def seed_current_revision(
        self, draft_id: int, job_id: int, content_md: str, fact_status: str = "passed"
    ) -> int:
        conn = self._seed_conn()
        try:
            conn.execute(
                "UPDATE draft_revisions SET is_current = 0 WHERE draft_id = ?",
                (draft_id,),
            )
            next_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM draft_revisions "
                    "WHERE draft_id = ?",
                    (draft_id,),
                ).fetchone()[0]
            )
            cur = conn.execute(
                "INSERT INTO draft_revisions (draft_id, job_id, revision_no, source, "
                "content_md, content_sha256, fact_status, is_current, created_by) "
                "VALUES (?, ?, ?, 'pipeline', ?, ?, ?, 1, ?)",
                (
                    draft_id,
                    job_id,
                    next_no,
                    content_md,
                    sha256_text(content_md),
                    fact_status,
                    OWNER_ID,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self.pool.release_connection(conn)

    def seed_fact_stage(self, job_id: int, candidate_sha256: str) -> None:
        conn = self._seed_conn()
        try:
            conn.execute(
                "INSERT INTO draft_job_stages (job_id, stage, attempt, status, "
                "input_sha256, candidate_sha256) VALUES (?, 'fact', 1, 'completed', 'x', ?)",
                (job_id, candidate_sha256),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def set_draft_status(self, draft_id: int, status: str) -> None:
        conn = self._seed_conn()
        try:
            conn.execute(
                "UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id)
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def seed_ready_eligible_draft(
        self, content_md: str = "# Body\n\nSome article text."
    ) -> tuple:
        """A draft with exactly one clean, Fact-current, needs_review
        revision — the minimum state the Ready route accepts."""
        draft_id = self.create_draft()
        self.seed_ready_input(draft_id)
        job_id = self.seed_compile_job(draft_id)
        revision_id = self.seed_current_revision(draft_id, job_id, content_md)
        self.seed_fact_stage(job_id, sha256_text(content_md))
        self.set_draft_status(draft_id, "needs_review")
        return draft_id, job_id, revision_id

    def seed_finding(self, draft_id: int, job_id: int, revision_id: int) -> int:
        conn = self._seed_conn()
        try:
            return int(
                DraftStore(conn).insert_finding(
                    draft_id=draft_id,
                    stage="standards",
                    rule_id="rule-x",
                    rule_version="1",
                    category="style",
                    severity="warning",
                    message="needs a fix",
                    revision_id=revision_id,
                    job_id=job_id,
                    waivable=True,
                )
            )
        finally:
            self.pool.release_connection(conn)

    def seed_applicable_finding(
        self, draft_id: int, job_id: int, revision_id: int, content_md: str
    ) -> int:
        """A finding carrying a valid APPLY span (suggestion + bounds over the
        current revision's exact content, span hash stored by
        ``insert_finding``) — the minimum state the apply arm of the
        disposition route accepts."""
        conn = self._seed_conn()
        try:
            span_text = "Some article text"
            start = content_md.index(span_text)
            return int(
                DraftStore(conn).insert_finding(
                    draft_id=draft_id,
                    stage="standards",
                    rule_id="rule-apply",
                    rule_version="1",
                    category="style",
                    severity="warning",
                    message="needs a rewrite",
                    revision_id=revision_id,
                    job_id=job_id,
                    waivable=True,
                    original_text=span_text,
                    suggestion="Some rewritten article text.",
                    span_start=start,
                    span_end=start + len(span_text),
                )
            )
        finally:
            self.pool.release_connection(conn)

    def seed_input_bytes(self, draft_id: int) -> None:
        """Write the seeded manuscript input's bytes at its storage_relpath —
        ``_copy_input_bytes`` copies from there during an input promotion."""
        relpath = str(
            self.scalar(
                "SELECT storage_relpath FROM draft_inputs WHERE draft_id = ?",
                (draft_id,),
            )
        )
        dest = self.storage.resolve(relpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_MANUSCRIPT.encode())

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def audit_rows(self, event_type: str) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
                (event_type,),
            )
        )

    def close(self) -> None:
        (
            data_dir,
            draft_room_enabled,
            ollama_chat_url,
            instant_chat_url,
            origins,
            allow_local,
        ) = self._saved
        settings.data_dir = data_dir
        settings.draft_room_enabled = draft_room_enabled
        settings.ollama_chat_url = ollama_chat_url
        settings.instant_chat_url = instant_chat_url
        settings.draft_allowed_model_origins = origins
        if allow_local is None:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        else:
            os.environ["ALLOW_LOCAL_SERVICES"] = allow_local
        try:
            if self.real_conn.in_transaction:
                self.real_conn.rollback()
        except sqlite3.Error:
            pass
        try:
            self.pool.release_connection(self.real_conn)
        except Exception:  # noqa: BLE001
            pass
        self.pool.close_all()
        _pool_cache.clear()


def _request_for(method: str, path: str) -> Request:
    # Review PRR-002/F-01b: the compile/retry routes read the Draft Room
    # worker from app.state (via get_draft_job_processor's scope-safe read),
    # so the harness scope must carry an app whose state mounts a live
    # processor — these legs exercise cancel-at-commit audit ordering,
    # not the worker gate.
    scope_app = SimpleNamespace(state=SimpleNamespace(draft_job_processor=object()))
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [],
            "path": path,
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": scope_app,
        }
    )


def _drive(world: _DraftWorld, make_call) -> tuple[str, dict]:  # noqa: ANN001
    """Deterministic commit-boundary schedule: the first transaction end on
    the world's driven connection arms the driving task's cancellation."""

    async def drive() -> str:
        loop = asyncio.get_running_loop()
        state = {"fired": False}
        holder: dict = {"task": None}

        def _arm() -> None:
            if not state["fired"]:
                state["fired"] = True
                loop.call_soon_threadsafe(holder["task"].cancel)

        conn = _CancelAtCommitConn(world.real_conn, _arm)
        world.pool_facade.hooked = conn
        holder["task"] = asyncio.ensure_future(make_call(conn))
        try:
            await holder["task"]
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"
        finally:
            state_holder["state"] = state

    state_holder: dict = {}
    outcome = asyncio.run(drive())
    return outcome, state_holder["state"]


# ── part one: the census classification pin (PRESERVING — green at base) ────


_ADMITTED_HANDLER_EVENTS = {
    "delete_draft": {"draft_deleted"},
    "cancel_draft_job": {"draft_job_cancelled"},
    "retry_draft_job": {"draft_job_retried"},
    "compile_draft": {"draft_compile_requested"},
    "mark_revision_ready": {"draft_ready_marked"},
    "export_draft_revision": {"draft_exported"},
    "promote_draft_source": {"draft_promoted"},
}
_NAP_EVENTS = {"draft_promotion_cleanup_failed"}
_MUTATION_CALL_NAMES = {"_run_store", "_run_promotion"}


def _audit_event_calls(tree: ast.AST):
    """Yield (enclosing_function_name, event_type, node) for every
    ``event_type="..."`` keyword call nested under an audit-record call."""
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "event_type" and isinstance(kw.value, ast.Constant):
                    yield fn.name, kw.value.value, node


def test_draft_room_audit_census_classification() -> None:
    """Census pin (green at base, must stay green after the fix): the 8
    admitted sites carry the commit-then-audit shape; the replay branch and
    the promotion-cleanup-failure helper are structurally no-applied-mutation
    / error paths."""
    source = Path(draft_room_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    found: dict[str, set] = {}
    for fn_name, event, _node in _audit_event_calls(tree):
        found.setdefault(fn_name, set()).add(event)

    expected = dict(_ADMITTED_HANDLER_EVENTS)
    expected["_record_promotion_cleanup_failure"] = set(_NAP_EVENTS)
    for handler, events in expected.items():
        assert found.get(handler, set()) >= events, (
            f"census drift: handler {handler} must carry audit event(s) "
            f"{sorted(events)}; found {sorted(found.get(handler, set()))}"
        )

    # Admitted shape: a mutation-bearing store call lexically precedes the
    # audit tail inside each admitted handler.
    for fn in ast.walk(tree):
        if (
            not isinstance(fn, ast.AsyncFunctionDef)
            or fn.name not in _ADMITTED_HANDLER_EVENTS
        ):
            continue
        audit_nodes = [
            node for name, _event, node in _audit_event_calls(tree) if name == fn.name
        ]
        assert audit_nodes, f"census drift: no audit call left in {fn.name}"
        mutation_lines = [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id in _MUTATION_CALL_NAMES
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "to_thread"
                )
            )
        ]
        for audit in audit_nodes:
            assert any(line < audit.lineno for line in mutation_lines), (
                f"census drift: {fn.name}'s audit tail is not preceded by a "
                "mutation-bearing store call (the commit-then-audit shape "
                "this family pins)"
            )

    # NAP exclusion 1 — compile replay: exactly two draft_compile_requested
    # audits in compile_draft, one inside `if created:` and one inside its
    # else arm; and every created=False exit of _sync_enqueue_compile is
    # preceded by a rollback in its own branch (no applied mutation).
    compile_audits = [
        node
        for name, event, node in _audit_event_calls(tree)
        if name == "compile_draft" and event == "draft_compile_requested"
    ]
    assert len(compile_audits) == 2, (
        f"census drift: compile_draft must carry exactly the created-branch "
        f"and replay-branch audits (found {len(compile_audits)})"
    )

    created_ifs = [
        node
        for node in ast.walk(_fn_by_name(tree, "compile_draft"))
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "created"
    ]
    assert len(created_ifs) == 1, (
        "census drift: compile_draft must gate its two audits on `if created:`"
    )
    gate = created_ifs[0]
    in_if = [
        n for n in compile_audits if any(n in ast.walk(stmt) for stmt in gate.body)
    ]
    in_else = [
        n for n in compile_audits if any(n in ast.walk(stmt) for stmt in gate.orelse)
    ]
    assert len(in_if) == 1 and len(in_else) == 1, (
        "census drift: the created-branch audit must sit in the if-body and "
        f"the replay audit in the else arm (if-body {len(in_if)}, else {len(in_else)})"
    )

    enqueue = _fn_by_name(tree, "_sync_enqueue_compile")
    assert enqueue is not None, "census drift: _sync_enqueue_compile not found"
    false_returns = [
        node
        for node in ast.walk(enqueue)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Tuple)
        and any(
            isinstance(elt, ast.Constant) and elt.value is False
            for elt in node.value.elts
        )
    ]
    assert false_returns, (
        "census drift: _sync_enqueue_compile must expose created=False exits"
    )

    def _parents(fn: ast.AST) -> dict:
        out: dict = {}
        for parent in ast.walk(fn):
            for child in ast.iter_child_nodes(parent):
                out[child] = parent
        return out

    def _if_chain(fn: ast.AST, parents: dict, node: ast.AST) -> set:
        """Ids of every enclosing If INSIDE fn, from nearest outward (empty
        set = function-body level)."""
        chain: set = set()
        cur = parents.get(node)
        while cur is not None and cur is not fn:
            if isinstance(cur, ast.If):
                chain.add(id(cur))
            cur = parents.get(cur)
        return chain

    def _branch_has_rollback(fn: ast.AST, parents: dict, ret: ast.Return) -> bool:
        """Dominance-aware containment (PR #1077 review PRR-005c): the
        rollback must DOMINATE the return — its enclosing If must sit on the
        return's own If-chain (the same branch or a protecting outer branch
        of it), textually before it. A rollback in a SIBLING branch (the
        pre-PRR-005c any-preceding scan's hole) satisfies nothing."""
        ret_chain = _if_chain(fn, parents, ret)
        for node in ast.walk(fn):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rollback"
            ):
                continue
            if node.lineno > ret.lineno:
                continue
            rollback_if = _if_chain(fn, parents, node)
            if rollback_if <= ret_chain:
                return True
        return False

    enqueue_parents = _parents(enqueue)
    for ret in false_returns:
        assert _branch_has_rollback(enqueue, enqueue_parents, ret), (
            "census drift: a created=False exit of _sync_enqueue_compile no "
            "longer has a rollback in its OWN branch — the replay branch's "
            "no-applied-mutation classification would be stale (PR #1077 "
            "review PRR-005c: an unrelated earlier branch's rollback must "
            "not satisfy this exit)"
        )
    # The dispose site's event type is computed (f"draft_finding_{action}"),
    # so the constant-keyword census above cannot see it: pin its admitted
    # membership structurally — dispose_draft_finding carries an audit call
    # whose event_type expression references 'draft_finding_'.
    dispose_fn = _fn_by_name(tree, "dispose_draft_finding")
    assert dispose_fn is not None, "census drift: dispose_draft_finding not found"
    dispose_audit = any(
        isinstance(node, ast.Call)
        and any(kw.arg == "event_type" for kw in node.keywords)
        and "draft_finding_" in ast.unparse(node)
        for node in ast.walk(dispose_fn)
    )
    assert dispose_audit, (
        "census drift: dispose_draft_finding must carry the "
        "draft_finding_{action} audit tail"
    )

    # NAP exclusion 2 — promotion cleanup failure: every call site of
    # _record_promotion_cleanup_failure sits inside an except arm.
    except_arm_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    except_arm_calls.add(sub.lineno)
    cleanup_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_record_promotion_cleanup_failure"
    ]
    assert cleanup_calls, (
        "census drift: _record_promotion_cleanup_failure has no call sites"
    )
    assert all(node.lineno in except_arm_calls for node in cleanup_calls), (
        "census drift: a _record_promotion_cleanup_failure call escaped the "
        "except arms — the error-path (NAP) classification would be stale"
    )


def _fn_by_name(tree: ast.AST, name: str):  # noqa: ANN202
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    return None


# ── part two: per-admitted-site cancellation-at-commit-boundary legs ────────
# (RED at base, GREEN after the fix)


# named-mutation: unwrap the drained unit at the delete_draft site (await the
# deletion to_thread bare, leaving the draft_deleted drain_audit outside any
# unit) -> test_delete_draft_cancel_at_commit_still_audits regresses.


def test_delete_draft_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The whole-draft purge commits inside
    the deletion service; a cancellation landing at that commit await must
    not lose the ``draft_deleted`` row."""
    world = _DraftWorld(tmp_path, "draft-delete.db")
    draft_id = world.create_draft()
    try:
        outcome, state = _drive(
            world,
            lambda conn: delete_draft(
                draft_id,
                _request_for("DELETE", f"/api/draft-room/drafts/{draft_id}"),
                db=conn,
                user=dict(_OWNER),
                storage=world.storage,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM drafts WHERE id = ?", (draft_id,)) == 0
        ), "precondition: the deletion commit completed (draft row gone)"
        assert world.audit_rows("draft_deleted") == 1, (
            "commit-boundary audit loss: the draft purge was applied (the "
            "commit completed) but its draft_deleted security-audit row was "
            "never recorded"
        )
    finally:
        world.close()


def test_cancel_draft_job_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. ``request_job_cancel`` commits the
    job cancellation; a cancellation landing at that commit await must not
    lose the ``draft_job_cancelled`` row."""
    world = _DraftWorld(tmp_path, "draft-cancel-job.db")
    draft_id = world.create_draft()
    job_id = world.seed_parse_job(draft_id)
    try:
        outcome, state = _drive(
            world,
            lambda conn: cancel_draft_job(
                _request_for(
                    "POST", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel"
                ),
                draft_id,
                job_id,
                db=conn,
                user=dict(_OWNER),
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT status FROM draft_jobs WHERE id = ?", (job_id,))
            == "cancelled"
        ), "precondition: the job-cancel commit completed"
        assert world.audit_rows("draft_job_cancelled") == 1, (
            "commit-boundary audit loss: the job cancellation was applied "
            "(the commit completed) but its draft_job_cancelled security-audit "
            "row was never recorded"
        )
    finally:
        world.close()


def test_retry_draft_job_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. ``_sync_enqueue_compile`` commits the
    retry's child compile job; a cancellation landing at that commit await
    must not lose the gated ``draft_job_retried`` row."""
    world = _DraftWorld(tmp_path, "draft-retry.db")
    draft_id = world.create_draft()
    world.seed_ready_input(draft_id)
    job_id = world.seed_compile_job(draft_id, status="failed")
    world.set_draft_status(draft_id, "failed")
    try:
        outcome, state = _drive(
            world,
            lambda conn: retry_draft_job(
                _request_for(
                    "POST", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry"
                ),
                draft_id,
                job_id,
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? AND job_type = 'compile'",
                (draft_id,),
            )
            == 2
        ), "precondition: the retry's child compile job commit completed"
        assert world.audit_rows("draft_job_retried") == 1, (
            "commit-boundary audit loss: the retry's child compile job was "
            "enqueued (the commit completed) but its draft_job_retried "
            "security-audit row was never recorded"
        )
    finally:
        world.close()


def test_compile_draft_created_branch_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. ``_sync_enqueue_compile`` commits the
    new compile job (the created, NON-replay branch); a cancellation landing
    at that commit await must not lose the ``draft_compile_requested`` row
    with ``idempotent_replay`` false."""
    world = _DraftWorld(tmp_path, "draft-compile.db")
    draft_id = world.create_draft()
    world.seed_ready_input(draft_id)
    lock_version = world.lock_version(draft_id)
    try:
        outcome, state = _drive(
            world,
            lambda conn: compile_draft(
                _request_for("POST", f"/api/draft-room/drafts/{draft_id}/compile"),
                draft_id,
                CompileRequest(
                    base_revision_id=None,
                    lock_version=lock_version,
                    start_stage="research",
                ),
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? AND job_type = 'compile'",
                (draft_id,),
            )
            == 1
        ), "precondition: the compile enqueue commit completed"
        assert world.audit_rows("draft_compile_requested") == 1, (
            "commit-boundary audit loss: the compile job was enqueued (the "
            "commit completed, non-replay created branch) but its "
            "draft_compile_requested security-audit row was never recorded"
        )
        replay_flags = world.scalar(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = "
            "'draft_compile_requested' AND metadata_json LIKE '%\"idempotent_replay\":false%'"
        )
        assert replay_flags == 1, (
            "the recorded row must be the created-branch (non-replay) audit"
        )
    finally:
        world.close()


def test_mark_revision_ready_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. ``_sync_mark_ready`` commits the
    human approval; a cancellation landing at that commit await must not lose
    the ``draft_ready_marked`` row."""
    world = _DraftWorld(tmp_path, "draft-ready.db")
    draft_id, _job_id, revision_id = world.seed_ready_eligible_draft()
    lock_version = world.lock_version(draft_id)
    try:
        outcome, state = _drive(
            world,
            lambda conn: mark_revision_ready(
                _request_for(
                    "POST",
                    f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/ready",
                ),
                draft_id,
                revision_id,
                ReadyRequest(lock_version=lock_version),
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT status FROM drafts WHERE id = ?", (draft_id,))
            == "ready"
        ), "precondition: the Ready commit completed"
        assert world.audit_rows("draft_ready_marked") == 1, (
            "commit-boundary audit loss: the revision was marked ready (the "
            "commit completed) but its draft_ready_marked security-audit row "
            "was never recorded"
        )
    finally:
        world.close()


def test_export_draft_revision_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. ``record_event("exported")`` commits
    the export event; a cancellation landing at that commit await must not
    lose the ``draft_exported`` row."""
    world = _DraftWorld(tmp_path, "draft-export.db")
    draft_id, job_id, revision_id = world.seed_ready_eligible_draft()
    try:
        outcome, state = _drive(
            world,
            lambda conn: export_draft_revision(
                _request_for(
                    "POST",
                    f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export",
                ),
                draft_id,
                revision_id,
                format="md",
                acknowledge_not_fact_checked=False,
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM draft_events WHERE draft_id = ? AND event_type = 'exported'",
                (draft_id,),
            )
            == 1
        ), "precondition: the export-event commit completed"
        assert world.audit_rows("draft_exported") == 1, (
            "commit-boundary audit loss: the export event was recorded (the "
            "commit completed) but its draft_exported security-audit row was "
            "never recorded"
        )
    finally:
        world.close()


def test_promote_draft_source_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The promotion service commits the
    files-row registration on a POOL connection (the REVISION arm,
    ``_run_promotion(promote_revision(...))``); a cancellation landing at
    that commit await must not lose the ``draft_promoted`` row, and the
    promoted document must be durably registered in the vault."""
    world = _DraftWorld(tmp_path, "draft-promote.db")
    draft_id = world.create_draft()
    job_id = world.seed_compile_job(draft_id)
    revision_id = world.seed_current_revision(draft_id, job_id, "# Promoted\n\nbody")
    processor = MagicMock()
    processor.enqueue = AsyncMock(return_value=None)
    try:
        outcome, state = _drive(
            world,
            lambda conn: promote_draft_source(
                _request_for("POST", f"/api/draft-room/drafts/{draft_id}/promote"),
                draft_id,
                PromoteRequest(
                    source_type="revision", source_id=revision_id, title="Promoted Doc"
                ),
                db=conn,
                db_pool=world.pool_facade,
                background_processor=processor,
                user=dict(_OWNER),
                evaluate=_allow_all,
                storage=world.storage,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM files WHERE vault_id = ?", (VAULT_ID,))
            == 1
        ), (
            "precondition: the promotion's files-row registration commit "
            "completed — the promoted document is durably registered in the "
            "vault read back after the cancellation"
        )
        assert world.audit_rows("draft_promoted") == 1, (
            "commit-boundary audit loss: the promotion's registration commit "
            "completed but its draft_promoted security-audit row was never "
            "recorded (the cancellation unwound the handler at the "
            "_run_promotion await, before the audit coroutine existed)"
        )
    finally:
        world.close()


def test_promote_draft_source_input_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. The promote route's INPUT-source arm
    (``_run_promotion(promote_input(...))``) commits the same files-row
    registration on a POOL connection; a cancellation landing at that commit
    await must not lose the ``draft_promoted`` row, and the promoted document
    must be durably registered in the vault."""
    world = _DraftWorld(tmp_path, "draft-promote-input.db")
    draft_id = world.create_draft()
    world.seed_ready_input(draft_id)
    world.seed_input_bytes(draft_id)
    input_id = int(
        world.scalar("SELECT id FROM draft_inputs WHERE draft_id = ?", (draft_id,))
    )
    processor = MagicMock()
    processor.enqueue = AsyncMock(return_value=None)
    try:
        outcome, state = _drive(
            world,
            lambda conn: promote_draft_source(
                _request_for("POST", f"/api/draft-room/drafts/{draft_id}/promote"),
                draft_id,
                PromoteRequest(
                    source_type="input", source_id=input_id, title="Promoted Input"
                ),
                db=conn,
                db_pool=world.pool_facade,
                background_processor=processor,
                user=dict(_OWNER),
                evaluate=_allow_all,
                storage=world.storage,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM files WHERE vault_id = ?", (VAULT_ID,))
            == 1
        ), (
            "precondition: the input promotion's files-row registration commit "
            "completed — the promoted document is durably registered in the "
            "vault read back after the cancellation"
        )
        assert world.audit_rows("draft_promoted") == 1, (
            "commit-boundary audit loss: the input-source promotion's "
            "registration commit completed but its draft_promoted "
            "security-audit row was never recorded (the cancellation unwound "
            "the handler at the _run_promotion await, before the audit "
            "coroutine existed)"
        )
    finally:
        world.close()


def test_dispose_draft_finding_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. ``dismiss_finding`` commits the
    disposition; a cancellation landing at that commit await must not lose
    the ``draft_finding_dismiss`` row."""
    world = _DraftWorld(tmp_path, "draft-finding.db")
    draft_id, job_id, revision_id = world.seed_ready_eligible_draft()
    finding_id = world.seed_finding(draft_id, job_id, revision_id)
    lock_version = world.lock_version(draft_id)
    try:
        outcome, state = _drive(
            world,
            lambda conn: dispose_draft_finding(
                _request_for(
                    "POST",
                    f"/api/draft-room/drafts/{draft_id}/findings/{finding_id}/disposition",
                ),
                Response(),
                draft_id,
                finding_id,
                FindingDispositionRequest(
                    action="dismiss",
                    base_revision_id=revision_id,
                    lock_version=lock_version,
                ),
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT status FROM draft_findings WHERE id = ?", (finding_id,)
            )
            == "dismissed"
        ), "precondition: the disposition commit completed"
        assert world.audit_rows("draft_finding_dismiss") == 1, (
            "commit-boundary audit loss: the finding disposition was applied "
            "(the commit completed) but its draft_finding_dismiss "
            "security-audit row was never recorded"
        )
    finally:
        world.close()


def test_dispose_draft_finding_apply_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. The apply arm's ``_sync_apply_finding``
    commits the suggestion splice (new manual revision + finding ``applied`` +
    Ready invalidation) in one ``BEGIN IMMEDIATE`` transaction; a cancellation
    landing at that commit await must not lose the ``draft_finding_apply``
    row."""
    world = _DraftWorld(tmp_path, "draft-finding-apply.db")
    draft_id, job_id, revision_id = world.seed_ready_eligible_draft()
    finding_id = world.seed_applicable_finding(
        draft_id, job_id, revision_id, "# Body\n\nSome article text."
    )
    lock_version = world.lock_version(draft_id)
    try:
        outcome, state = _drive(
            world,
            lambda conn: dispose_draft_finding(
                _request_for(
                    "POST",
                    f"/api/draft-room/drafts/{draft_id}/findings/{finding_id}/disposition",
                ),
                Response(),
                draft_id,
                finding_id,
                FindingDispositionRequest(
                    action="apply",
                    base_revision_id=revision_id,
                    lock_version=lock_version,
                ),
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT status FROM draft_findings WHERE id = ?", (finding_id,)
            )
            == "applied"
        ), "precondition: the apply transaction committed (finding applied)"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ? "
                "AND source = 'manual'",
                (draft_id,),
            )
            == 1
        ), "precondition: the apply transaction committed (spliced manual revision)"
        assert world.audit_rows("draft_finding_apply") == 1, (
            "commit-boundary audit loss: the finding apply was applied (the "
            "commit completed) but its draft_finding_apply security-audit row "
            "was never recorded"
        )
    finally:
        world.close()


def test_dispose_draft_finding_waive_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. The waive arm's
    ``DraftStore.waive_finding`` commits the waiver (status ``waived`` +
    resolution note + waiver pin + lock bump) in one guarded transaction; a
    cancellation landing at that commit await must not lose the
    ``draft_finding_waive`` row."""
    world = _DraftWorld(tmp_path, "draft-finding-waive.db")
    draft_id, job_id, revision_id = world.seed_ready_eligible_draft()
    finding_id = world.seed_finding(draft_id, job_id, revision_id)
    lock_version = world.lock_version(draft_id)
    try:
        outcome, state = _drive(
            world,
            lambda conn: dispose_draft_finding(
                _request_for(
                    "POST",
                    f"/api/draft-room/drafts/{draft_id}/findings/{finding_id}/disposition",
                ),
                Response(),
                draft_id,
                finding_id,
                FindingDispositionRequest(
                    action="waive",
                    base_revision_id=revision_id,
                    lock_version=lock_version,
                    note="accepted risk for this release",
                ),
                db=conn,
                user=dict(_OWNER),
                evaluate=_allow_all,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT status FROM draft_findings WHERE id = ?", (finding_id,)
            )
            == "waived"
        ), "precondition: the waiver commit completed (finding waived)"
        assert world.audit_rows("draft_finding_waive") == 1, (
            "commit-boundary audit loss: the finding waiver was applied (the "
            "commit completed) but its draft_finding_waive security-audit row "
            "was never recorded"
        )
    finally:
        world.close()
