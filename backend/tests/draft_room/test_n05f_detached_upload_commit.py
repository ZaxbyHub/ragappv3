"""Arm's-length acceptance check C21 (NEW-SURFACE) for the
draft-capability-liveness trace (issues #902/#905/#911/#913), detached
upload-commit side.

Frozen specification. NEW-SURFACE: the revised plan's symbol contract is
normative — ``app.api.routes.draft_room`` exposes module-level
``_UPLOAD_COMMIT_DRAIN_SECONDS: float`` and ``_INFLIGHT_UPLOAD_COMMITS:
Set[asyncio.Task]``. The module-level import below is deliberately
UNGUARDED: at base the module does not exist there yet, so the base leg's
ImportError/collection error IS the expected NEW-SURFACE base signature
(RED-by-construction at base, GREEN at head once the fix lands).

Design under test (per the plan, endorsed by review): the upload route runs
finalize -> enqueue -> compensation inside ONE detached task that acquires
its own pooled connection LAZILY at the DB step (not before the blocked
filesystem finalize — the check RELIES on this and does not force or
require an early checkout); the request awaits it via ``asyncio.shield``
inside ``anyio.CancelScope(shield=True)`` and, on cancellation, drains it
under a monotonic deadline of ``_UPLOAD_COMMIT_DRAIN_SECONDS`` anchored at
the FIRST cancellation.

Legs (all drive the REAL upload route through ``httpx.ASGITransport`` on a
private loop, parking a route step on threading events — event-driven, no
polling sleeps):

* (a) deadline anchor — with the finalize step blocked and
  ``_UPLOAD_COMMIT_DRAIN_SECONDS`` patched to a small value D, the FIRST
  native cancellation starts the drain clock; a SECOND cancellation is
  delivered at ~0.67*D while the request is still pending (asserted), and
  the request must exit within D + a margin strictly below 1.67*D — a
  MONOTONIC deadline exits at ~D, a RESTARTING one at ~1.67*D. The blocked
  op is released only after the exit, so only the deadline is measured.
  The cancellation must still propagate.
* (b) ownership generation — the POOL is instrumented (checkout/release
  events with a generation counter and monotonic timestamps). With the
  request exited and its ``get_db`` dependency connection released, the
  blocked enqueue is released and the upload settles COMMITTED (row +
  parse job + final bytes). Oracle (CHECK_WRONG amendment, critic
  approved — the previous opened-after-release clause is unsatisfiable
  when the parked step IS the DB step, since any implementation must
  check out before entering the parked store call): every enqueue DB
  operation runs inside an OWNED checkout interval open at the time
  (no-borrow: never on a connection nobody owns, e.g. the request's
  returned connection without a fresh checkout); the covering checkout is
  never the request dependency's own checkout generation (a fresh
  checkout of a recycled sqlite3 object IS valid); and every checkout is
  RELEASED once the outcome settles (balanced generations). The same
  oracle covers the compensation path's row-delete in leg (c).
* (c) late failure + sanitized log — the enqueue raises only after the
  request exited and its dependency teardown ran; the outcome must be
  COMPENSATED (row gone, final bytes gone); the delete-row compensation DB
  work obeys the same ownership-generation oracle; and EXACTLY ONE ERROR
  log record carries the draft id AND the input id AND the exception
  class, while never carrying a storage path or any uploaded content.
* (d) strong reference — during the in-flight window the commit task is a
  member of ``_INFLIGHT_UPLOAD_COMMITS``, and it is removed after
  completion.
"""

import asyncio
import contextlib
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from unittest.mock import patch

import httpx

import app.api.routes.draft_room as draft_room_module
from app.api.deps import get_db

# NEW-SURFACE contract, deliberately unguarded (see module docstring).
from app.api.routes.draft_room import (  # noqa: F401
    _INFLIGHT_UPLOAD_COMMITS,
    _UPLOAD_COMMIT_DRAIN_SECONDS,
)
from app.main import app
from app.services.draft_store import DraftStore
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase

_UPLOAD_BYTES = b"N05F-SECRET-CONTENT bytes for the detached commit upload."
_REAL_ENQUEUE = DraftStore.enqueue_parse_job
_REAL_DELETE_INPUT_ROW = DraftStore.delete_input_row


@contextlib.contextmanager
def _noop():
    yield None


class _PoolGenerationRecorder:
    """Instance-level wrapper around the shared connection pool: records
    every checkout (with a generation counter and a monotonic open
    timestamp) and every release (close timestamp), regardless of which
    code path performs it (``get_db`` overrides, ``pool.connection()``
    context managers, app.state consumers)."""

    def __init__(self, pool):
        self._pool = pool
        self._lock = threading.Lock()
        self.checkouts: list[dict] = []
        self._orig_get = None
        self._orig_release = None

    def install(self):
        self._orig_get = self._pool.get_connection
        self._orig_release = self._pool.release_connection

        def get_connection():
            conn = self._orig_get()
            with self._lock:
                self.checkouts.append(
                    {
                        "gen": len(self.checkouts) + 1,
                        "conn": conn,
                        "opened_at": time.monotonic(),
                        "closed_at": None,
                    }
                )
            return conn

        def release_connection(conn):
            with self._lock:
                for record in reversed(self.checkouts):
                    if record["conn"] is conn and record["closed_at"] is None:
                        record["closed_at"] = time.monotonic()
                        break
            return self._orig_release(conn)

        self._pool.get_connection = get_connection
        self._pool.release_connection = release_connection

    def restore(self):
        if self._orig_get is not None:
            self._pool.get_connection = self._orig_get
            self._pool.release_connection = self._orig_release

    def covering_interval(self, conn, at):
        """The checkout record whose [opened_at, closed_at] interval covers
        ``at`` on THIS connection object, or None."""
        with self._lock:
            for record in self.checkouts:
                if (
                    record["conn"] is conn
                    and record["opened_at"] <= at
                    and (record["closed_at"] is None or at <= record["closed_at"])
                ):
                    return record
        return None


def _parking_enqueue_patch(entered, release, finished, after_release, ops, ids):
    """Park ``DraftStore.enqueue_parse_job`` until released, then behave per
    ``after_release``: ``"real"`` performs the REAL enqueue, ``"raise"``
    fails the op once finally attempted. Records each execution as
    ``(connection, monotonic timestamp)`` in ``ops`` and any ``input_id``
    kwarg in ``ids``."""

    def _blocked(store_self, **kwargs):
        entered.set()
        if not release.wait(timeout=300):
            raise AssertionError("the blocked enqueue step was never released")
        finished.set()
        if "input_id" in kwargs:
            ids["input_id"] = kwargs["input_id"]
        ops.append((getattr(store_self, "_db", None), time.monotonic()))
        if after_release == "raise":
            raise RuntimeError("simulated late enqueue failure")
        return _REAL_ENQUEUE(store_self, **kwargs)

    return patch.object(DraftStore, "enqueue_parse_job", _blocked)


def _parking_delete_input_row_patch(ops):
    """Record every ``DraftStore.delete_input_row`` execution as
    ``(connection, monotonic timestamp)`` around the real call (the
    compensation path's row rollback)."""

    def _recording(store_self, **kwargs):
        ops.append((getattr(store_self, "_db", None), time.monotonic()))
        return _REAL_DELETE_INPUT_ROW(store_self, **kwargs)

    return patch.object(DraftStore, "delete_input_row", _recording)


def _parking_finalize_patch(entered, release, finished):
    """Park ``DraftInputStorage.finalize`` until released (then the REAL
    staged->final move runs)."""
    from app.services.draft_input_storage import DraftInputStorage

    real_finalize = DraftInputStorage.finalize

    def _blocked(storage_self, staged, relpath):
        entered.set()
        if not release.wait(timeout=300):
            raise AssertionError("the blocked finalize step was never released")
        result = real_finalize(storage_self, staged, relpath)
        finished.set()
        return result

    return patch.object(DraftInputStorage, "finalize", _blocked)


async def _settle_request_task(task):
    try:
        response = await task
        return {"propagated": False, "status": response.status_code, "error": None}
    except asyncio.CancelledError:
        return {"propagated": True, "status": None, "error": None}
    except BaseException as exc:  # reported, never swallowed
        return {"propagated": False, "status": None, "error": repr(exc)}


async def _drain_pending_tasks():
    for _ in range(100):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=60)
    raise AssertionError("loop tasks never drained after the cancellation")


def _request(draft_id, headers):
    request_headers = dict(headers)
    request_headers["user-agent"] = ""
    return dict(
        url=f"/api/draft-room/drafts/{draft_id}/inputs",
        data={"role": "manuscript", "authority": "primary"},
        files={"file": ("notes.txt", _UPLOAD_BYTES, "text/plain")},
        headers=request_headers,
    )


class TestN05FDetachedUploadCommit(CompileRouteTestBase):
    """C21 (NEW-SURFACE)."""

    def _install_recording_db(self, recorder):
        """``get_db`` override that captures WHICH checkout generation is
        the request dependency's own (the recorder's latest checkout at
        override time) and fires the teardown gate AFTER the connection
        really returned to the pool."""
        pool = self._connection_pool
        returned = threading.Event()
        state: dict = {"request_gen": None}
        previous = app.dependency_overrides.get(get_db)

        def recording_get_db():
            conn = pool.get_connection()
            # This override's get_connection just opened the recorder's
            # latest checkout — that generation IS the request dependency's
            # checkout.
            with recorder._lock:
                state["request_gen"] = (
                    recorder.checkouts[-1]["gen"] if recorder.checkouts else None
                )
            try:
                yield conn
            finally:
                pool.release_connection(conn)
                returned.set()

        app.dependency_overrides[get_db] = recording_get_db

        def restore():
            if previous is not None:
                app.dependency_overrides[get_db] = previous
            else:
                app.dependency_overrides.pop(get_db, None)

        return returned, state, restore

    def _row(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT id FROM draft_inputs WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)

    def _parse_job_count(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'parse_input'",
                (draft_id,),
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _final_files(self, draft_id):
        from pathlib import Path

        from app.config import settings

        project_dir = (
            Path(settings.data_dir) / "draft-room" / str(self.OWNER_ID) / str(draft_id)
        )
        if not project_dir.is_dir():
            return []
        return sorted(str(p) for p in project_dir.rglob("*") if p.is_file())

    def _assert_detached_ownership(
        self, recorder, ops, request_gen, label, *, require_detached=True
    ):
        """Ownership oracle (CHECK_WRONG amendment, critic-approved): the
        strict opened-after-release ordering of the previous revision is
        unsatisfiable when the parked step IS the DB step — any
        implementation must check out before entering the parked store
        call, and the park ends only after the request exits. The
        shape-independent properties are instead: every recorded DB
        operation ran inside an OWNED checkout interval open at the time
        (no-borrow — never on a connection nobody owns, e.g. the request's
        returned connection without a fresh checkout); when
        ``require_detached`` (the commit path's enqueue work), the covering
        checkout is additionally never the request dependency's own
        checkout generation (not the request's connection scope — a fresh
        checkout of a recycled sqlite3 object IS valid); and every checkout
        is released once the outcome settles (balanced generations).
        ``require_detached=False`` is used for the compensation path's
        row-delete: a request-level unwind that rolls the reserved row back
        on the request's OWN still-open connection is legitimate cleanup,
        and the no-borrow property alone is the correct bound there (the
        compensated outcome itself is asserted separately)."""
        self.assertTrue(
            ops,
            f"the {label} DB work must have run once the blocked op was released",
        )
        for conn, at in ops:
            interval = recorder.covering_interval(conn, at)
            self.assertIsNotNone(
                interval,
                f"the {label} DB work ran with NO open checkout interval — "
                "it must not run on a connection nobody owns (e.g. the "
                "request's returned connection)",
            )
            if require_detached:
                self.assertNotEqual(
                    interval["gen"],
                    request_gen,
                    f"the {label} DB work must run inside the detached "
                    "commit's OWN checkout, never inside the request "
                    "dependency's checkout generation",
                )
        for record in recorder.checkouts:
            self.assertIsNotNone(
                record["closed_at"],
                "every checkout must be RELEASED once the outcome settles "
                f"(balanced generations); generation {record['gen']} is "
                "still open",
            )

    def test_a_drain_deadline_is_anchored_at_the_first_cancellation(self):
        """(a) The drain deadline starts at the FIRST cancellation. With the
        finalize step blocked and the constant patched to D, the second
        cancellation is delivered at ~0.67*D while the request task is still
        pending (asserted immediately before the cancel); the request must
        still exit within D + a margin strictly below 1.67*D. A MONOTONIC
        deadline anchored at the FIRST cancel exits at ~D; a RESTARTING
        deadline would exit at ~1.67*D and exceed the bound. The blocked op
        is released only after the request exited, so the only thing being
        measured is the deadline."""
        draft_id = self._create_draft(title="N05F drain deadline").json()["id"]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        request = _request(draft_id, dict(self._owner_headers()))
        outcome = {}
        elapsed = {}
        patched_deadline = 1.5

        async def _drive():
            with (
                patch.object(
                    draft_room_module, "_UPLOAD_COMMIT_DRAIN_SECONDS", patched_deadline
                ),
                _parking_finalize_patch(entered, release, finished),
            ):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    task = asyncio.create_task(client.post(**request))
                    loop = asyncio.get_running_loop()
                    if not await loop.run_in_executor(None, entered.wait, 30):
                        release.set()
                        raise AssertionError(
                            "the blocked finalize step was never entered"
                        )
                    task.cancel()
                    started = time.monotonic()
                    # Deliver the second cancellation at ~0.67*D — BEFORE
                    # the first deadline expires, while the request task is
                    # still pending (asserted below). A MONOTONIC deadline
                    # anchored at the FIRST cancel then exits at ~D; a
                    # RESTARTING deadline would exit at ~1.67*D.
                    await asyncio.sleep(patched_deadline * 2 / 3)
                    pending_at_second_cancel = not task.done()
                    elapsed["pending_at_second_cancel"] = pending_at_second_cancel
                    task.cancel()
                    outcome.update(await _settle_request_task(task))
                    elapsed["seconds"] = time.monotonic() - started
                    # Release the blocked op only AFTER the request exited,
                    # so the only thing being measured is the deadline.
                    release.set()
                    await loop.run_in_executor(None, finished.wait, 60)
                    await _drain_pending_tasks()

        asyncio.run(_drive())
        self.assertTrue(
            outcome["propagated"],
            "the double-cancellation must still propagate out of the request",
        )
        self.assertTrue(
            elapsed["pending_at_second_cancel"],
            "the request task must still be pending when the second "
            "cancellation is delivered (the first deadline must not have "
            "expired yet)",
        )
        margin = 0.5
        self.assertLessEqual(
            elapsed["seconds"],
            patched_deadline + margin,
            f"the request must exit within the FIRST cancellation's deadline "
            f"({patched_deadline}s) plus a {margin}s margin (a RESTARTING "
            f"deadline would exit at ~{patched_deadline * 5 / 3:.1f}s); took "
            f"{elapsed['seconds']:.1f}s — the drain deadline was restarted by "
            "the second cancellation",
        )

    def test_b_commit_owns_a_fresh_pool_checkout_after_teardown(self):
        """(b) The blocked enqueue is released only after the request task
        exited and its ``get_db`` connection was returned; the upload then
        settles COMMITTED (row + parse job + final bytes), and the
        ownership-generation oracle holds for the enqueue's DB work."""
        draft_id = self._create_draft(title="N05F ownership gen").json()["id"]
        recorder = _PoolGenerationRecorder(self._connection_pool)
        recorder.install()
        returned, state, restore_db = self._install_recording_db(recorder)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        enqueue_ops: list = []
        ids: dict = {}
        enqueue_patch = _parking_enqueue_patch(
            entered, release, finished, "real", enqueue_ops, ids
        )
        request = _request(draft_id, dict(self._owner_headers()))
        outcome = {}

        async def _drive():
            with enqueue_patch:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    task = asyncio.create_task(client.post(**request))
                    loop = asyncio.get_running_loop()
                    if not await loop.run_in_executor(None, entered.wait, 30):
                        release.set()
                        raise AssertionError(
                            "the blocked enqueue step was never entered"
                        )
                    task.cancel()
                    outcome.update(await _settle_request_task(task))
                    # Teardown ordering: the request's dependency connection
                    # must be back in the pool BEFORE the blocked op runs.
                    if not await loop.run_in_executor(None, returned.wait, 30):
                        release.set()
                        raise AssertionError(
                            "the request's dependency connection was never "
                            "returned (teardown did not run)"
                        )
                    release.set()
                    await loop.run_in_executor(None, finished.wait, 60)
                    await _drain_pending_tasks()

        try:
            asyncio.run(_drive())
        finally:
            restore_db()
            recorder.restore()

        self.assertTrue(outcome["propagated"], "the cancellation must propagate")
        self.assertIsNotNone(
            self._row(draft_id),
            "the released commit must reach a definite outcome — the "
            "draft_inputs row must be present (committed)",
        )
        self.assertGreaterEqual(
            self._parse_job_count(draft_id),
            1,
            "a committed upload must have its parse job row",
        )
        self.assertTrue(
            self._final_files(draft_id),
            "a committed upload must have its final bytes",
        )
        self._assert_detached_ownership(
            recorder, enqueue_ops, state["request_gen"], "enqueue (commit path)"
        )

    def test_c_late_failure_is_compensated_and_logged_without_paths_or_content(self):
        """(c) The enqueue raises only after the request exited and its
        dependency teardown ran; the outcome must be COMPENSATED (row gone,
        final bytes gone), the compensation's row-delete obeys the same
        ownership-generation oracle, and EXACTLY ONE ERROR record carries
        the draft id AND input id AND exception class — never a storage
        path or any uploaded content."""
        draft_id = self._create_draft(title="N05F late failure").json()["id"]
        recorder = _PoolGenerationRecorder(self._connection_pool)
        recorder.install()
        returned, state, restore_db = self._install_recording_db(recorder)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        enqueue_ops: list = []
        delete_ops: list = []
        ids: dict = {}
        enqueue_patch = _parking_enqueue_patch(
            entered, release, finished, "raise", enqueue_ops, ids
        )
        delete_patch = _parking_delete_input_row_patch(delete_ops)
        request = _request(draft_id, dict(self._owner_headers()))
        outcome = {}

        async def _drive():
            with enqueue_patch, delete_patch:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    task = asyncio.create_task(client.post(**request))
                    loop = asyncio.get_running_loop()
                    if not await loop.run_in_executor(None, entered.wait, 30):
                        release.set()
                        raise AssertionError(
                            "the blocked enqueue step was never entered"
                        )
                    task.cancel()
                    outcome.update(await _settle_request_task(task))
                    if not await loop.run_in_executor(None, returned.wait, 30):
                        release.set()
                        raise AssertionError(
                            "the request's dependency connection was never "
                            "returned (teardown did not run)"
                        )
                    release.set()
                    await loop.run_in_executor(None, finished.wait, 60)
                    await _drain_pending_tasks()

        try:
            with self.assertLogs(level="ERROR") as captured:
                asyncio.run(_drive())
        finally:
            restore_db()
            recorder.restore()

        self.assertTrue(outcome["propagated"], "the cancellation must propagate")
        self.assertIsNone(
            self._row(draft_id),
            "a late enqueue failure after the request exited must be "
            "compensated — the reserved draft_inputs row must be gone",
        )
        self.assertEqual(
            self._final_files(draft_id),
            [],
            "a compensated upload must leave no final bytes",
        )
        self._assert_detached_ownership(
            recorder, enqueue_ops, state["request_gen"], "enqueue (failure recorded)"
        )
        self._assert_detached_ownership(
            recorder,
            delete_ops,
            state["request_gen"],
            "row-delete (compensation path)",
            require_detached=False,
        )

        input_id = ids.get("input_id")
        self.assertIsNotNone(input_id, "the enqueue attempt must carry input_id")
        messages = [record.getMessage() for record in captured.records]
        matching = [
            message
            for message in messages
            if "RuntimeError" in message
            and str(draft_id) in message
            and str(input_id) in message
        ]
        self.assertEqual(
            len(matching),
            1,
            "exactly ONE ERROR record must carry the exception class, the "
            f"draft id and the input id; got {messages!r}",
        )
        self.assertNotIn(
            "draft-room",
            matching[0],
            "the sanitized ERROR record must not carry a storage path",
        )
        self.assertNotIn(
            "N05F-SECRET-CONTENT",
            matching[0],
            "the sanitized ERROR record must not carry uploaded content",
        )
        self.assertFalse(
            any("N05F-SECRET-CONTENT" in message for message in messages),
            "no log record at all may carry the uploaded file's content",
        )

    def test_d_inflight_commit_is_strongly_referenced_and_then_removed(self):
        """(d) While the commit is parked mid-flight, its task must be a
        member of ``_INFLIGHT_UPLOAD_COMMITS`` (a strong reference the event
        loop cannot GC), and after the outcome settles the set must no
        longer hold it."""
        draft_id = self._create_draft(title="N05F strong ref").json()["id"]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        request = _request(draft_id, dict(self._owner_headers()))
        inflight: dict = {}
        outcome = {}

        async def _drive():
            with _parking_finalize_patch(entered, release, finished):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    task = asyncio.create_task(client.post(**request))
                    loop = asyncio.get_running_loop()
                    if not await loop.run_in_executor(None, entered.wait, 30):
                        release.set()
                        raise AssertionError(
                            "the blocked finalize step was never entered"
                        )
                    # The parked finalize proves the commit task is mid-flight
                    # right now, so its registration must already be visible.
                    inflight["snapshot"] = set(_INFLIGHT_UPLOAD_COMMITS)
                    task.cancel()
                    outcome.update(await _settle_request_task(task))
                    release.set()
                    await loop.run_in_executor(None, finished.wait, 60)
                    await _drain_pending_tasks()

        asyncio.run(_drive())

        self.assertTrue(
            inflight["snapshot"],
            "during the in-flight window the upload commit task must be a "
            "member of _INFLIGHT_UPLOAD_COMMITS (strong reference)",
        )
        self.assertTrue(
            inflight["snapshot"].isdisjoint(_INFLIGHT_UPLOAD_COMMITS),
            "after the upload settles, its commit task must be removed from "
            "_INFLIGHT_UPLOAD_COMMITS",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
